# SPDX-License-Identifier: Apache-2.0
"""Run install / launch / uninstall on many devices at once."""

import os
import re
import stat
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed

from devdrop import preflight
from devdrop.tools import ToolError, run

INSTALL_TIMEOUT = 600

# adb failure codes and what to do about them.
ANDROID_FAILURES = {
    "INSTALL_FAILED_UPDATE_INCOMPATIBLE": "installed app is signed with a different key; use --clean",
    "INSTALL_FAILED_VERSION_DOWNGRADE": "installed app has a newer versionCode; use --clean",
    "INSTALL_FAILED_OLDER_SDK": "device API level is below the app's minSdk",
    "INSTALL_FAILED_NO_MATCHING_ABIS": "no native code for this device's CPU",
    "INSTALL_FAILED_INSUFFICIENT_STORAGE": "not enough storage on the device",
    "INSTALL_FAILED_USER_RESTRICTED": "install over USB was refused on the device; allow it in developer options",
    "INSTALL_FAILED_TEST_ONLY": "test-only APK refused",
    "INSTALL_PARSE_FAILED_NO_CERTIFICATES": "APK is not signed",
    "INSTALL_FAILED_ALREADY_EXISTS": "app already installed; use --clean",
}
_FAILURE = re.compile(r"Failure \[([A-Z_]+)(?::[^\]]*)?\]|(INSTALL_[A-Z_]+)")
# package/activity as printed by `cmd package resolve-activity --brief`.
_COMPONENT = re.compile(r"^[A-Za-z][\w.]*/[\w.$]+$")


class StepError(Exception):
    def __init__(self, stage, reason):
        super().__init__(reason)
        self.stage = stage
        self.reason = reason


def record(dev, result, stage, reason, started):
    return {"id": dev.id, "name": dev.name, "platform": dev.platform, "kind": dev.kind,
            "manufacturer": dev.manufacturer, "model": dev.model, "os_version": dev.os_version,
            "api": dev.api, "result": result, "stage": stage, "reason": reason,
            "seconds": round(time.monotonic() - started, 1)}


def android_failure(text):
    m = _FAILURE.search(text)
    if not m:
        tail = text.strip().splitlines()
        return tail[-1] if tail else "unknown error"
    code = m.group(1) or m.group(2)
    return f"{code}: {ANDROID_FAILURES[code]}" if code in ANDROID_FAILURES else code


class Android:
    def __init__(self, adb):
        self.adb = adb

    def _adb(self, dev, *args, timeout=None):
        return run([self.adb, "-s", dev.id, *args], timeout=timeout)

    def install(self, dev, report):
        r = self._adb(dev, "install", "-r", "-t", report["path"], timeout=INSTALL_TIMEOUT)
        out = r.out + r.err
        if r.rc != 0 or "Success" not in out:
            raise StepError("install", android_failure(out))

    def uninstall(self, dev, package, missing_ok=False):
        r = self._adb(dev, "uninstall", package)
        out = r.out + r.err
        if "Success" in out:
            return
        if missing_ok and ("DELETE_FAILED_INTERNAL_ERROR" in out or "Unknown package" in out):
            return
        reason = "not installed" if "DELETE_FAILED_INTERNAL_ERROR" in out or "Unknown package" in out \
            else android_failure(out)
        raise StepError("uninstall", reason)

    def launch(self, dev, package, fallback=""):
        r = self._adb(dev, "shell", "cmd", "package", "resolve-activity", "--brief",
                      "-c", "android.intent.category.LAUNCHER", package)
        # Old Android (API 23 and below) has no `cmd`; its shell still exits 0 and
        # prints "/system/bin/sh: cmd: not found", so check the shape, not the exit code.
        lines = [ln.strip() for ln in r.out.splitlines() if ln.strip()]
        component = lines[-1] if lines and _COMPONENT.match(lines[-1]) else ""
        if not component and fallback:
            component = f"{package}/{fallback}"
        if not component:
            # Last resort on any Android version: let monkey fire the launcher intent.
            r = self._adb(dev, "shell", "monkey", "-p", package, "-c", "android.intent.category.LAUNCHER", "1")
            if "Events injected: 1" in r.out + r.err:
                return
            raise StepError("launch", "no launcher activity (is the app installed?)")
        r = self._adb(dev, "shell", "am", "start", "-n", component)
        out = r.out + r.err
        if r.rc != 0 or "Error" in out:
            line = next((ln for ln in out.splitlines() if "Error" in ln), out.strip())
            raise StepError("launch", line.strip() or f"am start exit {r.rc}")


IOS_HINTS = (
    ("Developer Mode", "turn on Developer Mode on the device"),
    ("locked", "unlock the device and retry"),
    ("provisioning profile", "the provisioning profile does not allow this device or app"),
    ("not installed", "not installed"),
)


def ios_failure(text):
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    first = next((ln for ln in lines if ln.startswith("ERROR:")), lines[-1] if lines else "unknown error")
    first = first.removeprefix("ERROR:").strip()
    for needle, hint in IOS_HINTS:
        if needle.lower() in text.lower() and hint not in first:
            return f"{first} ({hint})"
    return first


def unpack_ipa(ipa, dest):
    """Extract Payload/*.app from an IPA into dest, keeping file modes. Returns the .app path."""
    with zipfile.ZipFile(ipa) as z:
        app = None
        for info in z.infolist():
            parts = info.filename.split("/")
            if len(parts) < 2 or parts[0] != "Payload" or not parts[1].endswith(".app"):
                continue
            app = app or parts[1]
            if parts[1] != app or ".." in parts or info.filename.startswith("/"):
                continue
            target = os.path.join(dest, *parts[1:])
            if info.is_dir():
                os.makedirs(target, exist_ok=True)
                continue
            os.makedirs(os.path.dirname(target), exist_ok=True)
            with z.open(info) as src, open(target, "wb") as out:
                out.write(src.read())
            mode = (info.external_attr >> 16) & 0o777
            if stat.S_IFMT(info.external_attr >> 16) != stat.S_IFLNK and mode:
                os.chmod(target, mode)
    if app is None:
        raise StepError("unpack", "no Payload/*.app in the IPA")
    return os.path.join(dest, app)


class Ios:
    def __init__(self, xcrun):
        self.xcrun = xcrun

    def _run(self, *args, timeout=120):
        return run([self.xcrun, *args], timeout=timeout)

    def _check(self, stage, r):
        if r.rc != 0:
            raise StepError(stage, ios_failure(r.err or r.out))

    def install(self, dev, report):
        app = report.get("app_path") or report["path"]
        if dev.kind == "simulator":
            r = self._run("simctl", "install", dev.id, app, timeout=INSTALL_TIMEOUT)
        else:
            r = self._run("devicectl", "device", "install", "app", "--device", dev.id, app,
                          timeout=INSTALL_TIMEOUT)
        self._check("install", r)

    def uninstall(self, dev, bundle_id, missing_ok=False):
        if dev.kind == "simulator":
            installed = self._run("simctl", "get_app_container", dev.id, bundle_id).rc == 0
            if not installed:
                if missing_ok:
                    return
                raise StepError("uninstall", "not installed")
            r = self._run("simctl", "uninstall", dev.id, bundle_id)
        else:
            r = self._run("devicectl", "device", "uninstall", "app", "--device", dev.id, bundle_id)
            if r.rc != 0 and missing_ok:
                return
        self._check("uninstall", r)

    def launch(self, dev, bundle_id, fallback=""):
        if dev.kind == "simulator":
            r = self._run("simctl", "launch", "--terminate-running-process", dev.id, bundle_id)
        else:
            r = self._run("devicectl", "device", "process", "launch", "--device", dev.id,
                          "--terminate-existing", bundle_id)
        self._check("launch", r)


def install_one(backend, dev, report, clean=False, launch=False):
    started = time.monotonic()
    reason = preflight.check(report, dev)
    if reason:
        return record(dev, "skipped", "preflight", reason, started)
    try:
        if clean:
            backend.uninstall(dev, report["id"], missing_ok=True)
        backend.install(dev, report)
        if launch:
            backend.launch(dev, report["id"], report.get("launcher", ""))
    except StepError as e:
        return record(dev, "failed", e.stage, e.reason, started)
    except ToolError as e:
        return record(dev, "failed", "tool", str(e), started)
    return record(dev, "ok", "launch" if launch else "install", "", started)


def by_id_one(action, backend, dev, app_id):
    started = time.monotonic()
    if dev.state != "ready":
        return record(dev, "skipped", "preflight", f"device is {dev.state}", started)
    try:
        if action == "launch":
            backend.launch(dev, app_id)
        else:
            backend.uninstall(dev, app_id)
    except StepError as e:
        return record(dev, "failed", e.stage, e.reason, started)
    except ToolError as e:
        return record(dev, "failed", "tool", str(e), started)
    return record(dev, "ok", action, "", started)


def fan_out(devices, work, jobs=None, on_done=None):
    """Run work(device) on each device in parallel; records come back in device order."""
    if not devices:
        return []
    jobs = max(1, jobs or min(8, len(devices)))
    results = {}
    with ThreadPoolExecutor(max_workers=jobs) as pool:
        futures = {pool.submit(work, d): i for i, d in enumerate(devices)}
        for f in as_completed(futures):
            rec = f.result()
            results[futures[f]] = rec
            if on_done:
                on_done(rec)
    return [results[i] for i in range(len(devices))]
