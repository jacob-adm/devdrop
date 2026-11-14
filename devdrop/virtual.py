# SPDX-License-Identifier: Apache-2.0
"""Start and stop Android emulators and iOS simulators."""

import os
import re
import shutil
import subprocess
import sys
import time

from devdrop.device import Device
from devdrop.tools import ToolError, run, sdk_roots

POLL = 2.0


def find_emulator(env=None, home=None):
    name = "emulator.exe" if os.name == "nt" else "emulator"
    for root in sdk_roots(env, home):
        path = os.path.join(root, "emulator", name)
        if os.path.isfile(path) and os.access(path, os.X_OK):
            return path
    return shutil.which("emulator")


def avd_home(env=None):
    env = os.environ if env is None else env
    if env.get("ANDROID_AVD_HOME"):
        return env["ANDROID_AVD_HOME"]
    user = env.get("ANDROID_USER_HOME") or os.path.join(os.path.expanduser("~"), ".android")
    return os.path.join(user, "avd")


def _ini(path):
    values = {}
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                key, sep, value = line.partition("=")
                if sep:
                    values[key.strip()] = value.strip()
    except OSError:
        pass
    return values


def avd_api(name, home=None):
    """API level of an AVD from its config, or None."""
    home = home or avd_home()
    top = _ini(os.path.join(home, f"{name}.ini"))
    config = _ini(os.path.join(top.get("path") or os.path.join(home, f"{name}.avd"), "config.ini"))
    for text in (config.get("image.sysdir.1", ""), top.get("target", "")):
        m = re.search(r"android-(\d+)", text)
        if m:
            return int(m.group(1))
    return None


def stopped_avds(emulator, running_names):
    """Device records for AVDs that are not running."""
    r = run([emulator, "-list-avds"])
    if r.rc != 0:
        raise ToolError(f"emulator -list-avds: {r.err.strip() or 'exit ' + str(r.rc)}")
    names = [ln.strip() for ln in r.out.splitlines()
             if ln.strip() and not ln.startswith(("INFO", "WARNING", "ERROR"))]
    return [Device(id=n, platform="android", kind="emulator", state="shutdown", api=avd_api(n),
                   transport="local", name=n)
            for n in names if n not in running_names]


def start_avd(emulator, name, log_path, show=False):
    """Start an emulator in the background; it keeps running after devdrop exits.

    Output goes to log_path so an early failure can be reported.
    """
    argv = [emulator, "-avd", name]
    if not show:
        argv += ["-no-window", "-no-audio", "-no-boot-anim"]
    log = open(log_path, "wb")
    kwargs = {"stdin": subprocess.DEVNULL, "stdout": log, "stderr": subprocess.STDOUT}
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS
    else:
        kwargs["start_new_session"] = True
    try:
        return subprocess.Popen(argv, **kwargs)
    except OSError as e:
        raise ToolError(f"emulator: {e}")
    finally:
        log.close()


def _last_line(path):
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            lines = [ln.strip() for ln in f if ln.strip()]
    except OSError:
        return ""
    return lines[-1] if lines else ""


def wait_for_avd(adb, name, timeout, list_devices, proc=None, log_path=None):
    """Wait until an emulator running AVD `name` reports boot completed; return its serial."""
    deadline = time.monotonic() + timeout
    while True:
        if proc is not None and proc.poll() not in (None, 0):
            detail = _last_line(log_path) if log_path else ""
            raise ToolError(f"emulator exited with code {proc.returncode}" + (f": {detail}" if detail else ""))
        for d in list_devices():
            if d.kind == "emulator" and d.name == name and d.state == "ready":
                r = run([adb, "-s", d.id, "shell", "getprop", "sys.boot_completed"])
                if r.out.strip() == "1":
                    return d.id
        if time.monotonic() >= deadline:
            raise ToolError(f"{name} did not finish booting within {timeout}s")
        time.sleep(min(POLL, max(0.0, deadline - time.monotonic())))


def boot_simulator(xcrun, udid, timeout, show=False):
    r = run([xcrun, "simctl", "boot", udid])
    if r.rc != 0 and "current state: Booted" not in r.err:
        raise ToolError(f"simctl boot: {r.err.strip() or 'exit ' + str(r.rc)}")
    r = run([xcrun, "simctl", "bootstatus", udid, "-b"], timeout=timeout)
    if r.rc != 0:
        raise ToolError(f"simctl bootstatus: {r.err.strip() or 'exit ' + str(r.rc)}")
    if show and sys.platform == "darwin":
        run(["open", "-a", "Simulator", "--args", "-CurrentDeviceUDID", udid])


def shutdown(adb, xcrun, dev):
    if dev.platform == "android":
        r = run([adb, "-s", dev.id, "emu", "kill"])
    else:
        r = run([xcrun, "simctl", "shutdown", dev.id])
    if r.rc != 0:
        raise ToolError((r.err or r.out).strip() or f"exit {r.rc}")
