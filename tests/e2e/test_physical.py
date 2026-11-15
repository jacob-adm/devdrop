# SPDX-License-Identifier: Apache-2.0
"""End-to-end on a real iPhone: a development-signed build, installed, launched and removed.

Opt-in, because it needs an attached iPhone (unlocked, Developer Mode on, the
development team trusted under Settings > General > VPN & Device Management)
and a build signed for it:

    DEVDROP_E2E=1 DEVDROP_E2E_IPA=/path/to/app.ipa DEVDROP_E2E_ANCHOR=com.example.anchor \
        python3 -m unittest tests.e2e.test_physical -v

DEVDROP_E2E_IPA is an .ipa signed with a development profile that lists the
attached iPhone. Xcode makes one with automatic signing, for example
`xcodebuild -allowProvisioningUpdates -allowProvisioningDeviceRegistration`
with the phone as destination, then zipping the .app under Payload/.

DEVDROP_E2E_ANCHOR is the bundle id of another app from the same team that
stays installed. iOS withdraws the trust in a development team when its last
app is removed, so without one the uninstall test would undo the trust and
every later launch would be refused until it is granted again.

Every result devdrop reports is checked against the phone with devicectl.
The app is uninstalled at the end.
"""

import json
import os
import subprocess
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
IPA = os.environ.get("DEVDROP_E2E_IPA")
ANCHOR = os.environ.get("DEVDROP_E2E_ANCHOR")
ENABLED = os.environ.get("DEVDROP_E2E") == "1" and bool(IPA) and sys.platform == "darwin"

S = {}


def devdrop(*args):
    r = subprocess.run([sys.executable, "-m", "devdrop", *args], capture_output=True, text=True,
                       timeout=600, cwd=REPO)
    out = json.loads(r.stdout) if "--json" in args else None
    return r.returncode, out, r


def installed_version(device, bundle_id):
    """The app's version on the phone according to devicectl, or None when it is not installed."""
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "apps.json")
        subprocess.run(["xcrun", "devicectl", "device", "info", "apps", "--device", device,
                        "--bundle-id", bundle_id, "--json-output", path],
                       capture_output=True, text=True, timeout=120, check=True)
        with open(path, encoding="utf-8") as f:
            apps = json.load(f)["result"]["apps"]
    return next((a.get("version") for a in apps if a.get("bundleIdentifier") == bundle_id), None)


def setUpModule():
    if not ENABLED:
        raise unittest.SkipTest("set DEVDROP_E2E=1 and DEVDROP_E2E_IPA to a signed .ipa (macOS only)")
    if not ANCHOR:
        raise RuntimeError("set DEVDROP_E2E_ANCHOR to another installed app from the same team; "
                           "removing the team's last app withdraws the trust in it")
    rc, out, r = devdrop("devices", "--json")
    phones = [d for d in out["devices"]
              if (d["platform"], d["kind"], d["state"]) == ("ios", "physical", "ready")]
    if not phones:
        raise RuntimeError(f"no ready iPhone attached: {r.stdout}")
    rc, report, r = devdrop("inspect", IPA, "--json")
    if rc != 0:
        raise RuntimeError(f"inspect {IPA} failed: {r.stderr}")
    S["phones"] = [d["id"] for d in phones]
    for phone in S["phones"]:
        if installed_version(phone, ANCHOR) is None:
            raise RuntimeError(f"{ANCHOR} is not installed on {phone}")
    S["report"] = report
    S["to"] = ",".join(S["phones"])


def tearDownModule():
    if S.get("report"):
        devdrop("uninstall", S["report"]["id"], "--to", S["to"], "--json")


class A_Profile(unittest.TestCase):
    def test_inspect_reads_the_real_development_profile(self):
        report = S["report"]
        self.assertEqual((report["type"], report["platform"], report["target"]), ("ipa", "ios", "device"))
        profile = report["profile"]
        self.assertEqual(profile["kind"], "development")
        self.assertFalse(profile["expired"], profile["expires"])
        self.assertRegex(profile["team"], r"^[A-Z0-9]{10}$")
        for phone in S["phones"]:
            self.assertIn(phone, profile["devices"])


class B_Lifecycle(unittest.TestCase):
    def test_1_install_and_launch_with_log_and_junit(self):
        bundle_id = S["report"]["id"]
        devdrop("uninstall", bundle_id, "--to", S["to"], "--json")
        with tempfile.TemporaryDirectory() as tmp:
            junit = os.path.join(tmp, "devdrop.xml")
            rc, out, r = devdrop("install", IPA, "--to", "physical,ios", "--launch",
                                 "--logs", os.path.join(tmp, "logs"), "--junit", junit, "--json")
            self.assertEqual(rc, 0, r.stdout + r.stderr)
            self.assertEqual({rec["id"] for rec in out["results"]}, set(S["phones"]))
            for rec in out["results"]:
                self.assertEqual((rec["result"], rec["manufacturer"]), ("ok", "Apple"), rec)
            suite = ET.parse(junit).getroot().find("testsuite")
            self.assertEqual((suite.get("tests"), suite.get("failures")), (str(len(S["phones"])), "0"))
        for phone in S["phones"]:
            self.assertEqual(installed_version(phone, bundle_id), S["report"]["version"])

    def test_2_launch_uninstall_twice(self):
        bundle_id = S["report"]["id"]
        rc, out, r = devdrop("launch", bundle_id, "--to", S["to"], "--json")
        self.assertEqual(rc, 0, r.stdout + r.stderr)
        rc, out, r = devdrop("uninstall", bundle_id, "--to", S["to"], "--json")
        self.assertEqual(rc, 0, r.stdout + r.stderr)
        for phone in S["phones"]:
            self.assertIsNone(installed_version(phone, bundle_id))
        rc, out, _ = devdrop("uninstall", bundle_id, "--to", S["to"], "--json")
        self.assertEqual({rec["reason"] for rec in out["results"]}, {"not installed"})
        rc, out, _ = devdrop("launch", bundle_id, "--to", S["to"], "--json")
        self.assertEqual(rc, 1)
