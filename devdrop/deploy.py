# SPDX-License-Identifier: Apache-2.0
"""Run install / launch / uninstall on many devices at once."""

import re
import time
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


class StepError(Exception):
    def __init__(self, stage, reason):
        super().__init__(reason)
        self.stage = stage
        self.reason = reason


def _record(dev, result, stage, reason, started):
    return {"id": dev.id, "name": dev.name, "platform": dev.platform, "result": result,
            "stage": stage, "reason": reason, "seconds": round(time.monotonic() - started, 1)}


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

    def _adb(self, dev, *args, timeout=60):
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
        lines = [ln.strip() for ln in r.out.splitlines() if ln.strip()]
        component = lines[-1] if r.rc == 0 and lines and "/" in lines[-1] else ""
        if not component and fallback:
            component = f"{package}/{fallback}"
        if not component:
            raise StepError("launch", "no launcher activity (is the app installed?)")
        r = self._adb(dev, "shell", "am", "start", "-n", component)
        out = r.out + r.err
        if r.rc != 0 or "Error" in out:
            line = next((ln for ln in out.splitlines() if "Error" in ln), out.strip())
            raise StepError("launch", line.strip() or f"am start exit {r.rc}")


def install_one(backend, dev, report, clean=False, launch=False):
    started = time.monotonic()
    reason = preflight.check(report, dev)
    if reason:
        return _record(dev, "skipped", "preflight", reason, started)
    try:
        if clean:
            backend.uninstall(dev, report["id"], missing_ok=True)
        backend.install(dev, report)
        if launch:
            backend.launch(dev, report["id"], report.get("launcher", ""))
    except StepError as e:
        return _record(dev, "failed", e.stage, e.reason, started)
    except ToolError as e:
        return _record(dev, "failed", "tool", str(e), started)
    return _record(dev, "ok", "launch" if launch else "install", "", started)


def by_id_one(action, backend, dev, app_id):
    started = time.monotonic()
    if dev.state != "ready":
        return _record(dev, "skipped", "preflight", f"device is {dev.state}", started)
    try:
        if action == "launch":
            backend.launch(dev, app_id)
        else:
            backend.uninstall(dev, app_id)
    except StepError as e:
        return _record(dev, "failed", e.stage, e.reason, started)
    except ToolError as e:
        return _record(dev, "failed", "tool", str(e), started)
    return _record(dev, "ok", action, "", started)


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
