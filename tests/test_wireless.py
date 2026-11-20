# SPDX-License-Identifier: Apache-2.0
"""Wi-Fi pairing and connecting, duplicate devices, and install --watch."""

import json
import os
import signal
import subprocess
import sys
import time
import unittest

from devdrop import android
from devdrop.device import Device
from tests import builders as b
from tests.fakes import FakeTools
from tests.test_devices import REPO, devdrop
from tests.test_install import adb_rules

MDNS = ("List of discovered mdns services\n"
        "adb-14141FDF600081-wvUOyH\t_adb-tls-connect._tcp\t192.168.1.42:43085\n"
        "adb-14141FDF600081-wvUOyH\t_adb-tls-pairing._tcp\t192.168.1.42:37281\n"
        "adb-0A1B2C3D-Xy9zQw\t_adb-tls-connect._tcp.\t192.168.1.43:40001\n")


class Parsing(unittest.TestCase):
    def test_mdns(self):
        self.assertEqual(android.parse_mdns(MDNS), [
            ("adb-14141FDF600081-wvUOyH", "_adb-tls-connect._tcp", "192.168.1.42:43085"),
            ("adb-14141FDF600081-wvUOyH", "_adb-tls-pairing._tcp", "192.168.1.42:37281"),
            ("adb-0A1B2C3D-Xy9zQw", "_adb-tls-connect._tcp", "192.168.1.43:40001")])
        self.assertEqual(android.parse_mdns("List of discovered mdns services\n\n"), [])


def dev(id_, transport, serial, model="Pixel 8", kind="physical", name=""):
    return Device(id_, "android", kind, "ready", model=model, transport=transport, serial_number=serial, name=name)


class Duplicates(unittest.TestCase):
    def ids(self, devices):
        return [d.id for d in android.one_per_device(devices)]

    def test_wifi_copy_of_a_usb_phone_is_dropped(self):
        self.assertEqual(self.ids([dev("192.168.1.42:43085", "wifi", "S1"), dev("S1USB", "usb", "S1")]),
                         ["S1USB"])

    def test_distinct_devices_sharing_a_serial_are_kept(self):
        # Every emulator reports the emulator version as ro.serialno.
        emus = [dev("emulator-5554", "usb", "EMULATOR37X2X13X0", "sdk", "emulator", "api35"),
                dev("emulator-5556", "usb", "EMULATOR37X2X13X0", "sdk", "emulator", "api24")]
        self.assertEqual(self.ids(emus), ["emulator-5554", "emulator-5556"])
        # Some cheap phones share 0123456789ABCDEF; on cables both stay.
        cheap = [dev("A", "usb", "0123456789ABCDEF"), dev("B", "usb", "0123456789ABCDEF")]
        self.assertEqual(self.ids(cheap), ["A", "B"])
        # A Wi-Fi phone with the same bogus serial but another model stays too.
        self.assertEqual(self.ids(cheap + [dev("10.0.0.9:5555", "wifi", "0123456789ABCDEF", "Other")]),
                         ["A", "B", "10.0.0.9:5555"])

    def test_tcp_copy_of_an_emulator_is_dropped(self):
        emu = dev("emulator-5554", "usb", "EMULATOR37X2X13X0", "sdk", "emulator", "api35")
        tcp = dev("127.0.0.1:5555", "wifi", "EMULATOR37X2X13X0", "sdk", "emulator", "api35")
        self.assertEqual(self.ids([tcp, emu]), ["emulator-5554"])


class PairAndConnect(unittest.TestCase):
    def setUp(self):
        self.fake = FakeTools()
        self.addCleanup(self.fake.close)

    def test_pair(self):
        self.fake.add("adb", [
            {"match": ["pair", "192.168.1.42:37281", "123456"],
             "stdout": "Successfully paired to 192.168.1.42:37281 [guid=adb-14141FDF600081-wvUOyH]\n"},
            {"match": ["pair"], "rc": 1, "stdout": "Failed: Wrong password or connection was dropped.\n"}])
        r = devdrop(self.fake, "pair", "192.168.1.42:37281", "123456")
        self.assertEqual((r.returncode, "Successfully paired" in r.stdout), (0, True))
        r = devdrop(self.fake, "pair", "192.168.1.42:37281", "000000")
        self.assertEqual(r.returncode, 1)
        self.assertIn("Wrong password", r.stderr)

    def test_connect_given_and_discovered(self):
        self.fake.add("adb", [
            {"match": ["mdns", "services"], "stdout": MDNS},
            {"match": ["connect", "192.168.1.42:43085"], "stdout": "connected to 192.168.1.42:43085\n"},
            {"match": ["connect", "192.168.1.43:40001"],
             "stdout": "failed to connect to '192.168.1.43:40001': Connection refused\n"}])
        r = devdrop(self.fake, "connect", "192.168.1.42:43085")
        self.assertEqual((r.returncode, r.stdout.strip()), (0, "connected to 192.168.1.42:43085"))
        r = devdrop(self.fake, "connect")                  # discovery: the pairing service is not dialled
        self.assertEqual(r.returncode, 1)
        dialled = [c[1] for c in self.fake.calls("adb") if c[0] == "connect"]
        self.assertEqual(dialled[1:], ["192.168.1.42:43085", "192.168.1.43:40001"])
        self.assertIn("Connection refused", r.stderr)

    def test_nothing_to_discover_and_no_adb(self):
        self.fake.add("adb", [{"match": ["mdns"], "stdout": "List of discovered mdns services\n"}])
        self.assertEqual(devdrop(self.fake, "connect").returncode, 3)
        empty = FakeTools()
        self.addCleanup(empty.close)
        self.assertEqual(devdrop(empty, "connect").returncode, 4)
        self.assertEqual(devdrop(empty, "pair", "h:1", "1").returncode, 4)


class Watch(unittest.TestCase):
    def setUp(self):
        self.fake = FakeTools()
        self.addCleanup(self.fake.close)
        self.apk = os.path.join(self.fake.root, "a.apk")
        b.apk(self.apk, b.axml(b.manifest(min_sdk=21)), v2=True)
        self.later = os.path.join(self.fake.root, "plugged-in")
        lab = [("AAA111", 34, "14", "Pixel 8")]
        both = lab + [("BBB222", 34, "14", "Galaxy S24")]
        rules = adb_rules(devices=both)
        listing_one = "List of devices attached\nAAA111 device model:Pixel_8\n"
        rules.insert(0, {"match": ["devices", "-l"], "unless_file": self.later, "stdout": listing_one})
        self.booted = os.path.join(self.fake.root, "booted")
        # BBB222 answers as a device still starting until the "booted" marker exists.
        rules.insert(0, {"match": ["-s", "BBB222", "getprop", "sys.boot_completed"],
                         "unless_file": self.booted, "stdout": "\n"})
        rules.insert(1, {"match": ["getprop", "sys.boot_completed"], "stdout": "1\n"})
        self.fake.add("adb", rules)

    def watch(self, *args, plug_after=None):
        env = self.fake.env()
        env["PYTHONPATH"] = REPO
        p = subprocess.Popen([sys.executable, "-m", "devdrop", "install", self.apk, "--watch", "--json", *args],
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env, cwd=self.fake.root)
        if plug_after is not None:
            time.sleep(plug_after)
            open(self.later, "w").close()
        out, err = p.communicate(timeout=60)
        return p.returncode, out, err

    def test_device_plugged_in_later_is_installed_once(self):
        open(self.booted, "w").close()
        rc, out, err = self.watch("--watch-timeout", "9", plug_after=1.5)
        self.assertEqual(rc, 0, err)
        ids = [r["id"] for r in json.loads(out)["results"]]
        self.assertEqual(ids, ["AAA111", "BBB222"])
        installs = [c[1] for c in self.fake.calls("adb") if "install" in c]
        self.assertEqual(sorted(installs), ["AAA111", "BBB222"])   # once each, across several polls

    def test_device_still_booting_is_left_until_it_has_booted(self):
        # adb lists a device as ready before Android can take an install.
        def calls():
            try:
                return self.fake.calls("adb")
            except ValueError:                              # a call record still being written
                return []

        def asked():
            return [c for c in calls() if "sys.boot_completed" in c and "BBB222" in c]

        def installs():
            return [c[1] for c in calls() if "install" in c]

        env = self.fake.env()
        env["PYTHONPATH"] = REPO
        open(self.later, "w").close()                       # BBB222 is listed from the start
        p = subprocess.Popen([sys.executable, "-m", "devdrop", "install", self.apk, "--watch", "--json",
                              "--watch-timeout", "60"],
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env, cwd=self.fake.root)
        try:
            deadline = time.monotonic() + 50
            while len(asked()) < 2:                         # asked at the start and on a later scan
                self.assertLess(time.monotonic(), deadline, "the watch never asked whether BBB222 had booted")
                time.sleep(0.2)
            self.assertEqual(installs(), ["AAA111"])        # not installed on while it is starting
            open(self.booted, "w").close()
            while "BBB222" not in installs():
                self.assertLess(time.monotonic(), deadline, "no install after BBB222 finished booting")
                time.sleep(0.2)
        finally:
            if os.name == "nt":
                p.terminate()                               # no Ctrl-C to send: no summary either
            else:
                p.send_signal(signal.SIGINT)
            out, err = p.communicate(timeout=60)
        self.assertEqual(sorted(c[1] for c in self.fake.calls("adb") if "install" in c),
                         ["AAA111", "BBB222"])              # once each
        if os.name != "nt":
            self.assertEqual([r["id"] for r in json.loads(out)["results"]], ["AAA111", "BBB222"], err)

    def test_watch_with_no_device_ever_exits_3(self):
        rc, out, err = self.watch("--to", "nothing-matches", "--watch-timeout", "3")
        self.assertEqual(rc, 3)
        self.assertIn("no device appeared", err)


if __name__ == "__main__":
    unittest.main()
