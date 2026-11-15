# SPDX-License-Identifier: Apache-2.0
import copy
import json
import os
import stat
import tempfile
import unittest
import zipfile

from devdrop import deploy, preflight
from devdrop.device import Device
from tests import builders as b
from tests.fakes import FakeTools, fixture
from tests.test_devices import devdrop

IPHONE, IPAD, SIM = "00008150-0000000000000001", "00008150-0000000000000003", "AAAAAAAA-0000-0000-0000-000000000006"


def devicectl_doc():
    doc = json.loads(fixture("devicectl_list.json"))
    iphone = doc["result"]["devices"][0]                     # ready, wired
    ipad = copy.deepcopy(doc["result"]["devices"][2])        # iPad Air on the network
    ipad["deviceProperties"]["developerModeStatus"] = "enabled"
    doc["result"]["devices"] = [iphone, ipad]
    return json.dumps(doc)


def xcrun_rules(extra=()):
    return list(extra) + [
        {"match": ["--find", "devicectl"], "stdout": "/x/devicectl\n"},
        {"match": ["devicectl", "list", "devices"], "write_after": "--json-output", "file": devicectl_doc()},
        {"match": ["simctl", "list"], "stdout": fixture("simctl_list.json")},
        {"match": ["devicectl", "device", "install", "app"], "stdout": "App installed:\n"},
        {"match": ["devicectl", "device", "process", "launch"], "stdout": "Launched application\n"},
        {"match": ["devicectl", "device", "uninstall", "app"], "stdout": "App uninstalled.\n"},
        {"match": ["simctl", "install"]},
        {"match": ["simctl", "launch"], "stdout": "com.example.app: 4242\n"},
        {"match": ["simctl", "get_app_container"], "rc": 2,
         "stderr": "An error was encountered processing the command (domain=NSPOSIXErrorDomain, code=2):\n"},
        {"match": ["simctl", "uninstall"]},
    ]


class IosPreflight(unittest.TestCase):
    PROFILE = {"kind": "development", "expired": False, "expires": "2030-01-01T00:00:00Z",
               "all_devices": False, "devices": [IPHONE]}

    def report(self, **kw):
        r = {"platform": "ios", "type": "ipa", "target": "device", "min_os": "17.0",
             "families": ["iphone", "ipad"], "profile": dict(self.PROFILE)}
        r.update(kw)
        return r

    def dev(self, **kw):
        d = dict(id=IPHONE, platform="ios", kind="physical", state="ready", model="iPhone 17 Pro",
                 os_version="26.1")
        d.update(kw)
        return Device(**d)

    def test_reasons(self):
        cases = [
            (self.report(), self.dev(), None),
            (self.report(min_os="26.2"), self.dev(), "iOS 26.1 < minimum 26.2"),
            (self.report(min_os="26"), self.dev(), None),
            (self.report(target="simulator"), self.dev(), "simulator build -> physical device"),
            (self.report(), self.dev(kind="simulator"), "device build -> simulator"),
            (self.report(families=["ipad"]), self.dev(), "iPad-only app -> iPhone"),
            (self.report(families=["ipad"]), self.dev(model="iPad Air"), None),
            (self.report(profile=None), self.dev(), "no provisioning profile (build is not signed for devices)"),
            (self.report(profile=dict(self.PROFILE, expired=True, expires="2026-10-02T00:00:00Z")),
             self.dev(), "profile expired 2026-10-02"),
            (self.report(profile=dict(self.PROFILE, kind="app-store", devices=[])), self.dev(),
             "App Store profile cannot install on devices"),
            (self.report(), self.dev(id=IPAD), "UDID not in provisioning profile"),
            (self.report(profile=dict(self.PROFILE, all_devices=True, devices=[])), self.dev(id=IPAD), None),
            (self.report(target="simulator", profile=None), self.dev(kind="simulator"), None),
            (self.report(), self.dev(state="developer-mode-off"), "device is developer-mode-off"),
        ]
        for report, dev, want in cases:
            with self.subTest(want=want):
                self.assertEqual(preflight.check(report, dev), want)


class Unpack(unittest.TestCase):
    def test_keeps_modes_and_ignores_traversal(self):
        with tempfile.TemporaryDirectory() as tmp:
            ipa = os.path.join(tmp, "a.ipa")
            with zipfile.ZipFile(ipa, "w") as z:
                z.writestr("Payload/Example.app/Info.plist", b.info_plist())
                exe = zipfile.ZipInfo("Payload/Example.app/Example")
                exe.external_attr = (stat.S_IFREG | 0o755) << 16
                z.writestr(exe, b"\xcf\xfa\xed\xfe")
                z.writestr("Payload/Example.app/../../evil", b"x")
            out = os.path.join(tmp, "out")
            os.mkdir(out)
            app = deploy.unpack_ipa(ipa, out)
            self.assertEqual(app, os.path.join(out, "Example.app"))
            self.assertTrue(os.path.isfile(os.path.join(app, "Info.plist")))
            if os.name != "nt":
                self.assertTrue(os.stat(os.path.join(app, "Example")).st_mode & stat.S_IXUSR)
            self.assertFalse(os.path.exists(os.path.join(tmp, "evil")))


class FailureText(unittest.TestCase):
    def test_devicectl_errors(self):
        text = ("ERROR: Failed to launch the app. (com.apple.dt.CoreDeviceError error 10002)\n"
                "       NSLocalizedRecoverySuggestion = The device was not, or could not be, unlocked.\n")
        self.assertEqual(deploy.ios_failure(text),
                         "Failed to launch the app. (com.apple.dt.CoreDeviceError error 10002) "
                         "(unlock the device and retry)")


    def test_untrusted_developer(self):
        # devicectl's output on an iPhone that has not yet trusted a new development team.
        text = (
            "ERROR: The application failed to launch. (com.apple.dt.CoreDeviceError error 10002 (0x2712))\n"
            "       BundleIdentifier = com.example.app\n"
            "           The request to open \"com.example.app\" failed. "
            "(FBSOpenApplicationServiceErrorDomain error 1 (0x01))\n"
            "           NSLocalizedFailureReason = The request was denied by service delegate (SBMainWorkspace) "
            "for reason: Security (\"Unable to launch com.example.app because it has an invalid code "
            "signature, inadequate entitlements or its profile has not been explicitly trusted by the user\").\n")
        self.assertEqual(deploy.ios_failure(text),
                         "The application failed to launch. (com.apple.dt.CoreDeviceError error 10002 (0x2712)) "
                         "(trust the developer on the device: Settings > General > VPN & Device Management)")

class IosInstallCommand(unittest.TestCase):
    def setUp(self):
        self.fake = FakeTools()
        self.addCleanup(self.fake.close)
        self.ipa = os.path.join(self.fake.root, "app.ipa")
        b.ipa(self.ipa, b.info_plist(), b.mobileprovision(devices=[IPHONE]))

    def test_acceptance_iphone_installs_ipad_and_simulator_skipped(self):
        self.fake.add("xcrun", xcrun_rules())
        r = devdrop(self.fake, "install", self.ipa, "--launch", "--json")
        self.assertEqual(r.returncode, 1, r.stderr)
        recs = {x["id"]: x for x in json.loads(r.stdout)["results"]}
        self.assertEqual((recs[IPHONE]["result"], recs[IPHONE]["stage"]), ("ok", "launch"))
        self.assertEqual(recs[IPAD]["reason"], "UDID not in provisioning profile")
        self.assertEqual(recs[SIM]["reason"], "device build -> simulator")
        installs = [c for c in self.fake.calls("xcrun") if c[:3] == ["devicectl", "device", "install"]]
        self.assertEqual(len(installs), 1)
        self.assertEqual(installs[0][-1].rsplit(os.sep, 1)[-1], "Example.app")  # unpacked bundle
        self.assertEqual(installs[0][installs[0].index("--device") + 1], IPHONE)

    def test_install_error_is_explained(self):
        self.fake.add("xcrun", xcrun_rules(extra=[
            {"match": ["devicectl", "device", "install"], "rc": 1,
             "stderr": "ERROR: The device is locked. (com.apple.dt.CoreDeviceError error 4)\n"}]))
        r = devdrop(self.fake, "install", self.ipa, "--to", IPHONE, "--json")
        rec = json.loads(r.stdout)["results"][0]
        self.assertEqual((rec["result"], rec["stage"]), ("failed", "install"))
        self.assertIn("unlock the device", rec["reason"])

    def test_simulator_app_install_launch_and_uninstall(self):
        self.fake.add("xcrun", xcrun_rules())
        app = os.path.join(self.fake.root, "Example.app")
        os.mkdir(app)
        with open(os.path.join(app, "Info.plist"), "wb") as f:
            f.write(b.info_plist(simulator=True))
        r = devdrop(self.fake, "install", app, "--to", "sim", "--launch", "--clean")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        verbs = [c[1] for c in self.fake.calls("xcrun") if c[0] == "simctl" and c[1] != "list"]
        self.assertEqual(verbs, ["get_app_container", "install", "launch"])  # clean: nothing to remove
        r = devdrop(self.fake, "uninstall", "com.example.app", "--to", "sim", "--json")
        self.assertEqual(json.loads(r.stdout)["results"][0]["reason"], "not installed")

    def test_launch_and_uninstall_span_both_platforms(self):
        self.fake.add("xcrun", xcrun_rules())
        r = devdrop(self.fake, "launch", "com.example.app", "--to", "physical", "--json")
        out = json.loads(r.stdout)
        self.assertEqual({x["id"] for x in out["results"]}, {IPHONE, IPAD})
        self.assertEqual(out["ok"], 2)


if __name__ == "__main__":
    unittest.main()
