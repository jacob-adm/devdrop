# SPDX-License-Identifier: Apache-2.0
"""Error paths and edge cases, module by module."""

import datetime
import json
import os
import plistlib
import stat
import struct
import sys
import tempfile
import unittest
import zipfile

from devdrop import artifact, axml, config, deploy, preflight, tools, virtual
from devdrop.device import Device
from devdrop.select import SelectorError, select
from tests import builders as b
from tests.fakes import FakeTools, adb_rules, fixture, xcrun_rules
from tests.test_devices import devdrop
from tests.test_install import adb_rules as lab_rules

SLOW = {"DEVDROP_TOOL_TIMEOUT": "0.5"}


class Tmp(unittest.TestCase):
    def setUp(self):
        self._t = tempfile.TemporaryDirectory()
        self.addCleanup(self._t.cleanup)
        self.dir = self._t.name

    def path(self, *parts):
        return os.path.join(self.dir, *parts)


# ---------------------------------------------------------------- tools

class Tools(Tmp):
    def test_run_missing_program_and_timeout(self):
        with self.assertRaisesRegex(tools.ToolError, "not found"):
            tools.run([self.path("nope")])
        with self.assertRaisesRegex(tools.ToolError, "no answer after 0.3s"):
            tools.run([sys.executable, "-c", "import time; time.sleep(5)"], timeout=0.3)

    def test_timeout_setting(self):
        cases = {None: 30, "5": 5.0, "0.5": 0.5, "0": 30, "-1": 30, "abc": 30, "inf": 30, "nan": 30}
        for value, want in cases.items():
            with self.subTest(value=value):
                env = {} if value is None else {"DEVDROP_TOOL_TIMEOUT": value}
                self.assertEqual(tools.default_timeout(env), want)

    def test_sdk_roots_per_system(self):
        env = {"ANDROID_HOME": "/a", "ANDROID_SDK_ROOT": "/b", "LOCALAPPDATA": "C:/L"}
        self.assertEqual(tools.sdk_roots(env, "/h", "darwin"),
                         ["/a", "/b", os.path.join("/h", "Library", "Android", "sdk")])
        self.assertEqual(tools.sdk_roots(env, "/h", "linux")[-1], os.path.join("/h", "Android", "Sdk"))
        self.assertEqual(tools.sdk_roots(env, "/h", "windows")[-1], os.path.join("C:/L", "Android", "Sdk"))
        self.assertEqual(tools.sdk_roots({}, "/h", "windows"), [])

    def test_find_adb_prefers_sdk_and_skips_non_executable(self):
        if os.name == "nt":
            self.skipTest("executable bit")
        sdk = self.path("sdk", "platform-tools")
        os.makedirs(sdk)
        adb = os.path.join(sdk, "adb")
        open(adb, "w").close()
        env = {"ANDROID_HOME": self.path("sdk"), "PATH": ""}
        self.assertNotEqual(tools.find_adb(env, self.dir), adb)   # not executable
        os.chmod(adb, 0o755)
        self.assertEqual(tools.find_adb(env, self.dir), adb)


# ---------------------------------------------------------------- axml

def chunk(ctype, header, body=b""):
    return struct.pack("<HHI", ctype, header, header + len(body)) + body


class AxmlEdges(unittest.TestCase):
    def test_values_of_every_type(self):
        root = ("manifest", [("package", b.STR, "p"), ("versionCode", b.HEX, 0xFFFFFFFF),
                             ("flag", b.BOOL, False), ("ref", b.REF, 0x7F010001),
                             ("dim", 0x05, 0x1001)], [])
        attrs = axml.parse(b.axml(root)).attrs
        self.assertEqual(attrs["versionCode"], -1)
        self.assertIs(attrs["flag"], False)
        self.assertEqual(attrs["ref"], "@0x7f010001")
        self.assertEqual(attrs["dim"], 0x1001)

    def test_raw_string_fallback_for_untyped_value(self):
        data = bytearray(b.axml(("manifest", [("package", b.STR, "com.x")], [])))
        # Retype the attribute value from STRING to 0x00 (null): raw string index still names it.
        i = data.find(struct.pack("<HBB", 8, 0, b.STR))
        data[i + 3] = 0x00
        self.assertEqual(axml.parse(bytes(data)).attrs["package"], "com.x")

    def test_string_pool_overflows(self):
        good = b.axml(b.manifest())
        pool_at = 8
        bad = bytearray(good)
        struct.pack_into("<I", bad, pool_at + 8, 10 ** 6)          # string count far past the pool
        with self.assertRaisesRegex(axml.AxmlError, "string pool runs past"):
            axml.parse(bytes(bad))
        bad = bytearray(good)
        first = pool_at + struct.unpack_from("<I", bad, pool_at + 20)[0]
        struct.pack_into("<H", bad, first, 0x7FFF)                 # first string claims 32767 chars
        with self.assertRaisesRegex(axml.AxmlError, "string runs past"):
            axml.parse(bytes(bad))

    def test_stray_end_element_and_second_root(self):
        pool = b.string_pool(["a", "b"], utf8=True)
        end = struct.pack("<HHIIIII", 0x0103, 16, 24, 1, 0xFFFFFFFF, 0xFFFFFFFF, 0)

        def start(name):
            ext = struct.pack("<IIHHHHHH", 0xFFFFFFFF, name, 20, 20, 0, 0, 0, 0)
            return struct.pack("<HHIII", 0x0102, 16, 16 + len(ext), 1, 0xFFFFFFFF) + ext
        body = pool + end + start(0) + end + start(1) + end
        doc = struct.pack("<HHI", 0x0003, 8, 8 + len(body)) + body
        root = axml.parse(doc)
        self.assertEqual((root.tag, root.children), ("a", []))

    def test_chunk_past_end_and_short_attribute_area(self):
        doc = struct.pack("<HHI", 0x0003, 8, 24) + struct.pack("<HHI", 0x0001, 28, 9999) + b"\0" * 8
        with self.assertRaisesRegex(axml.AxmlError, "runs past the end"):
            axml.parse(doc)
        pool = b.string_pool(["m"], utf8=True)
        ext = struct.pack("<IIHHHHHH", 0xFFFFFFFF, 0, 20, 20, 3, 0, 0, 0)   # claims 3 attributes, has none
        el = struct.pack("<HHIII", 0x0102, 16, 16 + len(ext), 1, 0xFFFFFFFF) + ext
        body = pool + el
        doc = struct.pack("<HHI", 0x0003, 8, 8 + len(body)) + body
        with self.assertRaisesRegex(axml.AxmlError, "truncated or malformed"):
            axml.parse(doc)

    def test_no_elements(self):
        pool = b.string_pool(["x"], utf8=False)
        doc = struct.pack("<HHI", 0x0003, 8, 8 + len(pool)) + pool
        with self.assertRaisesRegex(axml.AxmlError, "no elements"):
            axml.parse(doc)


# ---------------------------------------------------------------- artifact

class ArtifactEdges(Tmp):
    def test_paths_that_are_not_builds(self):
        os.mkdir(self.path("folder"))
        with self.assertRaisesRegex(artifact.ArtifactError, "must be an .app bundle"):
            artifact.inspect(self.path("folder"))
        open(self.path("x.zip"), "wb").close()
        with self.assertRaisesRegex(artifact.ArtifactError, "unknown build type"):
            artifact.inspect(self.path("x.zip"))

    def test_corrupt_manifest_and_wrong_root(self):
        p = self.path("bad.apk")
        with zipfile.ZipFile(p, "w") as z:
            z.writestr("AndroidManifest.xml", b"\x03\x00\x08\x00garbage!")
        with self.assertRaisesRegex(artifact.ArtifactError, "AndroidManifest.xml: "):
            artifact.inspect(p)
        b.apk(p, b.axml(("application", [], [])))
        with self.assertRaisesRegex(artifact.ArtifactError, "root is <application>"):
            artifact.inspect(p)

    def test_string_version_code_bare_launcher_and_no_sdk(self):
        root = ("manifest", [("package", b.STR, "com.x"), ("versionCode", b.STR, "12")], [
            ("application", [], [("activity", [("name", b.STR, "Main")], [
                ("intent-filter", [], [
                    ("action", [("name", b.STR, "android.intent.action.MAIN")], []),
                    ("category", [("name", b.STR, "android.intent.category.LAUNCHER")], [])])]),
                ("activity", [("name", b.STR, ".Other")], [
                    ("intent-filter", [], [("action", [("name", b.STR, "android.intent.action.VIEW")], [])])])])])
        p = self.path("bare.apk")
        b.apk(p, b.axml(root), v1=True)
        r = artifact.inspect(p)
        self.assertEqual((r["build"], r["launcher"], r["min_sdk"], r["target_sdk"]),
                         ("12", "com.x.Main", 1, 1))
        self.assertEqual(artifact._int_or_codename("34"), 34)

    def test_signing_block_detection_edges(self):
        p = self.path("tiny.apk")
        b.apk(p, b.axml(b.manifest()))
        with open(p, "rb") as f:
            data = f.read()
        eocd = data.rfind(b"PK\x05\x06")
        bad = bytearray(data)
        struct.pack_into("<I", bad, eocd + 16, 4)                 # central directory "at" byte 4
        with open(p, "wb") as f:
            f.write(bad)
        self.assertEqual(artifact._apk_signing(p, []), [])
        with open(p, "wb") as f:
            f.write(b"no zip here")
        self.assertEqual(artifact._apk_signing(p, ["META-INF/A.SF"]), ["v1"])

    def test_ipa_without_info_plist_or_profile(self):
        p = self.path("noinfo.ipa")
        with zipfile.ZipFile(p, "w") as z:
            z.writestr("Payload/X.app/X", b"")
        with self.assertRaisesRegex(artifact.ArtifactError, "Info.plist missing"):
            artifact.inspect(p)
        b.ipa(p, b.info_plist())
        self.assertIsNone(artifact.inspect(p)["profile"])

    def test_app_directory_variants(self):
        app = self.path("X.app")
        os.mkdir(app)
        with self.assertRaisesRegex(artifact.ArtifactError, "Info.plist missing"):
            artifact.inspect(app)
        with open(os.path.join(app, "Info.plist"), "wb") as f:
            f.write(b"junk")
        with self.assertRaisesRegex(artifact.ArtifactError, "Info.plist: "):
            artifact.inspect(app)
        with open(os.path.join(app, "Info.plist"), "wb") as f:
            f.write(b.info_plist())
        with open(os.path.join(app, "embedded.mobileprovision"), "wb") as f:
            f.write(b.mobileprovision())
        r = artifact.inspect(app + os.sep, now=datetime.datetime(2026, 1, 1))
        self.assertEqual((r["target"], r["profile"]["kind"]), ("device", "development"))

    def test_profile_without_dates_or_team(self):
        data = plistlib.dumps({"Name": "n", "ProvisionedDevices": []})
        p = artifact.profile_from_bytes(b"\x30" + data)
        self.assertEqual((p["team"], p["expires"], p["expired"], p["kind"]), ("", "", False, "ad-hoc"))


# ---------------------------------------------------------------- config, select, preflight

class ConfigEdges(Tmp):
    def test_explicit_missing_and_non_table(self):
        with self.assertRaisesRegex(config.ConfigError, "not found"):
            config.load(self.path("none.toml"))
        with self.assertRaisesRegex(config.ConfigError, "must be tables"):
            config.parse({"devices": ["x"]})

    def test_default_search_order(self):
        xdg = self.path("xdg", "devdrop")
        os.makedirs(xdg)
        with open(os.path.join(xdg, "devdrop.toml"), "w") as f:
            f.write('[devices]\nfromxdg = "A"\n')
        env = {"XDG_CONFIG_HOME": self.path("xdg")}
        self.assertEqual(config.load(cwd=self.dir, env=env).devices, {"fromxdg": "A"})
        with open(self.path("devdrop.toml"), "w") as f:
            f.write('[devices]\nlocal = "B"\n')
        self.assertEqual(config.load(cwd=self.dir, env=env).devices, {"local": "B"})
        self.assertEqual(config.load(cwd=self.path("xdg"), env={"XDG_CONFIG_HOME": self.path("none")}).path, "")


class SelectEdges(unittest.TestCase):
    def test_empty_selector_and_odd_versions(self):
        cfg = config.Config()
        with self.assertRaises(SelectorError):
            select([], ",", cfg)
        d = Device("U", "ios", "physical", "ready", os_version="26.1b2")
        self.assertEqual(select([d], "ios>=26", cfg), [d])
        d.os_version = "beta"
        self.assertEqual(select([d], "ios>=1", cfg), [])


class PreflightEdges(unittest.TestCase):
    APK = {"platform": "android", "type": "apk", "signing": ["v2+"], "min_sdk": 21, "abis": ["x86_64"]}

    def test_android_reasons(self):
        dev = Device("A", "android", "physical", "ready", api=34, abis=["arm64-v8a"])
        self.assertEqual(preflight.check(self.APK, dev), "no native code for arm64-v8a (build has x86_64)")
        self.assertIsNone(preflight.check(dict(self.APK, abis=[]), dev))
        self.assertIsNone(preflight.check(self.APK, Device("B", "android", "emulator", "ready", api=34)))
        self.assertIn("preview build (minSdk Baklava)",
                      preflight.check(dict(self.APK, min_sdk="Baklava"), dev))
        ios_dev = Device("U", "ios", "physical", "ready")
        self.assertEqual(preflight.check(self.APK, ios_dev), "apk build cannot go on an ios device")

    def test_ios_version_with_suffix(self):
        dev = Device("U", "ios", "simulator", "ready", os_version="18.5b")
        report = {"platform": "ios", "target": "simulator", "min_os": "18.4", "families": ["iphone"]}
        self.assertIsNone(preflight.check(report, dev))


# ---------------------------------------------------------------- deploy

class DeployEdges(Tmp):
    def test_ios_failure_without_hint_and_empty(self):
        self.assertEqual(deploy.ios_failure("something odd\n"), "something odd")
        self.assertEqual(deploy.ios_failure(""), "unknown error")

    def test_unpack_skips_other_entries_and_dirs(self):
        ipa = self.path("a.ipa")
        with zipfile.ZipFile(ipa, "w") as z:
            z.writestr("Payload/", b"")
            z.writestr("Symbols/x", b"")
            z.writestr(zipfile.ZipInfo("Payload/A.app/Frameworks/"), b"")
            z.writestr("Payload/A.app/Info.plist", b"p")
            z.writestr("Payload/B.app/Info.plist", b"second app ignored")
            link = zipfile.ZipInfo("Payload/A.app/link")
            link.external_attr = (stat.S_IFLNK | 0o777) << 16
            z.writestr(link, "Info.plist")
        out = self.path("out")
        os.mkdir(out)
        app = deploy.unpack_ipa(ipa, out)
        self.assertTrue(os.path.isdir(os.path.join(app, "Frameworks")))
        self.assertFalse(os.path.exists(os.path.join(out, "B.app")))
        with zipfile.ZipFile(self.path("empty.ipa"), "w") as z:
            z.writestr("Other/x", b"")
        with self.assertRaisesRegex(deploy.StepError, "no Payload"):
            deploy.unpack_ipa(self.path("empty.ipa"), out)


class AndroidAdbEdges(unittest.TestCase):
    """adb behaviours seen on real devices that the main tests do not reach."""

    def setUp(self):
        self.fake = FakeTools()
        self.addCleanup(self.fake.close)

    def test_getprop_timeout_marks_device_not_responding(self):
        rules = adb_rules()
        rules.insert(1, {"match": ["-s", "37201FDH2000CK", "getprop"], "sleep": 3})
        self.fake.add("adb", rules)
        r = devdrop(self.fake, "devices", "--json", env_extra=SLOW)
        devs = {d["id"]: d for d in json.loads(r.stdout)["devices"]}
        self.assertEqual(devs["37201FDH2000CK"]["state"], "not-responding")

    def test_launch_with_no_launcher_and_am_start_error(self):
        self.fake.add("adb", lab_rules(extra=[
            {"match": ["-s", "AAA111", "resolve-activity"], "stdout": "No activity found\n"},
            {"match": ["-s", "BBB222", "am", "start"],
             "stdout": "Starting: Intent { cmp=com.example.app/.MainActivity }\n"
                       "Error type 3\nError: Activity class {com.example.app/.MainActivity} does not exist.\n"}]))
        r = devdrop(self.fake, "launch", "com.example.app", "--to", "AAA111,BBB222", "--json")
        recs = {x["id"]: x for x in json.loads(r.stdout)["results"]}
        self.assertEqual(recs["AAA111"]["reason"], "no launcher activity (is the app installed?)")
        self.assertEqual(recs["BBB222"]["reason"], "Error type 3")

    def test_tool_timeout_during_install_and_launch(self):
        self.fake.add("adb", lab_rules(extra=[{"match": ["-s", "AAA111", "resolve-activity"], "sleep": 3}]))
        r = devdrop(self.fake, "launch", "com.example.app", "--to", "AAA111", "--json", env_extra=SLOW)
        rec = json.loads(r.stdout)["results"][0]
        self.assertEqual((rec["result"], rec["stage"]), ("failed", "tool"))
        apk = os.path.join(self.fake.root, "a.apk")
        b.apk(apk, b.axml(b.manifest()), v2=True)
        r = devdrop(self.fake, "install", apk, "--to", "AAA111", "--launch", "--json", env_extra=SLOW)
        self.assertEqual(json.loads(r.stdout)["results"][0]["stage"], "tool")

    def test_by_id_skips_devices_that_are_not_ready(self):
        self.fake.add("adb", adb_rules())
        r = devdrop(self.fake, "uninstall", "com.x", "--to", "FA35DS911282", "--json")
        rec = json.loads(r.stdout)["results"][0]
        self.assertEqual((rec["result"], rec["reason"]), ("skipped", "device is unauthorized"))


class IosToolEdges(unittest.TestCase):
    def setUp(self):
        self.fake = FakeTools()
        self.addCleanup(self.fake.close)

    def backend_error(self, rules, env=None):
        self.fake.add("xcrun", rules)
        r = devdrop(self.fake, "devices", "--json", env_extra=env)
        return r.returncode, json.loads(r.stdout)["backends"]["ios"]["error"]

    def test_listing_failures_are_reported_per_tool(self):
        sims = fixture("simctl_list.json")
        cases = [
            ([{"match": ["--find"], "stdout": "x"}, {"match": ["devicectl"], "rc": 1, "stderr": "CoreDevice down\n"},
              {"match": ["simctl"], "stdout": sims}], "devicectl list devices: CoreDevice down"),
            ([{"match": ["--find"], "stdout": "x"},
              {"match": ["devicectl"], "write_after": "--json-output", "file": "{not json"},
              {"match": ["simctl"], "stdout": sims}], "devicectl list devices: unreadable JSON"),
            ([{"match": ["--find"], "rc": 1}, {"match": ["simctl"], "rc": 1, "stderr": "simctl broke\n"}],
             "simctl list: simctl broke"),
            ([{"match": ["--find"], "rc": 1}, {"match": ["simctl"], "stdout": "nope"}], "simctl list: unreadable JSON"),
        ]
        for rules, want in cases:
            with self.subTest(want=want):
                code, error = self.backend_error(rules)
                self.assertEqual(code, 1)
                self.assertIn(want, error)

    def test_find_devicectl_timeout_counts_as_missing(self):
        code, error = self.backend_error([{"match": ["--find"], "sleep": 3},
                                          {"match": ["simctl"], "stdout": fixture("simctl_list.json")}], SLOW)
        self.assertIn("devicectl not found", error)

    def test_unavailable_simulators_are_hidden(self):
        doc = json.loads(fixture("simctl_list.json"))
        for v in doc["devices"].values():
            for s in v:
                s["isAvailable"] = False
        self.assertEqual(__import__("devdrop.ios", fromlist=["x"]).parse_simctl(doc, True), [])

    def test_physical_uninstall_paths(self):
        rules = xcrun_rules()
        from tests.test_ios_install import xcrun_rules as ios_rules, IPHONE
        rules = ios_rules(extra=[{"match": ["devicectl", "device", "uninstall"], "rc": 1,
                                  "stderr": "ERROR: The app is not installed.\n"}])
        self.fake.add("xcrun", rules)
        r = devdrop(self.fake, "uninstall", "com.example.app", "--to", IPHONE, "--json")
        self.assertEqual(json.loads(r.stdout)["results"][0]["reason"], "The app is not installed.")
        ipa = os.path.join(self.fake.root, "a.ipa")
        b.ipa(ipa, b.info_plist(), b.mobileprovision(devices=[IPHONE]))
        r = devdrop(self.fake, "install", ipa, "--to", IPHONE, "--clean", "--json")
        self.assertEqual(json.loads(r.stdout)["ok"], 1)                  # missing app tolerated by --clean

    def test_simulator_uninstall_of_installed_app(self):
        from tests.test_ios_install import xcrun_rules as ios_rules
        self.fake.add("xcrun", ios_rules(extra=[{"match": ["simctl", "get_app_container"], "stdout": "/x\n"}]))
        r = devdrop(self.fake, "uninstall", "com.example.app", "--to", "sim", "--json")
        self.assertEqual(json.loads(r.stdout)["ok"], 1)
        self.assertIn("uninstall", [c[1] for c in self.fake.calls("xcrun") if c[0] == "simctl"])


# ---------------------------------------------------------------- virtual

class VirtualEdges(Tmp):
    def test_find_emulator_and_avd_home(self):
        self.assertIsNone(virtual.find_emulator({"PATH": ""}, self.dir) if not
                          __import__("shutil").which("emulator") else None)
        self.assertEqual(virtual.avd_home({"ANDROID_AVD_HOME": "/x"}), "/x")
        self.assertEqual(virtual.avd_home({"ANDROID_USER_HOME": "/u"}), os.path.join("/u", "avd"))
        self.assertTrue(virtual.avd_home({}).endswith(os.path.join(".android", "avd")))

    def test_avd_api_from_top_level_ini_and_junk_lines(self):
        with open(self.path("x.ini"), "w") as f:
            f.write("junk line without equals\ntarget=android-29\npath=/nowhere\n")
        self.assertEqual(virtual.avd_api("x", self.dir), 29)

    def test_start_failure_and_missing_log(self):
        with self.assertRaisesRegex(tools.ToolError, "emulator: "):
            virtual.start_avd(self.path("no-emulator"), "x", self.path("log"))
        self.assertEqual(virtual._last_line(self.path("missing.log")), "")
        open(self.path("empty.log"), "w").close()
        self.assertEqual(virtual._last_line(self.path("empty.log")), "")


class VirtualCommands(unittest.TestCase):
    def setUp(self):
        self.fake = FakeTools()
        self.addCleanup(self.fake.close)

    def test_list_avds_failure_and_boot_with_show(self):
        from tests.test_boot import BootFakes
        f = BootFakes(self.fake)
        self.fake.add("emulator", [{"match": ["-list-avds"], "rc": 1, "stderr": "no AVD home\n"}],
                      directory=os.path.join(self.fake.root, "sdk", "emulator"))
        r = f.run("devices", "--all", "--json")
        self.assertIn("emulator -list-avds: no AVD home", json.loads(r.stdout)["backends"]["android"]["error"])

    def test_show_flag_keeps_window_and_opens_simulator(self):
        from tests.test_boot import BootFakes, SIM_OFF
        f = BootFakes(self.fake)
        self.fake.add("open", [{"match": []}])
        r = f.run("install", os.path.join(self.fake.root, "missing.apk"), "--boot", "--to", "x")
        self.assertEqual(r.returncode, 2)                                 # unreadable build: nothing booted
        r = f.run("boot", "--to", "pixel_api35,iPhone 16 Pro Max", "--show", "--json")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        start = next(c for c in self.fake.calls("emulator") if "-avd" in c)
        self.assertEqual(start, ["-avd", "pixel_api35"])
        if sys.platform == "darwin":
            self.assertIn(["-a", "Simulator", "--args", "-CurrentDeviceUDID", SIM_OFF], self.fake.calls("open"))

    def test_simulator_boot_errors(self):
        from tests.test_boot import BootFakes
        f = BootFakes(self.fake)
        rules = [
            {"match": ["--find", "devicectl"], "rc": 1},
            {"match": ["simctl", "list"], "stdout": fixture("simctl_list.json")},
            {"match": ["simctl", "boot", "AAAAAAAA-0000-0000-0000-000000000008"], "rc": 149,
             "stderr": "Unable to boot device in current state: Booted\n"},
            {"match": ["simctl", "boot"], "rc": 1, "stderr": "Invalid device state\n"},
            {"match": ["simctl", "bootstatus"], "rc": 1, "stderr": "boot failed\n"},
        ]
        self.fake.add("xcrun", rules)
        r = f.run("boot", "--to", "iPhone 17 Pro,iPhone 17 Pro Max", "--json")
        recs = {x["id"][-2:]: x["reason"] for x in json.loads(r.stdout)["results"]}
        self.assertEqual(recs, {"08": "simctl bootstatus: boot failed", "09": "simctl boot: Invalid device state"})
        self.assertEqual(r.returncode, 1)

    def test_boot_and_shutdown_edges(self):
        from tests.test_boot import BootFakes
        f = BootFakes(self.fake)
        self.assertEqual(f.run("boot", "--to", "nothing-like-this").returncode, 3)
        self.assertEqual(f.run("boot", "--to", ",").returncode, 2)
        self.assertEqual(f.run("shutdown", "--to", ",").returncode, 2)
        os.remove(os.path.join(self.fake.root, "sdk", "emulator",
                               "emulator.cmd" if os.name == "nt" else "emulator"))
        r = f.run("boot", "--to", "x*", "--json")       # no AVDs listed without the emulator: nothing matches
        self.assertEqual(r.returncode, 3)

    def test_boot_reports_missing_emulator_binary(self):
        from devdrop import cli
        dev = Device("ghost", "android", "emulator", "shutdown", name="ghost")
        env = {k: v for k, v in os.environ.items() if k not in ("ANDROID_HOME", "ANDROID_SDK_ROOT")}
        old = dict(os.environ)
        try:
            os.environ.clear()
            os.environ.update(env, PATH=self.fake.bin, HOME=self.fake.root)
            rec = cli._boot_one("adb", "xcrun", dev, 1, False)
        finally:
            os.environ.clear()
            os.environ.update(old)
        self.assertEqual(rec["reason"], "emulator not found (install the Android SDK emulator package)")

    def test_shutdown_failure_is_reported(self):
        from tests.test_boot import BootFakes
        f = BootFakes(self.fake)
        self.fake.add("adb", [
            {"match": ["devices", "-l"], "stdout": "List of devices attached\nemulator-5554 device transport_id:1\n"},
            {"match": ["getprop"], "stdout": "[ro.boot.qemu]: [1]\n"},
            {"match": ["emu", "kill"], "rc": 1, "stderr": "error: could not connect to TCP port 5554\n"},
        ], directory=os.path.join(self.fake.root, "sdk", "platform-tools"))
        r = f.run("shutdown", "--to", "emulator", "--json")
        rec = json.loads(r.stdout)["results"][0]
        self.assertEqual(rec["reason"], "error: could not connect to TCP port 5554")


# ---------------------------------------------------------------- cli

class CliEdges(unittest.TestCase):
    def setUp(self):
        self.fake = FakeTools()
        self.addCleanup(self.fake.close)

    def bad_config(self):
        with open(os.path.join(self.fake.root, "devdrop.toml"), "w") as f:
            f.write("[devices]\nx = 1\n")

    def test_every_command_rejects_a_bad_config(self):
        self.fake.add("adb", adb_rules())
        self.bad_config()
        apk = os.path.join(self.fake.root, "a.apk")
        b.apk(apk, b.axml(b.manifest()), v2=True)
        for args in (["devices"], ["doctor"], ["install", apk], ["launch", "x"], ["uninstall", "x"],
                     ["boot", "--to", "x"], ["shutdown"]):
            with self.subTest(args=args):
                r = devdrop(self.fake, *args)
                self.assertEqual(r.returncode, 2)
                self.assertIn("devices.x must be a non-empty string", r.stderr)

    def test_doctor_without_any_tool_and_with_unattached_alias(self):
        r = devdrop(self.fake, "doctor")
        self.assertEqual(r.returncode, 4)
        self.assertIn("xcrun not found", r.stdout)
        self.fake.add("adb", [{"match": ["devices"], "rc": 1, "stderr": "daemon not running\n"},
                              {"match": ["version"], "rc": 1}])
        with open(os.path.join(self.fake.root, "devdrop.toml"), "w") as f:
            f.write('[devices]\nold = "X1"\n')
        r = devdrop(self.fake, "doctor")
        self.assertEqual(r.returncode, 1)
        self.assertIn("android: adb devices: daemon not running", r.stdout)
        self.assertIn("config alias old (X1): not attached", r.stdout)
        self.assertIn(": exit 1", r.stdout)

    def test_doctor_reports_unrunnable_xcodebuild(self):
        self.fake.add("xcrun", xcrun_rules())
        r = devdrop(self.fake, "doctor")
        self.assertIn("xcode: xcodebuild: not found", r.stdout)

    def test_inspect_ipa_without_profile_and_launch_without_tools(self):
        ipa = os.path.join(self.fake.root, "a.ipa")
        b.ipa(ipa, b.info_plist())
        self.assertIn("profile:     none", devdrop(self.fake, "inspect", ipa).stdout)
        self.assertEqual(devdrop(self.fake, "launch", "x").returncode, 4)

    def test_install_selector_and_artifact_errors(self):
        self.fake.add("adb", adb_rules())
        apk = os.path.join(self.fake.root, "a.apk")
        b.apk(apk, b.axml(b.manifest()), v2=True)
        self.assertEqual(devdrop(self.fake, "install", apk, "--to", ",").returncode, 2)
        self.assertEqual(devdrop(self.fake, "install", apk + "x").returncode, 2)
        self.assertEqual(devdrop(self.fake, "shutdown", "--to", "physical").returncode, 3)

    def test_install_boot_with_no_match_exits_3(self):
        from tests.test_boot import BootFakes
        f = BootFakes(self.fake)
        apk = os.path.join(self.fake.root, "a.apk")
        b.apk(apk, b.axml(b.manifest()), v2=True)
        self.assertEqual(f.run("install", apk, "--boot", "--to", "zzz").returncode, 3)

    def test_unpack_failure_of_a_damaged_ipa(self):
        from tests.test_ios_install import xcrun_rules as ios_rules
        self.fake.add("xcrun", ios_rules())
        ipa = os.path.join(self.fake.root, "a.ipa")
        b.ipa(ipa, b.info_plist(), b.mobileprovision())
        with zipfile.ZipFile(ipa, "a") as z:   # an entry whose data is corrupt: listing works, reading fails
            z.writestr("Payload/Example.app/big", b"x" * 1000, compress_type=zipfile.ZIP_DEFLATED)
        with open(ipa, "r+b") as f:
            data = f.read()
            i = data.rfind(b"Payload/Example.app/big") + len("Payload/Example.app/big")
            f.seek(i)
            f.write(b"\xff" * 8)
        r = devdrop(self.fake, "install", ipa)
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertNotIn("Traceback", r.stderr)

    def test_keyboard_interrupt_exits_130(self):
        from devdrop import cli
        parser_func = cli.cmd_devices
        try:
            cli.cmd_devices = lambda args: (_ for _ in ()).throw(KeyboardInterrupt)
            self.assertEqual(cli.main(["devices"]), 130)
        finally:
            cli.cmd_devices = parser_func


if __name__ == "__main__":
    unittest.main()
