# SPDX-License-Identifier: Apache-2.0
"""`install` with an Android and an iOS build in one run."""
import json
import os
import unittest
import xml.etree.ElementTree as ET

from tests import builders as b
from tests.fakes import FakeTools
from tests.test_devices import devdrop
from tests.test_install import adb_rules
from tests.test_ios_install import IPAD, IPHONE, SIM, xcrun_rules

PHONES = [("AAA111", 34, "14", "Pixel 8"), ("CCC333", 23, "6.0", "Nexus 5")]


class SeveralBuilds(unittest.TestCase):
    def setUp(self):
        self.fake = FakeTools()
        self.addCleanup(self.fake.close)
        self.apk = os.path.join(self.fake.root, "app.apk")
        b.apk(self.apk, b.axml(b.manifest(min_sdk=26)), v2=True)
        self.ipa = os.path.join(self.fake.root, "app.ipa")
        b.ipa(self.ipa, b.info_plist(), b.mobileprovision(devices=[IPHONE]))

    def tools(self, adb=True, xcrun=True):
        if adb:
            self.fake.add("adb", adb_rules(devices=PHONES))
        if xcrun:
            self.fake.add("xcrun", xcrun_rules())

    def test_each_build_goes_to_its_platform_in_one_report(self):
        self.tools()
        junit = os.path.join(self.fake.root, "r.xml")
        r = devdrop(self.fake, "install", self.apk, self.ipa, "--json", "--junit", junit)
        self.assertEqual(r.returncode, 1, r.stderr)             # skips on both platforms
        out = json.loads(r.stdout)
        recs = {x["id"]: x for x in out["results"]}
        self.assertEqual({k: (v["result"], os.path.basename(v["artifact"])) for k, v in recs.items()},
                         {"AAA111": ("ok", "app.apk"), "CCC333": ("skipped", "app.apk"),
                          IPHONE: ("ok", "app.ipa"), IPAD: ("skipped", "app.ipa"), SIM: ("skipped", "app.ipa")})
        self.assertEqual((out["ok"], out["skipped"], out["failed"]), (2, 3, 0))
        self.assertEqual([c[1] for c in self.fake.calls("adb") if "install" in c], ["AAA111"])
        sent = [c for c in self.fake.calls("xcrun") if "install" in c]
        self.assertEqual(len(sent), 1)
        self.assertIn(IPHONE, sent[0])
        self.assertEqual(len(ET.parse(junit).getroot().findall(".//testcase")), 5)

    def test_all_ok_exits_0_and_text_names_each_build(self):
        self.tools()
        r = devdrop(self.fake, "install", self.ipa, self.apk, "--to", f"AAA111,{IPHONE}")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        lines = r.stdout.splitlines()
        self.assertTrue(any("AAA111" in ln and "[app.apk]" in ln for ln in lines), r.stdout)
        self.assertTrue(any(IPHONE in ln and "[app.ipa]" in ln for ln in lines), r.stdout)
        self.assertIn("2 ok, 0 skipped, 0 failed", r.stdout)

    def test_one_build_prints_no_build_name_but_json_has_it(self):
        self.tools()
        r = devdrop(self.fake, "install", self.apk, "--to", "AAA111")
        self.assertNotIn("[app.apk]", r.stdout)
        r = devdrop(self.fake, "install", self.apk, "--to", "AAA111", "--json")
        self.assertEqual(json.loads(r.stdout)["results"][0]["artifact"], self.apk)

    def test_two_builds_for_one_platform_is_a_usage_error(self):
        self.tools()
        other = os.path.join(self.fake.root, "other.apk")
        b.apk(other, b.axml(b.manifest()), v2=True)
        r = devdrop(self.fake, "install", self.apk, other)
        self.assertEqual(r.returncode, 2)
        self.assertIn("are both android builds; give one build per platform", r.stderr)
        self.assertEqual(self.fake.calls("adb"), [])

    def test_a_missing_platform_tool_stops_the_run_before_anything_is_sent(self):
        self.tools(xcrun=False)
        r = devdrop(self.fake, "install", self.apk, self.ipa, "--json")
        self.assertEqual(r.returncode, 4, r.stdout + r.stderr)
        self.assertIn("app.ipa: xcrun (macOS with Xcode) not found", r.stderr)
        self.assertEqual([c for c in self.fake.calls("adb") if "install" in c], [])

    def test_a_build_no_device_matched_fails_the_run(self):
        self.tools()
        r = devdrop(self.fake, "install", self.apk, self.ipa, "--to", "AAA111", "--json")
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("app.ipa: no ios device matches --to AAA111", r.stderr)
        out = json.loads(r.stdout)
        self.assertEqual([(x["id"], x["result"]) for x in out["results"]], [("AAA111", "ok")])

    def test_dry_run_covers_both_builds(self):
        self.tools()
        r = devdrop(self.fake, "install", self.apk, self.ipa, "--dry-run", "--to", f"AAA111,{IPHONE}", "--json")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual([x["dry_run"] for x in json.loads(r.stdout)["results"]], [True, True])
        self.assertEqual([c for c in self.fake.calls("adb") + self.fake.calls("xcrun") if "install" in c], [])


if __name__ == "__main__":
    unittest.main()
