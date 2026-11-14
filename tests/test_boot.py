# SPDX-License-Identifier: Apache-2.0
import json
import os
import unittest

from devdrop import virtual
from tests import builders as b
from tests.fakes import FakeTools, fixture
from tests.test_devices import devdrop

EMU_PROPS = ("[ro.boot.qemu]: [1]\n[ro.boot.qemu.avd_name]: [{name}]\n[ro.build.version.release]: [15]\n"
             "[ro.build.version.sdk]: [35]\n[ro.product.cpu.abilist]: [arm64-v8a]\n"
             "[ro.product.model]: [sdk_gphone64_arm64]\n")
SIM_OFF = "AAAAAAAA-0000-0000-0000-000000000007"   # iPhone 16 Pro Max, shut down in the fixture


class BootFakes:
    """Stand-ins where pixel_api35 is stopped until the emulator stand-in starts it."""

    def __init__(self, fake):
        self.fake = fake
        self.running = os.path.join(fake.root, "pixel_running")
        self.sim_booted = os.path.join(fake.root, "sim_booted")
        sdk = os.path.join(fake.root, "sdk")
        self.env = {"ANDROID_HOME": sdk, "ANDROID_AVD_HOME": os.path.join(fake.root, "avd")}
        os.makedirs(os.path.join(fake.root, "avd", "pixel_api35.avd"))
        with open(os.path.join(fake.root, "avd", "pixel_api35.avd", "config.ini"), "w") as f:
            f.write("image.sysdir.1=system-images/android-35/google_apis/arm64-v8a/\n")
        listing_on = "List of devices attached\nemulator-5554 device product:x model:sdk_gphone64_arm64 transport_id:1\n" \
                     "emulator-5556 device product:x model:sdk_gphone64_arm64 transport_id:2\n"
        listing_off = "List of devices attached\nemulator-5554 device product:x model:sdk_gphone64_arm64 transport_id:1\n"
        fake.add("adb", [
            {"match": ["devices", "-l"], "when_file": self.running, "stdout": listing_on},
            {"match": ["devices", "-l"], "stdout": listing_off},
            {"match": ["devices"], "when_file": self.running, "stdout": listing_on},
            {"match": ["-s", "emulator-5554", "getprop", "sys.boot_completed"], "stdout": "1\n"},
            {"match": ["-s", "emulator-5556", "getprop", "sys.boot_completed"], "stdout": "1\n"},
            {"match": ["-s", "emulator-5554", "getprop"], "stdout": EMU_PROPS.format(name="tablet_api34")},
            {"match": ["-s", "emulator-5556", "getprop"], "stdout": EMU_PROPS.format(name="pixel_api35")},
            {"match": ["-s", "emulator-5556", "emu", "kill"], "remove": self.running, "stdout": "OK: killing emulator, bye bye\n"},
            {"match": ["-s", "emulator-5554", "emu", "kill"], "stdout": "OK: killing emulator, bye bye\n"},
            {"match": ["install"], "stdout": "Success\n"},
            {"match": ["devices"], "stdout": listing_off},
        ], directory=os.path.join(sdk, "platform-tools"))
        fake.add("emulator", [
            {"match": ["-list-avds"], "stdout": "INFO    | Storing crashdata in: /tmp/x\npixel_api35\ntablet_api34\n"},
            {"match": ["-avd", "pixel_api35"], "touch": self.running},
            {"match": ["-avd"], "rc": 1, "stderr": "PANIC: Unknown AVD name\n"},
        ], directory=os.path.join(sdk, "emulator"))
        sims = json.loads(fixture("simctl_list.json"))
        sims_booted = json.loads(fixture("simctl_list.json"))
        for v in sims_booted["devices"].values():
            for s in v:
                if s["udid"] == SIM_OFF:
                    s["state"] = "Booted"
        fake.add("xcrun", [
            {"match": ["--find", "devicectl"], "rc": 1},
            {"match": ["simctl", "list"], "when_file": self.sim_booted, "stdout": json.dumps(sims_booted)},
            {"match": ["simctl", "list"], "stdout": json.dumps(sims)},
            {"match": ["simctl", "boot", SIM_OFF], "touch": self.sim_booted},
            {"match": ["simctl", "bootstatus"], "stdout": "Device already booted, nothing to do.\n"},
            {"match": ["simctl", "shutdown"], "remove": self.sim_booted},
        ])

    def run(self, *args):
        return devdrop(self.fake, *args, env_extra=self.env)


class AvdConfig(unittest.TestCase):
    def test_api_from_config_and_missing_config(self):
        fake = FakeTools()
        self.addCleanup(fake.close)
        BootFakes(fake)
        home = os.path.join(fake.root, "avd")
        self.assertEqual(virtual.avd_api("pixel_api35", home), 35)
        self.assertIsNone(virtual.avd_api("tablet_api34", home))


class BootCommands(unittest.TestCase):
    def setUp(self):
        self.fake = FakeTools()
        self.addCleanup(self.fake.close)
        self.f = BootFakes(self.fake)
        virtual.POLL = 0.05

    def test_devices_all_lists_stopped_avds_but_not_running_ones(self):
        out = json.loads(self.f.run("devices", "--all", "--json").stdout)
        stopped = [d for d in out["devices"] if d["state"] == "shutdown" and d["platform"] == "android"]
        self.assertEqual([(d["id"], d["api"]) for d in stopped], [("pixel_api35", 35)])
        running = [d for d in out["devices"] if d["id"] == "emulator-5554"][0]
        self.assertEqual(running["name"], "tablet_api34")

    def test_acceptance_install_boot_starts_avd_then_installs(self):
        apk = os.path.join(self.fake.root, "app.apk")
        b.apk(apk, b.axml(b.manifest()), v2=True)
        r = self.f.run("install", apk, "--boot", "--to", "pixel*", "--json")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual([x["id"] for x in json.loads(r.stdout)["results"]], ["emulator-5556"])
        start = next(c for c in self.fake.calls("emulator") if "-avd" in c)
        self.assertEqual(start, ["-avd", "pixel_api35", "-no-window", "-no-audio", "-no-boot-anim"])
        self.assertTrue(any(c[:3] == ["-s", "emulator-5556", "install"] for c in self.fake.calls("adb")))

    def test_boot_needs_explicit_to(self):
        for args in (["boot"], ["boot", "--to", "all"]):
            with self.subTest(args=args):
                r = self.f.run(*args)
                self.assertEqual(r.returncode, 2)
                self.assertIn("explicit --to", r.stderr)
        self.assertEqual([c for c in self.fake.calls("emulator") if "-avd" in c], [])

    def test_boot_simulator_and_show_window_flag(self):
        r = self.f.run("boot", "--to", "iPhone 16 Pro Max", "--json")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(json.loads(r.stdout)["results"][0]["id"], SIM_OFF)
        r = self.f.run("boot", "--to", "iPhone 16 Pro Max")
        self.assertIn("nothing to boot", r.stdout)

    def test_emulator_that_exits_early_is_reported_with_its_error(self):
        r = self.f.run("boot", "--to", "pixel_api35,tablet*", "--json")
        out = json.loads(r.stdout)
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertEqual([x["id"] for x in out["results"]], ["pixel_api35"])  # tablet already running
        os.makedirs(os.path.join(self.fake.root, "avd", "broken.avd"))
        self.fake.add("emulator", [
            {"match": ["-list-avds"], "stdout": "broken\n"},
            {"match": ["-avd"], "rc": 1, "stdout": "PANIC: Missing emulator engine program for 'arm64' CPU.\n"},
        ], directory=os.path.join(self.fake.root, "sdk", "emulator"))
        r = self.f.run("boot", "--to", "broken", "--json")
        rec = json.loads(r.stdout)["results"][0]
        self.assertEqual(rec["result"], "failed")
        self.assertEqual(rec["reason"], "emulator exited with code 1: "
                                        "PANIC: Missing emulator engine program for 'arm64' CPU.")

    def test_boot_timeout_when_device_never_appears(self):
        os.makedirs(os.path.join(self.fake.root, "avd", "ghost.avd"))
        self.fake.add("emulator", [
            {"match": ["-list-avds"], "stdout": "ghost\n"},
            {"match": ["-avd"]},                       # starts, but nothing ever shows up in adb
        ], directory=os.path.join(self.fake.root, "sdk", "emulator"))
        r = self.f.run("boot", "--to", "ghost", "--boot-timeout", "1", "--json")
        rec = json.loads(r.stdout)["results"][0]
        self.assertEqual((rec["result"], rec["stage"]), ("failed", "boot"))
        self.assertIn("did not finish booting within 1s", rec["reason"])
        self.assertEqual(r.returncode, 1)

    def test_shutdown_only_touches_virtual_devices(self):
        open(self.f.running, "w").close()
        r = self.f.run("shutdown", "--to", "pixel_api35", "--json")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn(["-s", "emulator-5556", "emu", "kill"], self.fake.calls("adb"))
        self.assertFalse(os.path.exists(self.f.running))
        r = self.f.run("shutdown", "--to", "physical")
        self.assertEqual(r.returncode, 3)
        # devdrop waited for the device to leave adb's list before reporting it stopped
        plain = [c for c in self.fake.calls("adb") if c == ["devices"]]
        self.assertTrue(plain, "shutdown never checked that the emulator was gone")

    def test_shutdown_reports_an_emulator_that_does_not_exit(self):
        from devdrop.device import Device
        stuck = FakeTools()
        self.addCleanup(stuck.close)
        adb = stuck.add("adb", [
            {"match": ["emu", "kill"], "stdout": "OK: killing emulator, bye bye\n"},
            {"match": ["devices"], "stdout": "List of devices attached\nemulator-5554\tdevice\n"},
        ])
        dev = Device("emulator-5554", "android", "emulator", "ready")
        with self.assertRaisesRegex(virtual.ToolError, "still running 1s after emu kill"):
            virtual.shutdown(adb, None, dev, timeout=1)


if __name__ == "__main__":
    unittest.main()
