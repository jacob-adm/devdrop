# SPDX-License-Identifier: Apache-2.0
"""--url: opening a link in the app instead of its start screen."""

import json
import os
import unittest

from tests import builders as b
from tests.fakes import FakeTools
from tests.test_devices import devdrop
from tests.test_install import adb_rules

URL = "example://open?x=1&y=2"
# Real `am start` output (API 35 emulator) for a link no activity of the app opens.
UNRESOLVED = ("Starting: Intent { act=android.intent.action.VIEW dat=nobody://open pkg=com.example.app }\n"
              "Error: Activity not started, unable to resolve Intent { act=android.intent.action.VIEW "
              "dat=nobody://open flg=0x10000000 pkg=com.example.app }\n")


class AndroidUrl(unittest.TestCase):
    def setUp(self):
        self.fake = FakeTools()
        self.addCleanup(self.fake.close)

    def test_link_is_opened_in_the_app_and_quoted_for_the_device_shell(self):
        self.fake.add("adb", adb_rules(extra=[
            {"match": ["-s", "BBB222", "android.intent.action.VIEW"], "stdout": UNRESOLVED},
            {"match": ["android.intent.action.VIEW"], "stdout": "Starting: Intent { ... }\n"}]))
        r = devdrop(self.fake, "launch", "com.example.app", "--to", "AAA111,BBB222", "--url", URL, "--json")
        recs = {x["id"]: x for x in json.loads(r.stdout)["results"]}
        self.assertEqual((recs["AAA111"]["result"], recs["AAA111"]["stage"]), ("ok", "launch"))
        self.assertEqual(recs["BBB222"]["reason"], "the app has no activity that opens this URL (is it installed?)")
        start = next(c for c in self.fake.calls("adb") if c[1] == "AAA111" and "am" in c)
        # one quoted word for the device shell, limited to the package, and no launcher lookup
        self.assertEqual(start[2:], ["shell", "am", "start", "-a", "android.intent.action.VIEW",
                                     "-d", "'example://open?x=1&y=2'", "com.example.app"])
        self.assertFalse([c for c in self.fake.calls("adb") if "resolve-activity" in c])

    def test_install_launches_at_the_link_and_url_needs_launch(self):
        apk = os.path.join(self.fake.root, "a.apk")
        b.apk(apk, b.axml(b.manifest(min_sdk=21)), v2=True)
        self.fake.add("adb", adb_rules(extra=[
            {"match": ["android.intent.action.VIEW"], "stdout": "Starting: Intent { ... }\n"}]))
        r = devdrop(self.fake, "install", apk, "--to", "AAA111", "--launch", "--url", URL, "--json")
        self.assertEqual(json.loads(r.stdout)["ok"], 1, r.stdout)
        self.assertTrue([c for c in self.fake.calls("adb") if "android.intent.action.VIEW" in c])
        r = devdrop(self.fake, "install", apk, "--url", URL)
        self.assertEqual((r.returncode, "--url needs --launch" in r.stderr), (2, True))


class IosUrl(unittest.TestCase):
    def test_simulator_opens_the_link_after_launch_and_a_phone_gets_it_with_the_launch(self):
        from tests.test_ios_install import IPHONE, xcrun_rules
        fake = FakeTools()
        self.addCleanup(fake.close)
        fake.add("xcrun", xcrun_rules(extra=[{"match": ["simctl", "openurl"]}]))
        r = devdrop(fake, "launch", "com.example.app", "--to", "sim," + IPHONE, "--url", URL, "--json")
        self.assertEqual(json.loads(r.stdout)["ok"], 2, r.stdout)
        calls = fake.calls("xcrun")
        sim = [c[:2] for c in calls if c[:2] in (["simctl", "launch"], ["simctl", "openurl"])]
        self.assertEqual(sim, [["simctl", "launch"], ["simctl", "openurl"]])
        self.assertEqual(next(c for c in calls if "openurl" in c)[-1], URL)
        phone = next(c for c in calls if "process" in c and "launch" in c)
        self.assertEqual(phone[phone.index("--payload-url") + 1], URL)

    def test_a_link_no_app_claims_fails_the_launch(self):
        from tests.test_ios_install import xcrun_rules
        fake = FakeTools()
        self.addCleanup(fake.close)
        fake.add("xcrun", xcrun_rules(extra=[{"match": ["simctl", "openurl"], "rc": 115, "stderr":
                 "An error was encountered processing the command (domain=NSOSStatusErrorDomain, code=-10814):\n"
                 "The operation couldn’t be completed. (OSStatus error -10814.)\n"}]))
        r = devdrop(fake, "launch", "com.example.app", "--to", "sim", "--url", "nobody://open", "--json")
        rec = json.loads(r.stdout)["results"][0]
        self.assertEqual((r.returncode, rec["result"], rec["stage"]), (1, "failed", "launch"))


if __name__ == "__main__":
    unittest.main()
