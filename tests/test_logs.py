# SPDX-License-Identifier: Apache-2.0
"""--logs: watching a launched app, saving device logs, reporting apps that die."""

import json
import os
import unittest

from devdrop import deploy
from tests import builders as b
from tests.fakes import FakeTools
from tests.test_devices import devdrop
from tests.test_install import adb_rules

# Real logcat crash output (API 35 emulator), trimmed.
CRASH = """--------- beginning of crash
11-14 18:09:25.175 17662 17662 E AndroidRuntime: FATAL EXCEPTION: main
11-14 18:09:25.175 17662 17662 E AndroidRuntime: Process: com.example.app, PID: 17662
11-14 18:09:25.175 17662 17662 E AndroidRuntime: java.lang.RuntimeException: Unable to start activity ComponentInfo{com.example.app/com.example.app.MainActivity}: java.lang.RuntimeException: boom
11-14 18:09:25.175 17662 17662 E AndroidRuntime: \tat android.app.ActivityThread.performLaunchActivity(ActivityThread.java:4048)
"""
# Real simulator log line (iOS 18.5) for a Swift fatalError.
FATAL = "2026-11-14 18:09:38.234 Df E2E[79416:c980aa1] (libswiftCore.dylib) E2E/crash.swift:5: Fatal error: boom\n"


class CrashLines(unittest.TestCase):
    def test_android(self):
        self.assertEqual(deploy._android_crash(CRASH, "com.example.app"),
                         (True, "java.lang.RuntimeException: Unable to start activity ComponentInfo{com.example.app/"
                                "com.example.app.MainActivity}: java.lang.RuntimeException: boom"))
        self.assertEqual(deploy._android_crash("nothing to see\n", "com.example.app"), (False, ""))
        self.assertEqual(deploy._android_crash(CRASH, "com.other.app"), (False, ""))     # someone else's crash

    def test_android_root_cause_from_newer_layout(self):
        # Android 17 and Android 7 put the message on a "Caused by:" line.
        text = CRASH.replace(": java.lang.RuntimeException: boom", "") + \
            "11-14 18:09:25.176 17662 17662 E AndroidRuntime: Caused by: java.lang.RuntimeException: boom\n"
        self.assertEqual(deploy._android_crash(text, "com.example.app"), (True, "java.lang.RuntimeException: boom"))

    def test_ios(self):
        self.assertEqual(deploy._ios_crash_line(FATAL), "Fatal error: boom")
        objc = "x E2E: *** Terminating app due to uncaught exception 'NSInvalidArgumentException'\n"
        self.assertTrue(deploy._ios_crash_line(objc).startswith("Terminating app due to uncaught exception"))
        self.assertEqual(deploy._ios_crash_line(""), "")


class AndroidLogs(unittest.TestCase):
    def setUp(self):
        self.fake = FakeTools()
        self.addCleanup(self.fake.close)
        self.apk = os.path.join(self.fake.root, "a.apk")
        b.apk(self.apk, b.axml(b.manifest(min_sdk=21)), v2=True)
        self.logs = os.path.join(self.fake.root, "logs")

    def run_install(self, extra_rules, *args):
        self.fake.add("adb", adb_rules(extra=extra_rules))
        return devdrop(self.fake, "install", self.apk, "--to", "AAA111,BBB222", "--launch",
                       "--logs", self.logs, "--log-seconds", "1", "--json", *args)

    def test_dead_and_alive_apps(self):
        r = self.run_install([
            {"match": ["-s", "AAA111", "pidof"], "stdout": ""},
            {"match": ["-s", "AAA111", "logcat", "-d"], "stdout": CRASH},
            {"match": ["-s", "BBB222", "pidof"], "stdout": "4242\n"},
            {"match": ["-s", "BBB222", "logcat", "-d"], "stdout": "I/ActivityManager: Start proc\n"},
            {"match": ["logcat", "-c"]}])
        self.assertEqual(r.returncode, 1, r.stderr)
        recs = {x["id"]: x for x in json.loads(r.stdout)["results"]}
        dead, alive = recs["AAA111"], recs["BBB222"]
        self.assertEqual((dead["result"], dead["stage"], dead["running"]), ("failed", "running", False))
        self.assertTrue(dead["reason"].startswith("app exited within 1s: java.lang.RuntimeException"))
        self.assertEqual((alive["result"], alive["running"]), ("ok", True))
        with open(dead["log"]) as f:
            self.assertIn("FATAL EXCEPTION", f.read())
        self.assertEqual(os.path.dirname(alive["log"]), self.logs)
        # logs are cleared before launch, then read after
        verbs = [c[2:4] for c in self.fake.calls("adb") if c[:2] == ["-s", "AAA111"] and "logcat" in c]
        self.assertEqual(verbs, [["logcat", "-b"], ["logcat", "-d"]])

    def test_crash_in_the_window_wins_over_a_live_process(self):
        # A slow device can still be starting the app, or restart it, when devdrop checks.
        r = self.run_install([{"match": ["pidof"], "stdout": "4242\n"},
                              {"match": ["-s", "AAA111", "logcat", "-d"], "stdout": CRASH},
                              {"match": ["logcat"], "stdout": ""}])
        recs = {x["id"]: x for x in json.loads(r.stdout)["results"]}
        self.assertEqual((recs["AAA111"]["running"], recs["AAA111"]["result"]), (False, "failed"))
        self.assertEqual((recs["BBB222"]["running"], recs["BBB222"]["result"]), (True, "ok"))

    def test_pidof_missing_counts_as_not_running(self):
        r = self.run_install([{"match": ["pidof"], "stdout": "/system/bin/sh: pidof: not found\n"},
                              {"match": ["logcat"], "stdout": ""}])
        self.assertEqual({x["running"] for x in json.loads(r.stdout)["results"]}, {False})

    def test_logs_needs_launch_and_text_output(self):
        self.fake.add("adb", adb_rules(extra=[{"match": ["pidof"], "stdout": "1\n"}, {"match": ["logcat"]}]))
        r = devdrop(self.fake, "install", self.apk, "--logs", self.logs)
        self.assertEqual((r.returncode, "--logs needs --launch" in r.stderr), (2, True))
        r = devdrop(self.fake, "launch", "com.example.app", "--to", "AAA111", "--logs", self.logs,
                    "--log-seconds", "1")
        self.assertIn("log: " + os.path.join(self.logs, "AAA111.log"), r.stdout)


class IosLogs(unittest.TestCase):
    def setUp(self):
        from tests.test_ios_install import xcrun_rules
        self.xcrun_rules = xcrun_rules
        self.fake = FakeTools()
        self.addCleanup(self.fake.close)
        self.logs = os.path.join(self.fake.root, "logs")

    def test_simulator_crash_and_device_running(self):
        from tests.test_ios_install import IPHONE
        procs = {"result": {"runningProcesses": [
            {"executable": "file:///private/var/containers/Bundle/Application/X/E2E.app/E2E"}]}}
        apps = {"result": {"apps": [{"url": "file:///private/var/containers/Bundle/Application/X/E2E.app/"}]}}
        self.fake.add("xcrun", self.xcrun_rules(extra=[
            {"match": ["launchctl", "list"], "stdout": "PID\tStatus\tLabel\n-\t0\tcom.apple.other\n"},
            {"match": ["simctl", "appinfo"], "stdout": '{\n    CFBundleExecutable = E2E;\n}\n'},
            {"match": ["log", "show"], "stdout": FATAL},
            {"match": ["devicectl", "device", "info", "apps"], "write_after": "--json-output",
             "file": json.dumps(apps)},
            {"match": ["devicectl", "device", "info", "processes"], "write_after": "--json-output",
             "file": json.dumps(procs)}]))
        r = devdrop(self.fake, "launch", "com.example.app", "--to", "sim," + IPHONE, "--logs", self.logs,
                    "--log-seconds", "1", "--json")
        recs = {x["id"]: x for x in json.loads(r.stdout)["results"]}
        sim = next(x for x in recs.values() if x["kind"] == "simulator")
        self.assertEqual((sim["result"], sim["reason"]), ("failed", "app exited within 1s: Fatal error: boom"))
        predicate = next(c for c in self.fake.calls("xcrun") if "log" in c and "show" in c)
        self.assertIn('process == "E2E"', predicate)
        self.assertEqual((recs[IPHONE]["result"], recs[IPHONE]["running"], recs[IPHONE]["log"]),
                         ("ok", True, None))

    def test_unknown_state_is_not_a_failure(self):
        from tests.test_ios_install import IPHONE
        self.fake.add("xcrun", self.xcrun_rules(extra=[
            {"match": ["devicectl", "device", "info"], "rc": 1, "stderr": "ERROR: device busy\n"}]))
        r = devdrop(self.fake, "launch", "com.example.app", "--to", IPHONE, "--logs", self.logs,
                    "--log-seconds", "1", "--json")
        rec = json.loads(r.stdout)["results"][0]
        self.assertEqual((rec["result"], rec["running"]), ("ok", None))


if __name__ == "__main__":
    unittest.main()
