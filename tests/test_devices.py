# SPDX-License-Identifier: Apache-2.0
import json
import os
import subprocess
import sys
import unittest

from devdrop import android, config, ios
from tests.fakes import XCODEBUILD_RULES, FakeTools, adb_rules, fixture, xcrun_rules

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def devdrop(fake, *args, env_extra=None, cwd=None):
    env = fake.env()
    env["PYTHONPATH"] = REPO
    env.update(env_extra or {})
    return subprocess.run([sys.executable, "-m", "devdrop", *args], capture_output=True,
                          text=True, env=env, cwd=cwd or fake.root, timeout=60)


class ParseAdb(unittest.TestCase):
    def test_states_kinds_and_transports(self):
        devs = {d.id: d for d in android.parse_devices(fixture("adb_devices.txt"))}
        self.assertEqual(len(devs), 6)
        self.assertEqual((devs["emulator-5554"].kind, devs["emulator-5554"].state), ("emulator", "ready"))
        self.assertEqual(devs["FA35DS911282"].state, "unauthorized")
        self.assertEqual((devs["192.168.1.20:5555"].state, devs["192.168.1.20:5555"].transport),
                         ("offline", "wifi"))
        self.assertEqual(devs["adb-0A1B2C3D-Xy9zQw._adb-tls-connect._tcp"].transport, "wifi")
        self.assertEqual(devs["0123456789ABCDEF"].state, "no-permissions")
        self.assertEqual(devs["37201FDH2000CK"].model, "Pixel 8 Pro")

    def test_daemon_chatter_and_blank_lines_ignored(self):
        text = "* daemon not running; starting now at tcp:5037\n* daemon started successfully\n" \
               "List of devices attached\n\n"
        self.assertEqual(android.parse_devices(text), [])

    def test_getprop_skips_malformed_lines(self):
        props = android.parse_getprop(fixture("getprop_pixel8.txt"))
        self.assertEqual(props["ro.build.version.sdk"], "36")
        self.assertNotIn("persist.sys.multiline", props)


class ParseIos(unittest.TestCase):
    def test_devicectl_states_and_platform_filter(self):
        devs = {d.id: d for d in ios.parse_devicectl(json.loads(fixture("devicectl_list.json")))}
        self.assertEqual(devs["00008150-0000000000000001"].state, "ready")
        self.assertEqual(devs["00008150-0000000000000001"].transport, "usb")
        self.assertEqual(devs["00008150-0000000000000002"].state, "offline")
        self.assertEqual(devs["00008150-0000000000000003"].state, "developer-mode-off")
        self.assertEqual(devs["00008150-0000000000000003"].transport, "wifi")
        self.assertEqual(devs["00008150-0000000000000004"].state, "unpaired")
        self.assertNotIn("00008310-0000000000000005", devs)  # watchOS: not a mobile target
        self.assertEqual(devs["00008150-0000000000000001"].os_version, "26.5.1")

    def test_simctl_keeps_booted_ios_only_unless_all(self):
        doc = json.loads(fixture("simctl_list.json"))
        booted = ios.parse_simctl(doc)
        self.assertEqual([(d.model, d.os_version, d.state) for d in booted],
                         [("iPhone 16 Pro", "18.5", "ready")])
        everything = ios.parse_simctl(doc, include_shutdown=True)
        self.assertTrue(all(d.platform == "ios" for d in everything))
        self.assertIn("shutdown", {d.state for d in everything})
        self.assertFalse(any("Watch" in d.model or "TV" in d.model for d in everything))


class Config(unittest.TestCase):
    def test_valid(self):
        cfg = config.parse({"devices": {"p8": "37201FDH2000CK"}, "groups": {"lab": ["p8"]}})
        self.assertEqual(cfg.alias_for("37201FDH2000CK"), "p8")

    def test_rejections(self):
        bad = [
            {"device": {}},
            {"devices": {"a": ""}},
            {"devices": {"a": "X", "b": "X"}},
            {"devices": {"a": "X"}, "groups": {"g": ["a", "nope"]}},
            {"devices": {"a": "X"}, "groups": {"a": ["a"]}},
            {"groups": {"g": "a"}},
        ]
        for data in bad:
            with self.subTest(data=data), self.assertRaises(config.ConfigError):
                config.parse(data)


class DevicesCommand(unittest.TestCase):
    def setUp(self):
        self.fake = FakeTools()
        self.addCleanup(self.fake.close)

    def sdk_adb(self, rules=None):
        sdk = os.path.join(self.fake.root, "sdk")
        self.fake.add("adb", rules or adb_rules(), directory=os.path.join(sdk, "platform-tools"))
        return {"ANDROID_HOME": sdk}

    def test_both_platforms_json(self):
        env = self.sdk_adb()
        self.fake.add("xcrun", xcrun_rules())
        r = devdrop(self.fake, "devices", "--json", env_extra=env)
        self.assertEqual(r.returncode, 0, r.stderr)
        out = json.loads(r.stdout)
        devs = {d["id"]: d for d in out["devices"]}
        self.assertEqual(devs["37201FDH2000CK"]["api"], 36)
        self.assertEqual(devs["37201FDH2000CK"]["os_version"], "16")
        self.assertEqual(devs["emulator-5554"]["kind"], "emulator")
        self.assertEqual(devs["FA35DS911282"]["state"], "unauthorized")
        self.assertIsNone(devs["FA35DS911282"]["api"])
        self.assertEqual(devs["00008150-0000000000000001"]["platform"], "ios")
        self.assertEqual(len(out["devices"]), len(devs))  # each device once
        # getprop is asked only of ready devices
        asked = {c[1] for c in self.fake.calls("adb") if "getprop" in c}
        self.assertEqual(asked, {"emulator-5554", "37201FDH2000CK",
                                 "adb-0A1B2C3D-Xy9zQw._adb-tls-connect._tcp"})

    def test_no_xcode_reports_ios_unsupported_and_exits_0(self):
        env = self.sdk_adb()
        r = devdrop(self.fake, "devices", "--json", env_extra=env)
        self.assertEqual(r.returncode, 0, r.stderr)
        out = json.loads(r.stdout)
        self.assertIn("unsupported", out["backends"]["ios"]["error"])
        self.assertTrue(all(d["platform"] == "android" for d in out["devices"]))

    def test_adb_found_on_path_when_no_sdk(self):
        self.fake.add("adb", adb_rules())
        r = devdrop(self.fake, "devices", "--json")
        self.assertEqual(r.returncode, 0, r.stderr)
        # normcase: Windows lookups may return the extension in PATHEXT's case (adb.EXE).
        self.assertEqual(os.path.normcase(json.loads(r.stdout)["backends"]["android"]["tool"]),
                         os.path.normcase(os.path.join(self.fake.bin, "adb.exe" if os.name == "nt" else "adb")))

    def test_no_tools_exits_4(self):
        r = devdrop(self.fake, "devices")
        self.assertEqual(r.returncode, 4)
        self.assertIn("adb not found", r.stderr)

    def test_old_xcode_lists_simulators_only(self):
        self.fake.add("xcrun", xcrun_rules(devicectl=False))
        r = devdrop(self.fake, "devices", "--json")
        out = json.loads(r.stdout)
        self.assertEqual([d["kind"] for d in out["devices"]], ["simulator"])
        self.assertIn("Xcode 15", out["backends"]["ios"]["error"])

    def test_adb_failure_exits_1_and_keeps_ios(self):
        env = self.sdk_adb([{"match": ["devices"], "rc": 1, "stderr": "cannot connect to daemon\n"}])
        self.fake.add("xcrun", xcrun_rules())
        r = devdrop(self.fake, "devices", env_extra=env)
        self.assertEqual(r.returncode, 1)
        self.assertIn("cannot connect to daemon", r.stderr)
        self.assertIn("00008150-0000000000000001", r.stdout)

    def test_device_that_stops_answering_is_marked(self):
        rules = adb_rules()
        rules.insert(1, {"match": ["-s", "37201FDH2000CK", "getprop"], "rc": 1,
                         "stderr": "error: closed\n"})
        r = devdrop(self.fake, "devices", "--json", env_extra=self.sdk_adb(rules))
        devs = {d["id"]: d for d in json.loads(r.stdout)["devices"]}
        self.assertEqual(devs["37201FDH2000CK"]["state"], "not-responding")

    def test_alias_from_config_and_bad_config_exits_2(self):
        env = self.sdk_adb()
        with open(os.path.join(self.fake.root, "devdrop.toml"), "w") as f:
            f.write('[devices]\nhtc = "FA35DS911282"\n')
        r = devdrop(self.fake, "devices", "--json", env_extra=env)
        devs = {d["id"]: d for d in json.loads(r.stdout)["devices"]}
        self.assertEqual(devs["FA35DS911282"]["name"], "htc")
        with open(os.path.join(self.fake.root, "devdrop.toml"), "w") as f:
            f.write("[devices\n")
        r = devdrop(self.fake, "devices", env_extra=env)
        self.assertEqual(r.returncode, 2)
        self.assertNotIn("Traceback", r.stderr)

    def test_control_characters_in_names_are_escaped(self):
        text = "List of devices attached\nX1  device model:Evil\x1b]0;pwned\x07 transport_id:1\n"
        rules = adb_rules(text) + [{"match": ["getprop"], "stdout": ""}]
        r = devdrop(self.fake, "devices", env_extra=self.sdk_adb(rules))
        self.assertNotIn("\x1b", r.stdout)
        self.assertIn("\\x1b", r.stdout)


class DoctorCommand(unittest.TestCase):
    def setUp(self):
        self.fake = FakeTools()
        self.addCleanup(self.fake.close)

    def test_hints_for_devices_that_need_action(self):
        self.fake.add("adb", adb_rules())
        self.fake.add("xcrun", xcrun_rules())
        self.fake.add("xcodebuild", XCODEBUILD_RULES)
        r = devdrop(self.fake, "doctor")
        self.assertEqual(r.returncode, 1, r.stderr)
        self.assertIn("adb", r.stdout)
        self.assertIn("Version 36.0.0", r.stdout)
        self.assertIn("FA35DS911282: unauthorized", r.stdout)
        self.assertIn("accept the USB debugging prompt", r.stdout)
        self.assertIn("Developer Mode", r.stdout)
        self.assertIn("tap Trust", r.stdout)
        self.assertIn("Xcode 26.2", r.stdout)

    def test_all_ready_exits_0(self):
        ready_only = "List of devices attached\nemulator-5554 device transport_id:1\n"
        self.fake.add("adb", adb_rules(ready_only))
        rules = xcrun_rules()
        rules[1] = dict(rules[1], file='{"result": {"devices": []}}')  # no physical devices
        self.fake.add("xcrun", rules)
        self.fake.add("xcodebuild", XCODEBUILD_RULES)
        r = devdrop(self.fake, "doctor")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertNotIn("!!", r.stdout)


if __name__ == "__main__":
    unittest.main()
