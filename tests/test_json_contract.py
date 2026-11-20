# SPDX-License-Identifier: Apache-2.0
"""docs/json.md lists every --json field: no more and no fewer than the code produces."""
import dataclasses
import json
import os
import re
import unittest

from devdrop import artifact, deploy
from devdrop.device import Device
from tests import builders as b
from tests.fakes import FakeTools
from tests.test_devices import devdrop
from tests.test_install import adb_rules
from tests.test_ios_install import IPHONE, xcrun_rules
from tests.test_screenshot import IMAGE, shot_rules

PAGE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "docs", "json.md")
OPTIONAL = "Result record: fields present only in some records"


def documented():
    """{heading: [field, ...]} from the tables of docs/json.md (first column, in backticks)."""
    tables, heading = {}, None
    with open(PAGE, encoding="utf-8") as f:
        for line in f:
            if line.startswith("### "):
                heading = line[4:].strip()
            m = re.match(r"\| `(\w+)` \|", line)
            if m and heading:
                tables.setdefault(heading, []).append(m.group(1))
    return tables


class JsonContract(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.doc = documented()

    def same(self, heading, actual):
        listed = self.doc[heading]
        self.assertEqual(len(listed), len(set(listed)), f"{heading}: a field is listed twice")
        self.assertEqual(sorted(listed), sorted(actual), f"docs/json.md, table '{heading}'")

    def test_device_and_backend(self):
        fake = FakeTools()
        self.addCleanup(fake.close)
        fake.add("adb", adb_rules())
        out = json.loads(devdrop(fake, "devices", "--json").stdout)
        self.assertEqual(sorted(out), ["backends", "devices"])
        self.same("Device", out["devices"][0])
        self.same("Device", [f.name for f in dataclasses.fields(Device)])
        self.same("Backend", out["backends"]["android"])

    def test_inspect(self):
        fake = FakeTools()
        self.addCleanup(fake.close)
        apk = os.path.join(fake.root, "a.apk")
        b.apk(apk, b.axml(b.manifest()), libs=("arm64-v8a",), v2=True)
        report = artifact.inspect(apk)
        self.same("Android build", report)
        self.same("16 KB readiness", report["page_16k"])
        self.same("Native library", report["page_16k"]["libraries"][0])
        aab = os.path.join(fake.root, "a.aab")
        b.aab(aab, b.manifest(), libs=(("arm64-v8a/libnative.so", b"\x7fELF"),))
        bundle = artifact.inspect(aab)
        self.same("Android build", bundle)
        self.same("Native library", bundle["page_16k"]["libraries"][0])
        ipa = os.path.join(fake.root, "a.ipa")
        b.ipa(ipa, b.info_plist(), b.mobileprovision(devices=[IPHONE]))
        report = artifact.inspect(ipa)
        self.same("iOS build", report)
        self.same("Provisioning profile", report["profile"])

    def test_result_records(self):
        fake = FakeTools()
        self.addCleanup(fake.close)
        apk = os.path.join(fake.root, "a.apk")
        b.apk(apk, b.axml(b.manifest(min_sdk=21)), v2=True)
        fake.add("adb", adb_rules(extra=shot_rules({"AAA111": {"write_after": deploy.DEVICE_SHOT,
                                                               "file_hex": IMAGE}})
                                  + [{"match": ["pidof"], "stdout": "4242\n"}, {"match": ["logcat"]}]))
        fake.add("xcrun", xcrun_rules())
        ipa = os.path.join(fake.root, "a.ipa")
        b.ipa(ipa, b.info_plist(), b.mobileprovision(devices=[IPHONE]))
        logs, shots = os.path.join(fake.root, "logs"), os.path.join(fake.root, "shots")
        runs = [("install", apk, ipa, "--launch", "--logs", logs, "--log-seconds", "1",
                 "--screenshot", shots, "--screenshot-seconds", "1"),
                ("install", apk, "--dry-run"), ("launch", "com.example.app", "--to", "AAA111"),
                ("uninstall", "com.example.app", "--to", "AAA111"), ("shutdown",)]
        always, sometimes = set(self.doc["Result record"]), set(self.doc[OPTIONAL])
        self.assertFalse(always & sometimes)
        seen = set()
        for run in runs:
            r = devdrop(fake, *run, "--json")
            out = json.loads(r.stdout) if r.stdout else {}
            if run[0] == "shutdown" and not out:
                continue                    # no running emulator in the stand-in lab: nothing to report
            self.assertEqual(sorted(out), ["failed", "ok", "results", "skipped"], r.stderr)
            for rec in out["results"]:
                with self.subTest(run=run[0], device=rec["id"]):
                    self.assertEqual(always - set(rec), set(), "a documented field is missing")
                    self.assertEqual(set(rec) - always - sometimes, set(), "a field is not in docs/json.md")
                seen |= set(rec) & sometimes
        self.assertEqual(seen, sometimes, "docs/json.md lists an optional field no command produced")

    def test_results_and_stages_named_on_the_page(self):
        with open(PAGE, encoding="utf-8") as f:
            text = f.read()
        dev = Device("A", "android", "physical", "ready")
        self.same("Result record", deploy.record(dev, "ok", "install", "", 0.0))
        for word in ("ok", "skipped", "failed", "preflight", "build-apks", "install", "uninstall", "launch",
                     "running", "boot", "shutdown", "tool"):
            self.assertIn(f"`{word}`", text)


if __name__ == "__main__":
    unittest.main()
