# SPDX-License-Identifier: Apache-2.0
"""--junit: one testcase per device, readable by CI dashboards."""

import os
import tempfile
import unittest
import xml.etree.ElementTree as ET

from devdrop import junit
from tests import builders as b
from tests.fakes import FakeTools
from tests.test_devices import devdrop
from tests.test_install import adb_rules


class Report(unittest.TestCase):
    def test_acceptance_ok_skipped_failed(self):
        fake = FakeTools()
        self.addCleanup(fake.close)
        fake.add("adb", adb_rules(install_out={"BBB222": "Failure [INSTALL_FAILED_INSUFFICIENT_STORAGE]\n"}))
        apk = os.path.join(fake.root, "a.apk")
        b.apk(apk, b.axml(b.manifest(min_sdk=26)), v2=True)
        report = os.path.join(fake.root, "out", "report.xml")
        os.makedirs(os.path.dirname(report))
        r = devdrop(fake, "install", apk, "--junit", report)
        self.assertEqual(r.returncode, 1)
        self.assertIn("1 ok, 1 skipped, 1 failed", r.stdout)            # normal output unchanged
        suite = ET.parse(report).getroot().find("testsuite")
        self.assertEqual({k: suite.get(k) for k in ("name", "tests", "failures", "skipped", "errors")},
                         {"name": "devdrop install", "tests": "3", "failures": "1", "skipped": "1", "errors": "0"})
        cases = {c.get("name"): c for c in suite.findall("testcase")}
        self.assertEqual(set(cases), {"AAA111", "BBB222", "CCC333"})
        self.assertEqual(cases["AAA111"].get("classname"), "android.physical")
        self.assertIsNone(cases["AAA111"].find("failure"))
        failure = cases["BBB222"].find("failure")
        self.assertEqual(failure.get("type"), "install")
        self.assertIn("not enough storage", failure.get("message"))
        self.assertEqual(cases["CCC333"].find("skipped").get("message"), "preflight: API 23 < minSdk 26")
        self.assertIn("device: CCC333", cases["CCC333"].find("system-out").text)

    def test_names_warnings_logs_and_escaping(self):
        records = [{"id": "U1", "name": "Lab <iPhone> & co", "platform": "ios", "kind": "simulator",
                    "result": "failed", "stage": "running", "reason": 'app exited: Fatal error: "x" < y',
                    "seconds": 5.04, "warnings": ["w1"], "log": "/tmp/U1.log", "os_version": "18.5"}]
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "r.xml")
            junit.write(path, "launch", records)
            case = ET.parse(path).getroot().find("testsuite/testcase")
        self.assertEqual((case.get("name"), case.get("classname"), case.get("time")),
                         ("Lab <iPhone> & co", "ios.simulator", "5.0"))
        self.assertEqual(case.find("failure").get("message"), 'running: app exited: Fatal error: "x" < y')
        out = case.find("system-out").text
        for line in ("warning: w1", "log: /tmp/U1.log", "os_version: 18.5"):
            self.assertIn(line, out)

    def test_every_device_command_accepts_junit_and_bad_paths_do_not_crash(self):
        fake = FakeTools()
        self.addCleanup(fake.close)
        fake.add("adb", adb_rules())
        for args in (["launch", "com.example.app"], ["uninstall", "com.example.app"]):
            with self.subTest(args=args):
                path = os.path.join(fake.root, args[0] + ".xml")
                devdrop(fake, *args, "--to", "AAA111", "--junit", path)
                self.assertEqual(ET.parse(path).getroot().find("testsuite").get("name"), f"devdrop {args[0]}")
        r = devdrop(fake, "launch", "com.example.app", "--to", "AAA111", "--junit",
                    os.path.join(fake.root, "missing-dir", "r.xml"))
        self.assertEqual(r.returncode, 0)
        self.assertIn("--junit", r.stderr)
        self.assertNotIn("Traceback", r.stderr)


if __name__ == "__main__":
    unittest.main()
