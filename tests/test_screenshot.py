# SPDX-License-Identifier: Apache-2.0
"""--screenshot: saving what each device shows after launch."""

import json
import os
import unittest

from devdrop import deploy
from tests import builders as b
from tests.fakes import FakeTools
from tests.test_devices import devdrop
from tests.test_install import adb_rules

# The smallest thing the check accepts: a PNG signature followed by anything.
IMAGE = (deploy.PNG + b"rest of the image").hex()


def shot_rules(pulls):
    """Stand-in adb rules for screencap, pull and rm; pulls maps a serial to its pull rule."""
    rules = [{"match": ["-s", serial, "pull"], **rule} for serial, rule in pulls.items()]
    return rules + [{"match": ["screencap"]}, {"match": ["rm", "-f"]}]


class AndroidScreenshots(unittest.TestCase):
    def setUp(self):
        self.fake = FakeTools()
        self.addCleanup(self.fake.close)
        self.apk = os.path.join(self.fake.root, "a.apk")
        b.apk(self.apk, b.axml(b.manifest(min_sdk=21)), v2=True)
        self.shots = os.path.join(self.fake.root, "shots")

    def test_one_image_per_device_and_warnings_for_the_rest(self):
        self.fake.add("adb", adb_rules(extra=shot_rules({
            "AAA111": {"write_after": deploy.DEVICE_SHOT, "file_hex": IMAGE},
            # Old Android exits 0 and leaves an error text where the image should be.
            "BBB222": {"write_after": deploy.DEVICE_SHOT, "file": "/system/bin/sh: screencap: not found\n"},
            "CCC333": {"rc": 1, "stderr": "adb: error: failed to stat remote object\n"}})))
        junit = os.path.join(self.fake.root, "out.xml")
        r = devdrop(self.fake, "install", self.apk, "--launch", "--screenshot", self.shots,
                    "--screenshot-seconds", "1", "--junit", junit, "--json")
        out = json.loads(r.stdout)
        recs = {x["id"]: x for x in out["results"]}
        self.assertEqual((r.returncode, out["ok"]), (0, 3), r.stdout)        # a missing picture fails nothing
        path = os.path.join(self.shots, "AAA111.png")
        self.assertEqual(recs["AAA111"]["screenshot"], path)
        with open(path, "rb") as f:
            self.assertTrue(f.read().startswith(deploy.PNG))
        self.assertEqual(recs["BBB222"]["warnings"], ["no screenshot: the device returned no image"])
        self.assertEqual(recs["CCC333"]["warnings"], ["no screenshot: adb: error: failed to stat remote object"])
        self.assertNotIn("screenshot", recs["BBB222"])
        self.assertEqual(os.listdir(self.shots), ["AAA111.png"])             # no half-written files left
        # captured on the device, copied, then removed from the device
        steps = [c for c in self.fake.calls("adb") if c[1] == "AAA111" and deploy.DEVICE_SHOT in c]
        self.assertEqual([c[2:4] for c in steps], [["shell", "screencap"], ["pull", deploy.DEVICE_SHOT],
                                                   ["shell", "rm"]])
        with open(junit, encoding="utf-8") as f:
            self.assertIn("screenshot: " + path, f.read())

    def test_text_output_and_launch_by_id(self):
        self.fake.add("adb", adb_rules(extra=shot_rules({
            "AAA111": {"write_after": deploy.DEVICE_SHOT, "file_hex": IMAGE}})))
        r = devdrop(self.fake, "launch", "com.example.app", "--to", "AAA111", "--screenshot", self.shots,
                    "--screenshot-seconds", "1")
        self.assertIn("screenshot: " + os.path.join(self.shots, "AAA111.png"), r.stdout)

    def test_screenshot_needs_launch(self):
        self.fake.add("adb", adb_rules())
        r = devdrop(self.fake, "install", self.apk, "--screenshot", self.shots)
        self.assertEqual((r.returncode, "--screenshot needs --launch" in r.stderr), (2, True))
        self.assertFalse(os.path.exists(self.shots))


class IosScreenshots(unittest.TestCase):
    def test_simulator_gets_an_image_and_a_physical_iphone_a_warning(self):
        from tests.test_ios_install import IPHONE, xcrun_rules
        fake = FakeTools()
        self.addCleanup(fake.close)
        shots = os.path.join(fake.root, "shots")
        fake.add("xcrun", xcrun_rules(extra=[
            {"match": ["simctl", "io"], "write_after": "screenshot", "file_hex": IMAGE}]))
        r = devdrop(fake, "launch", "com.example.app", "--to", "sim," + IPHONE, "--screenshot", shots,
                    "--screenshot-seconds", "1", "--json")
        recs = json.loads(r.stdout)["results"]
        sim = next(x for x in recs if x["kind"] == "simulator")
        phone = next(x for x in recs if x["id"] == IPHONE)
        self.assertEqual(sim["screenshot"], os.path.join(shots, sim["id"] + ".png"))
        self.assertTrue(os.path.isfile(sim["screenshot"]))
        self.assertEqual((phone["result"], phone["warnings"]),
                         ("ok", ["no screenshot: not available on a physical iPhone or iPad"]))


if __name__ == "__main__":
    unittest.main()
