# SPDX-License-Identifier: Apache-2.0
"""Find the platform tools and run them."""

import os
import shutil
import subprocess
import sys
from dataclasses import dataclass

TIMEOUT = 30


class ToolError(Exception):
    """A platform tool could not be run or did not answer in time."""


@dataclass
class Result:
    rc: int
    out: str
    err: str


def run(argv, timeout=TIMEOUT):
    try:
        p = subprocess.run(argv, capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=timeout)
    except FileNotFoundError:
        raise ToolError(f"{argv[0]}: not found")
    except subprocess.TimeoutExpired:
        raise ToolError(f"{' '.join(argv[:2])}: no answer after {timeout}s")
    return Result(p.returncode, p.stdout, p.stderr)


def sdk_roots(env=None, home=None):
    """Android SDK locations in the order they are tried."""
    env = os.environ if env is None else env
    home = os.path.expanduser("~") if home is None else home
    roots = [env.get("ANDROID_HOME"), env.get("ANDROID_SDK_ROOT")]
    if sys.platform == "darwin":
        roots.append(os.path.join(home, "Library", "Android", "sdk"))
    elif os.name == "nt":
        local = env.get("LOCALAPPDATA")
        roots.append(os.path.join(local, "Android", "Sdk") if local else None)
    else:
        roots.append(os.path.join(home, "Android", "Sdk"))
    return [r for r in roots if r]


def find_adb(env=None, home=None):
    """Path to adb: the Android SDK first, then PATH. None if absent."""
    name = "adb.exe" if os.name == "nt" else "adb"
    for root in sdk_roots(env, home):
        path = os.path.join(root, "platform-tools", name)
        if os.path.isfile(path) and os.access(path, os.X_OK):
            return path
    return shutil.which("adb")


def find_xcrun():
    """Path to xcrun (Xcode command line tools). None if absent."""
    return shutil.which("xcrun")
