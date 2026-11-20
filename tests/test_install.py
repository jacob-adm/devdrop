# SPDX-License-Identifier: Apache-2.0
import json
import os
import unittest

from devdrop import config, deploy
from devdrop.device import Device
from devdrop.select import select
from tests import builders as b
from tests.fakes import FakeTools
from tests.test_devices import devdrop

PROPS = "[ro.build.version.release]: [{rel}]\n[ro.build.version.sdk]: [{api}]\n" \
        "[ro.product.cpu.abilist]: [arm64-v8a]\n[ro.product.model]: [{model}]\n"
LAB = [("AAA111", 34, "14", "Pixel 8"), ("BBB222", 34, "14", "Galaxy S24"), ("CCC333", 23, "6.0", "Nexus 5")]


def adb_rules(devices=LAB, install_out=None, extra=()):
    listing = "List of devices attached\n" + "".join(f"{d[0]} device model:{d[3].replace(' ', '_')}\n"
                                                     for d in devices)
    rules = list(extra) + [{"match": ["devices", "-l"], "stdout": listing}]
    for serial, api, rel, model in devices:
        rules += [
            {"match": ["-s", serial, "getprop"], "stdout": PROPS.format(rel=rel, api=api, model=model)},
            {"match": ["-s", serial, "install"], "stdout": (install_out or {}).get(serial, "Performing Streamed Install\nSuccess\n")},
            {"match": ["-s", serial, "uninstall"], "stdout": "Success\n"},
            {"match": ["-s", serial, "resolve-activity"],
             "stdout": "priority=0 preferredOrder=0 match=0x108000 specificIndex=-1 isDefault=false\n"
                       "com.example.app/.MainActivity\n"},
            {"match": ["-s", serial, "am", "start"],
             "stdout": "Starting: Intent { cmp=com.example.app/.MainActivity }\n"},
        ]
    return rules


class Selectors(unittest.TestCase):
    DEVS = [
        Device("AAA111", "android", "physical", "ready", model="Pixel 8", api=34),
        Device("emulator-5554", "android", "emulator", "ready", model="sdk gphone64", api=35),
        Device("CCC333", "android", "physical", "ready", model="Nexus 5", api=23),
        Device("UDID1", "ios", "physical", "ready", model="iPhone 17 Pro", os_version="26.1", name="Lab phone"),
        Device("SIM1", "ios", "simulator", "ready", model="iPhone 16", os_version="18.5"),
    ]
    CFG = config.parse({"devices": {"old": "CCC333", "p8": "AAA111"}, "groups": {"lab": ["old", "p8"]}})

    def ids(self, sel):
        return [d.id for d in select(self.DEVS, sel, self.CFG)]

    def test_terms(self):
        cases = {
            "all": ["AAA111", "emulator-5554", "CCC333", "UDID1", "SIM1"],
            "android": ["AAA111", "emulator-5554", "CCC333"],
            "sim": ["SIM1"],
            "emulator": ["emulator-5554"],
            "physical,ios": ["AAA111", "CCC333", "UDID1", "SIM1"],
            "api>=30": ["AAA111", "emulator-5554"],
            "api<24": ["CCC333"],
            "ios>=26": ["UDID1"],
            "ios=18.5": ["SIM1"],
            "ios<19": ["SIM1"],
            "lab": ["AAA111", "CCC333"],
            "old": ["CCC333"],
            "pixel*": ["AAA111"],
            "*iphone*": ["UDID1", "SIM1"],
            "*phone*": ["emulator-5554", "UDID1", "SIM1"],
            "lab*": ["UDID1"],           # pattern matches the device's own name
            "emulator-5554": ["emulator-5554"],
            "nope": [],
        }
        for sel, want in cases.items():
            with self.subTest(sel=sel):
                self.assertEqual(self.ids(sel), want)


class FailureText(unittest.TestCase):
    def test_known_and_unknown_codes(self):
        self.assertIn("use --clean", deploy.android_failure(
            "Performing Streamed Install\nadb: failed to install x.apk: Failure "
            "[INSTALL_FAILED_UPDATE_INCOMPATIBLE: Existing package com.x signatures do not match]\n"))
        self.assertEqual(deploy.android_failure("Failure [INSTALL_FAILED_SOMETHING_NEW]"),
                         "INSTALL_FAILED_SOMETHING_NEW")
        self.assertEqual(deploy.android_failure("adb: device offline\n"), "adb: device offline")


class InstallCommand(unittest.TestCase):
    def setUp(self):
        self.fake = FakeTools()
        self.addCleanup(self.fake.close)
        self.apk = os.path.join(self.fake.root, "app.apk")
        b.apk(self.apk, b.axml(b.manifest(min_sdk=26)), v2=True)

    def adb(self, **kw):
        self.fake.add("adb", adb_rules(**kw))

    def installs(self):
        return [c[1] for c in self.fake.calls("adb") if "install" in c]

    def test_acceptance_two_installed_and_launched_one_skipped(self):
        self.adb()
        r = devdrop(self.fake, "install", self.apk, "--launch", "--json")
        self.assertEqual(r.returncode, 1, r.stderr)
        out = json.loads(r.stdout)
        recs = {x["id"]: x for x in out["results"]}
        self.assertEqual(len(out["results"]), 3)
        self.assertEqual((recs["AAA111"]["result"], recs["AAA111"]["stage"]), ("ok", "launch"))
        self.assertEqual(recs["BBB222"]["result"], "ok")
        self.assertEqual((recs["CCC333"]["result"], recs["CCC333"]["reason"]),
                         ("skipped", "API 23 < minSdk 26"))
        self.assertEqual(sorted(self.installs()), ["AAA111", "BBB222"])
        starts = [c for c in self.fake.calls("adb") if "am" in c and "start" in c]
        self.assertEqual(sorted(c[1] for c in starts), ["AAA111", "BBB222"])
        self.assertIn("com.example.app/.MainActivity", starts[0])

    def test_dry_run_reports_verdicts_and_sends_nothing(self):
        self.adb()
        r = devdrop(self.fake, "install", self.apk, "--dry-run", "--clean", "--launch", "--json")
        self.assertEqual(r.returncode, 1, r.stderr)     # one device would be skipped
        recs = {x["id"]: x for x in json.loads(r.stdout)["results"]}
        for serial in ("AAA111", "BBB222"):
            self.assertEqual((recs[serial]["result"], recs[serial]["stage"], recs[serial]["dry_run"]),
                             ("ok", "preflight", True))
            self.assertIn("nothing was sent", recs[serial]["reason"])
        self.assertEqual((recs["CCC333"]["result"], recs["CCC333"]["reason"]),
                         ("skipped", "API 23 < minSdk 26"))
        self.assertNotIn("dry_run", recs["CCC333"])
        sent = [c for c in self.fake.calls("adb") if {"install", "uninstall", "am", "resolve-activity"} & set(c)]
        self.assertEqual(sent, [])

    def test_dry_run_all_ok_exit_0_and_text(self):
        self.adb()
        r = devdrop(self.fake, "install", self.apk, "--dry-run", "--to", "api>=30")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("preflight: would install; nothing was sent (dry run)", r.stdout)
        self.assertEqual(self.installs(), [])

    def test_dry_run_with_watch_is_a_usage_error(self):
        self.adb()
        r = devdrop(self.fake, "install", self.apk, "--dry-run", "--watch")
        self.assertEqual(r.returncode, 2)
        self.assertIn("--dry-run cannot be used with --watch", r.stderr)

    def test_text_output_and_all_ok_exit_0(self):
        self.adb()
        r = devdrop(self.fake, "install", self.apk, "--to", "api>=30")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("2 ok, 0 skipped, 0 failed", r.stdout)
        self.assertNotIn("CCC333", r.stdout)

    def test_install_failure_is_explained(self):
        self.adb(install_out={"AAA111": "adb: failed to install app.apk: Failure "
                                        "[INSTALL_FAILED_UPDATE_INCOMPATIBLE: signatures do not match]\n"})
        r = devdrop(self.fake, "install", self.apk, "--to", "AAA111", "--json")
        rec = json.loads(r.stdout)["results"][0]
        self.assertEqual((rec["result"], rec["stage"]), ("failed", "install"))
        self.assertIn("different key; use --clean", rec["reason"])
        self.assertEqual(r.returncode, 1)

    def test_clean_uninstalls_before_install_and_tolerates_missing_app(self):
        rules = adb_rules()
        rules = [dict(x, stdout="Failure [DELETE_FAILED_INTERNAL_ERROR]\n") if "uninstall" in x["match"] else x
                 for x in rules]
        self.fake.add("adb", rules)
        r = devdrop(self.fake, "install", self.apk, "--clean", "--to", "p*", "--json")
        self.assertEqual(r.returncode, 0, r.stdout)
        calls = [c for c in self.fake.calls("adb") if c[:2] == ["-s", "AAA111"]]
        verbs = [c[2] for c in calls if c[2] in ("uninstall", "install")]
        self.assertEqual(verbs, ["uninstall", "install"])

    def test_launch_falls_back_to_manifest_launcher(self):
        self.adb(extra=[{"match": ["resolve-activity"], "rc": 255, "stderr": "cmd: Can't find service: package\n"}])
        r = devdrop(self.fake, "install", self.apk, "--launch", "--to", "AAA111", "--json")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        start = next(c for c in self.fake.calls("adb") if "am" in c)
        self.assertIn("com.example.app/com.example.app.MainActivity", start)

    def test_unsigned_apk_skipped_everywhere(self):
        self.adb()
        unsigned = os.path.join(self.fake.root, "unsigned.apk")
        b.apk(unsigned, b.axml(b.manifest()))
        r = devdrop(self.fake, "install", unsigned, "--json")
        self.assertEqual({x["reason"] for x in json.loads(r.stdout)["results"]}, {"APK is not signed"})
        self.assertEqual(self.installs(), [])

    def test_no_match_exits_3_and_no_adb_exits_4(self):
        self.adb()
        r = devdrop(self.fake, "install", self.apk, "--to", "ios")
        self.assertEqual(r.returncode, 3)
        self.assertIn("no android device matches", r.stderr)
        empty = FakeTools()
        self.addCleanup(empty.close)
        r = devdrop(empty, "install", self.apk)
        self.assertEqual(r.returncode, 4)

    def test_bad_jobs_value_is_a_usage_error(self):
        self.adb()
        r = devdrop(self.fake, "install", self.apk, "--jobs", "0")
        self.assertEqual(r.returncode, 2)


class LaunchUninstallCommands(unittest.TestCase):
    def setUp(self):
        self.fake = FakeTools()
        self.addCleanup(self.fake.close)

    def test_launch_and_uninstall_by_package(self):
        self.fake.add("adb", adb_rules())
        r = devdrop(self.fake, "launch", "com.example.app", "--to", "AAA111")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        r = devdrop(self.fake, "uninstall", "com.example.app", "--to", "AAA111,BBB222", "--json")
        self.assertEqual(json.loads(r.stdout)["ok"], 2)

    def test_uninstall_of_missing_app_fails_with_reason(self):
        rules = [dict(x, stdout="Failure [DELETE_FAILED_INTERNAL_ERROR]\n") if "uninstall" in x["match"] else x
                 for x in adb_rules()]
        self.fake.add("adb", rules)
        r = devdrop(self.fake, "uninstall", "com.example.app", "--to", "AAA111", "--json")
        rec = json.loads(r.stdout)["results"][0]
        self.assertEqual((rec["result"], rec["reason"]), ("failed", "not installed"))
        self.assertEqual(r.returncode, 1)


if __name__ == "__main__":
    unittest.main()


class RecordFields(unittest.TestCase):
    """Every result names the device family it came from."""

    def test_android_and_ios_records(self):
        fake = FakeTools()
        self.addCleanup(fake.close)
        props = PROPS + "[ro.product.manufacturer]: [{maker}]\n"
        rules = adb_rules()
        for i, (serial, api, rel, model) in enumerate(LAB):
            maker = ("samsung", "Google", "LGE")[i]
            rules.insert(0, {"match": ["-s", serial, "getprop"],
                             "stdout": props.format(rel=rel, api=api, model=model, maker=maker)})
        fake.add("adb", rules)
        apk = os.path.join(fake.root, "a.apk")
        b.apk(apk, b.axml(b.manifest(min_sdk=26)), v2=True)
        out = json.loads(devdrop(fake, "install", apk, "--json").stdout)
        recs = {r["id"]: r for r in out["results"]}
        self.assertEqual({k: recs["AAA111"][k] for k in ("manufacturer", "model", "os_version", "api", "kind")},
                         {"manufacturer": "samsung", "model": "Pixel 8", "os_version": "14", "api": 34,
                          "kind": "physical"})
        self.assertEqual((recs["CCC333"]["manufacturer"], recs["CCC333"]["api"], recs["CCC333"]["result"]),
                         ("LGE", 23, "skipped"))
        table = devdrop(fake, "devices").stdout
        self.assertIn("MAKER", table.splitlines()[0])
        self.assertIn("samsung", table)

    def test_ios_records_say_apple(self):
        from tests.test_ios_install import xcrun_rules as ios_rules, IPHONE
        fake = FakeTools()
        self.addCleanup(fake.close)
        fake.add("xcrun", ios_rules())
        r = json.loads(devdrop(fake, "launch", "com.example.app", "--to", "ios", "--json").stdout)
        recs = {x["id"]: x for x in r["results"]}
        self.assertEqual((recs[IPHONE]["manufacturer"], recs[IPHONE]["model"], recs[IPHONE]["os_version"]),
                         ("Apple", "iPhone 17 Pro Max", "26.5.1"))
        sim = next(x for x in r["results"] if x["kind"] == "simulator")
        self.assertEqual((sim["manufacturer"], sim["os_version"]), ("Apple", "18.5"))
