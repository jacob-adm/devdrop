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

POLL = 2.0  # seconds between boot/shutdown checks


def find_emulator(env=None, home=None):
    """Path to the Android emulator: the SDK first, then PATH. None if absent."""
    name = "emulator.exe" if os.name == "nt" else "emulator"
    for root in sdk_roots(env, home):
        path = os.path.join(root, "emulator", name)
        if os.path.isfile(path) and os.access(path, os.X_OK):
            return path
    return shutil.which("emulator")


def avd_home(env=None):
    """Directory holding AVD definitions, honouring ANDROID_AVD_HOME and ANDROID_USER_HOME."""
    env = os.environ if env is None else env
    if env.get("ANDROID_AVD_HOME"):
        return env["ANDROID_AVD_HOME"]
    user = env.get("ANDROID_USER_HOME") or os.path.join(os.path.expanduser("~"), ".android")
    return os.path.join(user, "avd")


def _ini(path):
    """key=value lines of an AVD .ini file as a dict; an unreadable file gives {}."""
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
    # The system image path ("system-images/android-35/...") is most reliable; target= is the fallback.
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
    # Newer emulators mix log lines into -list-avds output; keep only AVD names.
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
        argv += ["-no-window", "-no-audio", "-no-boot-anim"]   # headless and faster
    log = open(log_path, "wb")
    kwargs = {"stdin": subprocess.DEVNULL, "stdout": log, "stderr": subprocess.STDOUT}
    # Detach from devdrop's terminal and process group so Ctrl-C or exit does not kill it.
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS
    else:
        kwargs["start_new_session"] = True
    try:
        return subprocess.Popen(argv, **kwargs)
    except OSError as e:
        raise ToolError(f"emulator: {e}")
    finally:
        log.close()                     # the child keeps its own handle


def _last_line(path):
    """Last non-blank line of a log file, or "" if unreadable: the likely error message."""
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
        # The emulator died (bad AVD, no disk, GPU crash): fail now with its last log line.
        if proc is not None and proc.poll() not in (None, 0):
            detail = _last_line(log_path) if log_path else ""
            raise ToolError(f"emulator exited with code {proc.returncode}" + (f": {detail}" if detail else ""))
        for d in list_devices():
            if d.kind == "emulator" and d.name == name and d.state == "ready":
                # "ready" in adb is not booted: Android sets sys.boot_completed=1 when the launcher can run.
                r = run([adb, "-s", d.id, "shell", "getprop", "sys.boot_completed"])
                if r.out.strip() == "1":
                    return d.id
        if time.monotonic() >= deadline:
            raise ToolError(f"{name} did not finish booting within {timeout}s")
        time.sleep(min(POLL, max(0.0, deadline - time.monotonic())))


def boot_simulator(xcrun, udid, timeout, show=False):
    """Boot a simulator and wait until it has finished booting; raises ToolError on failure.

    Already booted counts as success. show=True opens Simulator.app on it.
    """
    r = run([xcrun, "simctl", "boot", udid])
    if r.rc != 0 and "current state: Booted" not in r.err:
        raise ToolError(f"simctl boot: {r.err.strip() or 'exit ' + str(r.rc)}")
    # bootstatus -b blocks until the simulator is usable (boot alone returns early).
    r = run([xcrun, "simctl", "bootstatus", udid, "-b"], timeout=timeout)
    if r.rc != 0:
        raise ToolError(f"simctl bootstatus: {r.err.strip() or 'exit ' + str(r.rc)}")
    if show and sys.platform == "darwin":
        run(["open", "-a", "Simulator", "--args", "-CurrentDeviceUDID", udid])


def shutdown(adb, xcrun, dev, timeout=60):
    """Stop an emulator or simulator and return once it is really gone."""
    if dev.platform == "android":
        r = run([adb, "-s", dev.id, "emu", "kill"])
    else:
        r = run([xcrun, "simctl", "shutdown", dev.id])     # returns when shut down
    if r.rc != 0:
        raise ToolError((r.err or r.out).strip() or f"exit {r.rc}")
    if dev.platform == "android":
        # `emu kill` only asks the emulator to quit; wait until adb no longer lists it.
        deadline = time.monotonic() + timeout
        while dev.id in [ln.split()[0] for ln in run([adb, "devices"]).out.splitlines()[1:] if ln.strip()]:
            if time.monotonic() >= deadline:
                raise ToolError(f"{dev.id} still running {timeout}s after emu kill")
            time.sleep(min(POLL, max(0.0, deadline - time.monotonic())))
