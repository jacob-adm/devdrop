# SPDX-License-Identifier: Apache-2.0
"""End-to-end: the installed devdrop CLI against real emulators and simulators.

Opt-in, because it boots devices and takes minutes:

    DEVDROP_E2E=1 python3 -m unittest tests.e2e.test_e2e -v

Settings (environment):
    DEVDROP_E2E_AVDS     comma-separated AVD names (default: DEFAULT_AVDS below)
    DEVDROP_E2E_SIMS     comma-separated simulator names or UDIDs
                         (default: the first iPhone simulator of each installed iOS runtime)

A named AVD or simulator that is not installed fails the run rather than
shrinking the matrix silently.
    DEVDROP_E2E_CACHE    where test apps and the report go (default ~/.cache/devdrop-e2e)

Every result devdrop reports is checked against the device itself with
adb or simctl. Devices this run booted are shut down at the end.
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
ENABLED = os.environ.get("DEVDROP_E2E") == "1"
CACHE = os.environ.get("DEVDROP_E2E_CACHE") or os.path.expanduser("~/.cache/devdrop-e2e")
PACKAGE = "dev.devdrop.e2e"
# Chosen where adb behaves differently, not by market share:
#   23  no `cmd package`: launch must fall back to the manifest launcher
#   24  oldest with `cmd package resolve-activity`; 29 scoped storage era
#   29_lowmem  the same image with 1 GB of RAM
#   34, 35, 36  current; 37 newest release
DEFAULT_AVDS = ("devdrop_api23,devdrop_api24,devdrop_api29,devdrop_api29_lowmem,"
                "devdrop_api34,devdrop_api35,devdrop_api36,devdrop_api37")

S = {}            # shared state: devdrop path, fixtures, device ids
REPORT = []       # every devdrop run, for the report file


def devdrop(*args, check_json=True):
    started = time.monotonic()
    r = subprocess.run([S["devdrop"], *args], capture_output=True, text=True, timeout=1800)
    entry = {"args": list(args), "rc": r.returncode, "seconds": round(time.monotonic() - started, 1)}
    out = None
    if "--json" in args and check_json:
        out = json.loads(r.stdout)
        entry["results"] = out.get("results", out.get("devices"))
    REPORT.append(entry)
    return r.returncode, out, r


def adb(serial, *args):
    return subprocess.run([S["adb"], "-s", serial, *args], capture_output=True, text=True, timeout=120).stdout


def simctl(*args):
    return subprocess.run(["xcrun", "simctl", *args], capture_output=True, text=True, timeout=300)


def by_id(out):
    return {r["id"]: r for r in out["results"]}


def setUpModule():
    if not ENABLED:
        raise unittest.SkipTest("set DEVDROP_E2E=1 to run end-to-end tests")
    try:
        _set_up()
    except BaseException:
        tearDownModule()        # unittest skips tearDownModule when setUpModule fails
        raise


def _set_up():
    venv = tempfile.mkdtemp(prefix="devdrop-e2e-venv-")
    S["venv"] = venv
    subprocess.run([sys.executable, "-m", "venv", venv], check=True)
    bindir = os.path.join(venv, "Scripts" if os.name == "nt" else "bin")
    subprocess.run([os.path.join(bindir, "python"), "-m", "pip", "install", "-q", REPO], check=True)
    S["devdrop"] = os.path.join(bindir, "devdrop")
    index = os.path.join(CACHE, "index.json")
    if os.environ.get("DEVDROP_E2E_REBUILD") or not os.path.exists(index):
        subprocess.run([sys.executable, os.path.join(HERE, "build_fixtures.py"), CACHE], check=True,
                       stdout=subprocess.DEVNULL)
    with open(index) as f:
        S["fx"] = json.load(f)

    _, devs, _ = devdrop("devices", "--all", "--json")
    S["adb"] = devs["backends"]["android"]["tool"]
    avds = os.environ.get("DEVDROP_E2E_AVDS", DEFAULT_AVDS).split(",")
    known = {d["name"] for d in devs["devices"] if d["platform"] == "android" and d["kind"] == "emulator"}
    missing = [a for a in avds if a not in known]
    if missing:
        raise RuntimeError(f"AVDs not found: {', '.join(missing)} (create them or set DEVDROP_E2E_AVDS)")
    S["avds"] = avds
    sims = os.environ.get("DEVDROP_E2E_SIMS")
    ios_sims = [d for d in devs["devices"] if d["kind"] == "simulator" and d["model"].startswith("iPhone")]
    if sims:
        wanted = sims.split(",")
        chosen = [d for d in ios_sims if d["id"] in wanted or d["name"] in wanted]
        found = {d["id"] for d in chosen} | {d["name"] for d in chosen}
        missing = [w for w in wanted if w not in found]
        if missing:
            raise RuntimeError(f"simulators not found: {', '.join(missing)}")
    else:
        first = {}
        for d in ios_sims:
            first.setdefault(d["os_version"], d)
        chosen = list(first.values())
    if sys.platform == "darwin" and not chosen:
        raise RuntimeError("no iPhone simulator found (install an iOS runtime or set DEVDROP_E2E_SIMS)")
    S["sims"] = [d["id"] for d in chosen]
    S["sim_os"] = {d["id"]: d["os_version"] for d in chosen}
    S["booted_by_us"] = [d["id"] if d["kind"] == "simulator" else d["name"]
                         for d in devs["devices"] if d["state"] == "shutdown"
                         and (d["name"] in S["avds"] or d["id"] in S["sims"])]

    # One iOS simulator proves the boot timeout before the real boot.
    if S["sims"] and S["sims"][0] in S["booted_by_us"]:
        rc, out, _ = devdrop("boot", "--to", S["sims"][0], "--boot-timeout", "1", "--json")
        S["boot_timeout"] = out["results"][0]
        simctl("shutdown", S["sims"][0])
        time.sleep(3)

    targets = ",".join(S["avds"] + S["sims"])
    rc, out, r = devdrop("boot", "--to", targets, "--json", "--boot-timeout", "600")
    failed = [x["id"] for x in out["results"] if x["result"] != "ok"]
    if failed:
        # The Android emulator occasionally crashes at start when many boot at once
        # (seen: "Attempted to set thread local GL render thread info twice").
        # Retry those once; the report records it.
        S["boot_retries"] = {x["id"]: x["reason"] for x in out["results"] if x["result"] != "ok"}
        rc, out, r = devdrop("boot", "--to", ",".join(failed), "--json", "--boot-timeout", "600")
    if rc != 0:
        raise RuntimeError(f"boot failed: {r.stdout}{r.stderr}")
    _, devs, _ = devdrop("devices", "--json")
    S["serials"] = {d["name"]: d["id"] for d in devs["devices"] if d["name"] in S["avds"]}
    S["api"] = {d["id"]: d["api"] for d in devs["devices"] if d["platform"] == "android"}
    S["android"] = ",".join(S["serials"].values())


def tearDownModule():
    if not ENABLED or "devdrop" not in S:
        return
    try:
        running = []
        _, devs, _ = devdrop("devices", "--json")
        for d in devs["devices"]:
            if d["name"] in S.get("booted_by_us", []) or d["id"] in S.get("booted_by_us", []):
                running.append(d["id"])
        if running:
            devdrop("shutdown", "--to", ",".join(running), "--json")
            S["left_running"] = [x for x in subprocess.run([S["adb"], "devices"], capture_output=True,
                                                           text=True).stdout.split() if x in running]
    finally:
        with open(os.path.join(CACHE, "e2e-report.json"), "w") as f:
            json.dump({"avds": S.get("avds"), "sims": S.get("sims"), "ios_versions": S.get("sim_os"),
                       "left_running_after_shutdown": S.get("left_running"),
                       "boot_retries": S.get("boot_retries"),
                       "android_api": S.get("api"), "runs": REPORT}, f, indent=2)
        shutil.rmtree(S.get("venv", ""), ignore_errors=True)


def apk(name):
    return S["fx"]["android"][name]["path"]


def ios(name, ipa=False):
    entry = S["fx"]["ios"][name]
    return entry["ipa"] if ipa else entry["path"]


class A_Inspect(unittest.TestCase):
    def test_every_fixture_reads_as_built(self):
        for name, meta in S["fx"]["android"].items():
            with self.subTest(name=name):
                rc, rep, _ = devdrop("inspect", meta["path"], "--json")
                self.assertEqual(rc, 0)
                self.assertEqual((rep["id"], rep["build"], rep["min_sdk"]),
                                 (PACKAGE, str(meta["version_code"]), meta["min_sdk"]))
                self.assertEqual(bool(rep["signing"]), meta["key"] is not None)
                self.assertEqual(rep["abis"], [meta["abi"]] if meta.get("abi") else [])
                self.assertEqual(rep["launcher"], PACKAGE + (".Shown" if meta.get("alias") else ".Main"))
        for name, meta in S["fx"]["ios"].items():
            for path in (meta["path"], meta["ipa"]):
                with self.subTest(name=name, path=os.path.basename(path)):
                    rc, rep, _ = devdrop("inspect", path, "--json")
                    self.assertEqual(rc, 0)
                    self.assertEqual((rep["id"], rep["min_os"]), (PACKAGE, meta["min_os"]))
                    self.assertEqual(rep["target"], "simulator" if meta["sdk"] == "iphonesimulator" else "device")
                    self.assertEqual(rep["families"], ["iphone" if f == 1 else "ipad" for f in meta["families"]])


@unittest.skipUnless(ENABLED and os.environ.get("DEVDROP_E2E_AVDS", "x"), "no Android matrix")
class B_Android(unittest.TestCase):
    def assert_version(self, serial, code):
        self.assertIn(f"versionCode={code}", adb(serial, "shell", "dumpsys", "package", PACKAGE))

    def test_1_clean_install_and_launch_everywhere_at_once(self):
        rc, out, _ = devdrop("install", apk("app-v1"), "--to", S["android"], "--clean", "--launch", "--json")
        self.assertEqual(rc, 0, out)
        self.assertEqual(out["ok"], len(S["serials"]))
        for rec in out["results"]:      # every result names the device family it came from
            maker = adb(rec["id"], "shell", "getprop", "ro.product.manufacturer").strip()
            self.assertEqual((rec["manufacturer"], rec["api"]), (maker, S["api"][rec["id"]]))
            self.assertTrue(rec["model"] and rec["os_version"])
        time.sleep(2)
        for serial in S["serials"].values():
            with self.subTest(serial=serial):
                self.assert_version(serial, 1)
                top = adb(serial, "shell", "dumpsys", "activity", "activities")
                self.assertIn(f"{PACKAGE}/.Main", top)

    def test_2_upgrade_then_downgrade(self):
        rc, out, _ = devdrop("install", apk("app-v2"), "--to", S["android"], "--json")
        self.assertEqual(rc, 0, out)
        for serial in S["serials"].values():
            self.assert_version(serial, 2)
        rc, out, _ = devdrop("install", apk("app-v1"), "--to", S["android"], "--json")
        recs = by_id(out)
        for serial in S["serials"].values():
            with self.subTest(serial=serial, api=S["api"][serial]):
                rec = recs[serial]
                if rec["result"] == "ok":
                    # Some system images let debuggable apps downgrade; then the device must really hold v1.
                    self.assert_version(serial, 1)
                else:
                    self.assertIn("VERSION_DOWNGRADE", rec["reason"])
                    self.assertIn("use --clean", rec["reason"])
                    self.assert_version(serial, 2)

    def test_3_signing_key_clash_then_clean(self):
        rc, out, _ = devdrop("install", apk("app-keyb"), "--to", S["android"], "--json")
        self.assertEqual(rc, 1)
        for rec in out["results"]:
            self.assertIn("UPDATE_INCOMPATIBLE", rec["reason"])
            self.assertIn("use --clean", rec["reason"])
        rc, out, _ = devdrop("install", apk("app-keyb"), "--to", S["android"], "--clean", "--json")
        self.assertEqual(rc, 0, out)
        for serial in S["serials"].values():
            self.assert_version(serial, 3)

    def test_4_preflight_skips(self):
        rc, out, _ = devdrop("install", apk("app-unsigned"), "--to", S["android"], "--json")
        self.assertEqual({r["reason"] for r in out["results"]}, {"APK is not signed"})
        rc, out, _ = devdrop("install", apk("app-x86only"), "--to", S["android"], "--json")
        for rec in out["results"]:
            self.assertEqual(rec["result"], "skipped")
            self.assertIn("build has x86_64", rec["reason"])
        rc, out, _ = devdrop("install", apk("app-min35"), "--to", S["android"], "--clean", "--json")
        for serial, rec in by_id(out).items():
            with self.subTest(serial=serial, api=S["api"][serial]):
                if S["api"][serial] >= 35:
                    self.assertEqual(rec["result"], "ok")
                else:
                    self.assertEqual(rec["reason"], f"API {S['api'][serial]} < minSdk 35")
        self.assertEqual(rc, 0 if all(S["api"][s] >= 35 for s in S["serials"].values()) else 1)

    def test_5_launcher_alias(self):
        rc, out, _ = devdrop("install", apk("app-alias"), "--to", S["android"], "--clean", "--launch", "--json")
        self.assertEqual(rc, 0, out)
        time.sleep(2)
        for serial in S["serials"].values():
            with self.subTest(serial=serial):
                top = adb(serial, "shell", "dumpsys", "activity", "activities")
                self.assertIn(PACKAGE, top)

    def test_5b_launch_without_cmd_package_uses_manifest_launcher(self):
        old = [s for s in S["serials"].values() if S["api"][s] < 24]
        if not old:
            self.skipTest("no device below API 24 in the matrix")
        for serial in old:
            # Precondition: this device really cannot resolve the launcher itself.
            probe = adb(serial, "shell", "cmd", "package", "resolve-activity", "--brief", PACKAGE)
            self.assertNotIn(f"{PACKAGE}/", probe)
        rc, out, _ = devdrop("install", apk("app-alias"), "--to", ",".join(old), "--clean", "--launch", "--json")
        self.assertEqual(rc, 0, out)
        time.sleep(2)
        for serial in old:
            self.assertIn(f"{PACKAGE}/.Main", adb(serial, "shell", "dumpsys", "activity", "activities"))

    def test_6_launch_uninstall_twice(self):
        devdrop("install", apk("app-v1"), "--to", S["android"], "--clean", "--json")
        rc, out, _ = devdrop("launch", PACKAGE, "--to", S["android"], "--json")
        self.assertEqual(rc, 0, out)
        rc, out, _ = devdrop("uninstall", PACKAGE, "--to", S["android"], "--json")
        self.assertEqual(rc, 0, out)
        for serial in S["serials"].values():
            self.assertNotIn(PACKAGE, adb(serial, "shell", "pm", "list", "packages", PACKAGE))
        rc, out, _ = devdrop("uninstall", PACKAGE, "--to", S["android"], "--json")
        self.assertEqual(rc, 1)
        self.assertEqual({r["reason"] for r in out["results"]}, {"not installed"})
        rc, out, _ = devdrop("launch", PACKAGE, "--to", S["android"], "--json")
        self.assertEqual({r["stage"] for r in out["results"]}, {"launch"})


@unittest.skipUnless(ENABLED and sys.platform == "darwin", "iOS simulators need macOS")
class C_Ios(unittest.TestCase):
    def installed(self, udid):
        return simctl("get_app_container", udid, PACKAGE).returncode == 0

    def running(self, udid):
        return PACKAGE in simctl("spawn", udid, "launchctl", "list").stdout

    def test_0_boot_timeout_was_reported(self):
        if "boot_timeout" not in S:
            self.skipTest("first simulator was already running")
        rec = S["boot_timeout"]
        self.assertEqual((rec["result"], rec["stage"]), ("failed", "boot"))
        self.assertIn("no answer after 1s", rec["reason"])

    def test_1_app_and_ipa_install_and_launch(self):
        sims = ",".join(S["sims"])
        rc, out, _ = devdrop("install", ios("ios-sim"), "--to", sims, "--clean", "--launch", "--json")
        self.assertEqual(rc, 0, out)
        for rec in out["results"]:
            self.assertEqual((rec["manufacturer"], rec["os_version"]), ("Apple", S["sim_os"][rec["id"]]))
        time.sleep(2)
        for udid in S["sims"]:
            self.assertTrue(self.installed(udid))
            self.assertTrue(self.running(udid))
        rc, out, _ = devdrop("install", ios("ios-sim", ipa=True), "--to", sims, "--clean", "--launch", "--json")
        self.assertEqual(rc, 0, out)

    def test_2_preflight_skips(self):
        sims = ",".join(S["sims"])
        cases = {
            ios("ios-sim-min99"): lambda u: f"iOS {S['sim_os'][u]} < minimum 99.0",
            ios("ios-sim-ipadonly"): lambda u: "iPad-only app -> iPhone",
            ios("ios-device", ipa=True): lambda u: "device build -> simulator",
        }
        for path, reason in cases.items():
            with self.subTest(path=os.path.basename(path)):
                rc, out, _ = devdrop("install", path, "--to", sims, "--json")
                self.assertEqual(rc, 1)
                for udid, rec in by_id(out).items():
                    self.assertEqual((rec["result"], rec["reason"]), ("skipped", reason(udid)))

    def test_3_launch_uninstall_twice(self):
        sims = ",".join(S["sims"])
        devdrop("install", ios("ios-sim"), "--to", sims, "--json")
        rc, out, _ = devdrop("launch", PACKAGE, "--to", sims, "--json")
        self.assertEqual(rc, 0, out)
        rc, out, _ = devdrop("uninstall", PACKAGE, "--to", sims, "--json")
        self.assertEqual(rc, 0, out)
        for udid in S["sims"]:
            self.assertFalse(self.installed(udid))
        rc, out, _ = devdrop("uninstall", PACKAGE, "--to", sims, "--json")
        self.assertEqual({r["reason"] for r in out["results"]}, {"not installed"})
        rc, out, _ = devdrop("launch", PACKAGE, "--to", sims, "--json")
        self.assertEqual(rc, 1)


class D_Everything(unittest.TestCase):
    def test_one_install_across_both_platforms_kinds(self):
        """devices, doctor and a mixed launch across every matrix device at once."""
        rc, out, _ = devdrop("devices", "--json")
        ids = {d["id"] for d in out["devices"]}
        for serial in S["serials"].values():
            self.assertIn(serial, ids)
        for udid in S["sims"]:
            self.assertIn(udid, ids)
        rc, _, r = devdrop("doctor", check_json=False)
        self.assertIn("devicectl found", r.stdout)
        devdrop("install", apk("app-v1"), "--to", S["android"], "--json")
        devdrop("install", ios("ios-sim"), "--to", ",".join(S["sims"]), "--json")
        rc, out, _ = devdrop("launch", PACKAGE, "--to", ",".join([S["android"]] + S["sims"]), "--json")
        self.assertEqual(rc, 0, out)
        self.assertEqual(out["ok"], len(S["serials"]) + len(S["sims"]))


if __name__ == "__main__":
    unittest.main()
