# SPDX-License-Identifier: Apache-2.0
import hashlib
import json
import os
import tempfile
import unittest

from devdrop import artifact, config, deploy, preflight
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


# `dumpsys package` as printed by an Android 15 emulator, cut to the lines that matter.
DUMPSYS_35 = """Packages:
  Package [com.example.app] (bb0c7e9):
    appId=10212
    versionCode={code} minSdk=21 targetSdk=35
    signatures=PackageSignatures{{4fe50fc version:4, signatures:[{ids}], past signatures:[{past}]}}
"""
# The same from an Android 6 emulator: the bracketed number changes with every install.
DUMPSYS_23 = """Packages:
  Package [com.example.app] (8a56907):
    versionCode={code} targetSdk=35
    signatures=PackageSignatures{{962990c [4e50255]}}
"""
CERT = b"0\x82\x02\xff certificate one"
OTHER = b"0\x82\x02\xfe certificate two"


class InstalledState(unittest.TestCase):
    def test_cert_id_is_javas_array_hash(self):
        self.assertEqual(artifact.android_cert_id(b""), "1")
        self.assertEqual(artifact.android_cert_id(b"\x01"), "20")          # 31 * 1 + 1
        self.assertEqual(artifact.android_cert_id(b"\xff"), "1e")          # bytes are signed: 31 - 1
        # Past 32 bits and with negative bytes; both values are what Java's Arrays.hashCode prints.
        self.assertEqual(artifact.android_cert_id(b"\x7f" * 8), "3b21de81")
        self.assertEqual(artifact.android_cert_id(b"\xc8" * 40), "ca641f01")

    def test_certs_read_from_the_signing_block(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "a.apk")
            b.apk(path, b.axml(b.manifest()), v2=True, certs=(CERT, OTHER))
            self.assertEqual(artifact.signer_certs(path), [CERT, OTHER])
            report = artifact.inspect(path)
            self.assertEqual(report["cert_ids"], [artifact.android_cert_id(CERT), artifact.android_cert_id(OTHER)])
            self.assertEqual(report["certs"][0], hashlib.sha256(CERT).hexdigest())
            self.assertEqual(report["size"], os.path.getsize(path))
            b.apk(path, b.axml(b.manifest()), v1=True)
            self.assertEqual(artifact.signer_certs(path), [])
            b.apk(path, b.axml(b.manifest()), v2=True)          # a block with no signer in it
            self.assertEqual(artifact.signer_certs(path), [])

    def test_parse_installed(self):
        parse = deploy.parse_installed
        self.assertEqual(parse(DUMPSYS_35.format(code=7, ids="15f777a2", past=""), "com.example.app"),
                         {"version_code": 7, "cert_ids": ["15f777a2"]})
        # Past certificates as a device prints them after a key rotation: only one whose flags
        # (hex) grant rollback is accepted again. 1f does, 17 and 1 do not.
        self.assertEqual(parse(DUMPSYS_35.format(code=7, ids="aa, bb", past="cc flags: 1f, dd flags: 17, ee flags: 1"),
                               "com.example.app")["cert_ids"], ["aa", "bb", "cc"])
        self.assertEqual(parse(DUMPSYS_23.format(code=3), "com.example.app"),
                         {"version_code": 3, "cert_ids": None})
        for text in ("Unable to find package: com.example.app\n", "", DUMPSYS_35.format(code=1, ids="aa", past="")
                     .replace("com.example.app", "com.example.application")):
            self.assertEqual(parse(text, "com.example.app"), {"version_code": None, "cert_ids": None})

    def test_verdicts(self):
        dev = Device("A", "android", "physical", "ready", api=35)
        report = {"type": "apk", "build": "7", "cert_ids": ["aa"], "size": 5 * 1024 * 1024}
        same = {"version_code": 7, "cert_ids": ["aa"], "free_kb": 900000}
        check = preflight.installed
        self.assertIsNone(check(report, dev, same))
        self.assertIsNone(check(report, dev, None))                     # the device could not be asked
        self.assertIsNone(check(report, dev, {"version_code": None, "cert_ids": None, "free_kb": None}))
        self.assertIn("different key", check(report, dev, same | {"cert_ids": ["bb"]}))
        self.assertIsNone(check(report, dev, same | {"cert_ids": ["bb", "aa"]}))    # rotated key: one in common
        self.assertIsNone(check(report | {"cert_ids": []}, dev, same | {"cert_ids": ["bb"]}))   # v1-only APK
        self.assertIsNone(check(report | {"type": "aab"}, dev, same | {"cert_ids": ["bb"]}))    # signed later
        for api, checked in ((27, False), (28, True), (34, True)):                   # checked from Android 9
            other = check(report, Device("B", "android", "physical", "ready", api=api), same | {"cert_ids": ["bb"]})
            self.assertEqual(other is not None, checked, api)
        self.assertEqual(check(report, dev, same | {"version_code": 8}),
                         "installed app has a newer versionCode (8 > 7); use --clean")
        self.assertIsNone(check(report, dev, same | {"version_code": 6}))
        self.assertIn("newer versionCode", check(report | {"type": "aab"}, dev, same | {"version_code": 8}))
        self.assertEqual(check(report, dev, same | {"free_kb": 4096}),
                         "not enough storage on the device (4 MB free, build is 5 MB)")
        self.assertIsNone(check(report, dev, same | {"free_kb": 5 * 1024}))          # exactly enough for the file
        # --clean removes the installed copy first: none of the three applies.
        for state in (same | {"cert_ids": ["bb"]}, same | {"version_code": 8}, same | {"free_kb": 1}):
            self.assertIsNone(check(report, dev, state, clean=True))


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

    def state_rules(self, serial, dumpsys, free_kb=900000):
        return [{"match": ["-s", serial, "dumpsys", "package"], "stdout": dumpsys},
                {"match": ["-s", serial, "dumpsys", "diskstats"],
                 "stdout": f"Latency: 1ms [512B Data Write]\nData-Free: {free_kb}K / 10167132K total = 8% free\n"}]

    def test_refusals_are_predicted_before_anything_is_sent(self):
        b.apk(self.apk, b.axml(b.manifest(min_sdk=21, version_code=7)), v2=True, certs=(CERT,))
        mine = artifact.android_cert_id(CERT)
        self.adb(devices=[("AAA111", 35, "15", "Pixel 9"), ("BBB222", 35, "15", "Pixel 9a"),
                          ("CCC333", 35, "15", "Pixel 8"), ("DDD444", 35, "15", "Pixel 7")],
                 extra=self.state_rules("AAA111", DUMPSYS_35.format(code=7, ids="deadbeef", past=""))
                 + self.state_rules("BBB222", DUMPSYS_35.format(code=9, ids=mine, past=""))
                 + self.state_rules("CCC333", DUMPSYS_35.format(code=7, ids=mine, past=""), free_kb=0)
                 + self.state_rules("DDD444", DUMPSYS_35.format(code=6, ids=mine, past="")))
        r = devdrop(self.fake, "install", self.apk, "--json")
        self.assertEqual(r.returncode, 1, r.stderr)
        recs = {x["id"]: x for x in json.loads(r.stdout)["results"]}
        self.assertEqual({k: (v["result"], v["stage"]) for k, v in recs.items()},
                         {"AAA111": ("skipped", "preflight"), "BBB222": ("skipped", "preflight"),
                          "CCC333": ("skipped", "preflight"), "DDD444": ("ok", "install")})
        self.assertIn("different key; use --clean", recs["AAA111"]["reason"])
        self.assertIn("newer versionCode (9 > 7); use --clean", recs["BBB222"]["reason"])
        self.assertIn("not enough storage", recs["CCC333"]["reason"])
        self.assertEqual(self.installs(), ["DDD444"])

    def test_clean_installs_over_a_different_key_and_a_newer_version(self):
        b.apk(self.apk, b.axml(b.manifest(min_sdk=21, version_code=7)), v2=True, certs=(CERT,))
        self.adb(devices=[("AAA111", 35, "15", "Pixel 9")],
                 extra=self.state_rules("AAA111", DUMPSYS_35.format(code=9, ids="deadbeef", past="")))
        r = devdrop(self.fake, "install", self.apk, "--clean", "--json")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self.installs(), ["AAA111"])

    def test_a_device_that_cannot_be_asked_still_gets_the_install(self):
        self.adb(extra=[{"match": ["dumpsys"], "rc": 1, "stderr": "error: closed\n"}])
        r = devdrop(self.fake, "install", self.apk, "--to", "AAA111", "--json")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self.installs(), ["AAA111"])

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
