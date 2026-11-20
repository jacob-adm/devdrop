# SPDX-License-Identifier: Apache-2.0
"""Find the platform tools and run them."""

import math
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass

DEFAULT_TIMEOUT = 30  # seconds for one adb/xcrun call; installs and boots pass their own


def default_timeout(env=None):
    """Seconds devdrop waits for one adb/xcrun call (DEVDROP_TOOL_TIMEOUT overrides)."""
    env = os.environ if env is None else env
    try:
        value = float(env.get("DEVDROP_TOOL_TIMEOUT", DEFAULT_TIMEOUT))
    except ValueError:
        return DEFAULT_TIMEOUT          # not a number: fall back, never crash
    # Reject nan, inf, zero and negatives: each would mean "never" or "instantly".
    return value if math.isfinite(value) and value > 0 else DEFAULT_TIMEOUT


class ToolError(Exception):
    """A platform tool could not be run or did not answer in time."""


@dataclass
class Result:
    """Exit code and decoded output of one finished tool run."""

    rc: int
    out: str
    err: str


def run(argv, timeout=None):
    """Run a tool and capture its output; never raises on a non-zero exit.

    Raises ToolError when the program is missing or exceeds the timeout,
    so callers handle "could not ask" apart from "asked and it said no".
    """
    timeout = default_timeout() if timeout is None else timeout
    try:
        # errors="replace": device output is not always valid UTF-8; a bad byte must not crash a run.
        p = subprocess.run(argv, capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=timeout)
    except FileNotFoundError:
        raise ToolError(f"{argv[0]}: not found")
    except subprocess.TimeoutExpired:
        raise ToolError(f"{' '.join(argv[:2])}: no answer after {timeout}s")
    return Result(p.returncode, p.stdout, p.stderr)


def sdk_roots(env=None, home=None, system=None):
    """Android SDK locations in the order they are tried."""
    env = os.environ if env is None else env
    home = os.path.expanduser("~") if home is None else home
    system = system or ("windows" if os.name == "nt" else sys.platform)
    # Explicit settings win; then each OS's Android Studio default location.
    roots = [env.get("ANDROID_HOME"), env.get("ANDROID_SDK_ROOT")]
    if system == "darwin":
        roots.append(os.path.join(home, "Library", "Android", "sdk"))
    elif system == "windows":
        local = env.get("LOCALAPPDATA")
        roots.append(os.path.join(local, "Android", "Sdk") if local else None)
    else:
        roots.append(os.path.join(home, "Android", "Sdk"))
    return [r for r in roots if r]      # unset entries dropped


def find_adb(env=None, home=None):
    """Path to adb: the Android SDK first, then PATH. None if absent."""
    name = "adb.exe" if os.name == "nt" else "adb"
    for root in sdk_roots(env, home):
        path = os.path.join(root, "platform-tools", name)
        if os.path.isfile(path) and os.access(path, os.X_OK):
            return path
    return shutil.which("adb")


def find_pymobiledevice3():
    """Path to pymobiledevice3, an optional tool for a physical iPhone's log and screenshot. None if absent."""
    return shutil.which("pymobiledevice3")


def start(argv, out_path):
    """Start a tool that keeps running, its output going to out_path. Returns the process.

    Raises ToolError when the program is missing.
    """
    try:
        with open(out_path, "wb") as out:
            return subprocess.Popen(argv, stdout=out, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL)
    except OSError as e:
        raise ToolError(f"{argv[0]}: {e.strerror or e}")


def find_xcrun():
    """Path to xcrun (Xcode command line tools). None if absent."""
    return shutil.which("xcrun")
