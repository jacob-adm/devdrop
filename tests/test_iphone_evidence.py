# SPDX-License-Identifier: Apache-2.0
"""A physical iPhone's log and screenshot through the optional pymobiledevice3 tool."""
import json
import os
import unittest

from devdrop import deploy
from tests.fakes import FakeTools
from tests.test_devices import devdrop
from tests.test_ios_install import IPHONE, xcrun_rules
from tests.test_screenshot import IMAGE

FOLDER = "file:///private/var/containers/Bundle/Application/X/My%20App.app"
APPS = {"result": {"apps": [{"url": FOLDER + "/"}]}}
RUNNING = {"result": {"runningProcesses": [{"executable": FOLDER + "/MyExec"}]}}
GONE = {"result": {"runningProcesses": [{"executable": "file:///usr/libexec/other"}]}}
# The device log as pymobiledevice3 prints it: the app under its executable's name, and others.
SYSLOG = ("2026-11-20 16:50:33.940749 MyExec{libxpc.dylib}[12284] <NOTICE>: activating connection\n"
          "2026-11-20 16:50:33.941000 audiomxd{AudioToolbox}[110] <NOTICE>: unrelated\n"
          "2026-11-20 16:50:33.942000 My App[12284] <FAULT>: Fatal error: boom\n"
          "2026-11-20 16:50:33.943000 MyExecHelper[7] <NOTICE>: a different process\n")


def state_rules(procs):
    return [{"match": ["devicectl", "device", "info", "apps"], "write_after": "--json-output",
             "file": json.dumps(APPS)},
            {"match": ["devicectl", "device", "info", "processes"], "write_after": "--json-output",
             "file": json.dumps(procs)}]


class ProcessLines(unittest.TestCase):
    def test_keeps_only_the_named_processes(self):
        kept = deploy.process_lines(SYSLOG, {"MyExec", "My App"})
        self.assertEqual(kept.count("\n"), 2)
        self.assertIn("activating connection", kept)
        self.assertIn("Fatal error: boom", kept)
        self.assertEqual(deploy.process_lines("not a log line\n\n", {"MyExec"}), "")


class IphoneEvidence(unittest.TestCase):
    def setUp(self):
        self.fake = FakeTools()
        self.addCleanup(self.fake.close)
        self.logs = os.path.join(self.fake.root, "logs")
        self.shots = os.path.join(self.fake.root, "shots")

    def launch(self, procs=RUNNING, xcrun_extra=(), tool_rules=None):
        self.fake.add("xcrun", xcrun_rules(extra=list(xcrun_extra) + state_rules(procs)))
        if tool_rules is not None:
            self.fake.add("pymobiledevice3", tool_rules)
        r = devdrop(self.fake, "launch", "com.example.app", "--to", IPHONE, "--logs", self.logs,
                    "--log-seconds", "1", "--screenshot", self.shots, "--screenshot-seconds", "1", "--json")
        return json.loads(r.stdout)["results"][0]

    TOOL = [{"match": ["syslog", "live", "--udid", IPHONE], "stdout": SYSLOG},
            {"match": ["dvt", "screenshot", "--userspace"], "write_after": IPHONE, "file_hex": IMAGE}]

    def test_log_and_screenshot_are_saved_like_any_other_device(self):
        rec = self.launch(tool_rules=self.TOOL)
        self.assertEqual((rec["result"], rec["running"], rec["warnings"]), ("ok", True, []))
        self.assertEqual(rec["log"], os.path.join(self.logs, IPHONE + ".log"))
        with open(rec["log"], encoding="utf-8") as f:
            text = f.read()
        self.assertIn("activating connection", text)
        self.assertNotIn("audiomxd", text)                      # other processes are left out
        self.assertNotIn("MyExecHelper", text)
        with open(rec["screenshot"], "rb") as f:
            self.assertTrue(f.read().startswith(deploy.PNG))
        tool = self.fake.calls("pymobiledevice3")
        self.assertTrue(all(IPHONE in c for c in tool), tool)   # always aimed at this device

    def test_a_crash_is_reported_with_its_cause_from_the_device_log(self):
        rec = self.launch(procs=GONE, tool_rules=self.TOOL)
        self.assertEqual((rec["result"], rec["stage"], rec["reason"]),
                         ("failed", "running", "app exited within 1s: Fatal error: boom"))
        self.assertTrue(os.path.isfile(rec["log"]))

    def test_without_the_tool_nothing_changes(self):
        rec = self.launch()
        self.assertEqual((rec["result"], rec["running"], rec["log"]), ("ok", True, None))
        self.assertEqual(rec["warnings"], ["no screenshot: not available on a physical iPhone or iPad"])

    def test_a_tool_failure_is_a_warning_and_no_log(self):
        rec = self.launch(tool_rules=[
            {"match": ["syslog"], "rc": 1, "stderr": "ERROR Device is not connected\n"},
            {"match": ["screenshot"], "rc": 1, "stderr":
             "2026-11-20 16:27:22 host.lan pymobiledevice3.__main__[41198] ERROR Device is not connected\n"
             "2026-11-20 16:27:22 host.lan pymobiledevice3.__main__[41198] INFO Run again with -v for the "
             "full traceback\n"}])
        self.assertEqual((rec["result"], rec["running"], rec["log"]), ("ok", True, None))
        self.assertEqual(rec["warnings"], ["no screenshot: Device is not connected"])
        self.assertNotIn("screenshot", rec)

    def test_a_failed_launch_leaves_no_log_behind(self):
        rec = self.launch(tool_rules=self.TOOL, xcrun_extra=[
            {"match": ["devicectl", "device", "process", "launch"], "rc": 1,
             "stderr": "ERROR: The application is not installed.\n"}])
        self.assertEqual((rec["result"], rec["stage"]), ("failed", "launch"))
        self.assertNotIn("log", rec)
        self.assertFalse(os.path.exists(os.path.join(self.logs, IPHONE + ".log")))
        tmp = os.path.join(self.fake.root, "tmp")
        self.assertEqual([n for n in os.listdir(tmp) if n.endswith(".syslog")], [])

    def test_doctor_names_the_optional_tool(self):
        self.fake.add("xcrun", xcrun_rules())
        r = devdrop(self.fake, "doctor")
        self.assertIn("ok  pymobiledevice3 not found (optional)", r.stdout)
        self.fake.add("pymobiledevice3", [])
        r = devdrop(self.fake, "doctor")
        self.assertIn("logs and screenshots on physical iPhones", r.stdout)


if __name__ == "__main__":
    unittest.main()
