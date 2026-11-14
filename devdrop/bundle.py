# SPDX-License-Identifier: Apache-2.0
"""Install Android App Bundles through bundletool, one device at a time.

For each device bundletool builds the APK set that device needs
(`build-apks --connected-device`) and installs it (`install-apks`).
Keystore passwords are handed over in files readable only by the user,
so they never appear in a process list.
"""

import os
import re
import shutil
from dataclasses import dataclass

from devdrop.tools import run

INSTALL_TIMEOUT = 600
DEBUG_KEYSTORE = os.path.join("~", ".android", "debug.keystore")


class BundleError(Exception):
    pass


class BundletoolMissing(BundleError):
    """bundletool (or the java it needs) is not installed."""


@dataclass
class Signing:
    keystore: str
    alias: str
    ks_pass_file: str
    key_pass_file: str


def find_bundletool(env=None):
    """argv prefix for bundletool: $BUNDLETOOL_JAR with java, else bundletool on PATH."""
    env = os.environ if env is None else env
    jar = env.get("BUNDLETOOL_JAR")
    if jar:
        java = shutil.which("java", path=env.get("PATH"))
        if not os.path.isfile(jar):
            raise BundletoolMissing(f"BUNDLETOOL_JAR={jar}: not found")
        if java is None:
            raise BundletoolMissing("BUNDLETOOL_JAR is set but java is not on PATH")
        return [java, "-jar", jar]
    found = shutil.which("bundletool", path=env.get("PATH"))
    if found is None:
        raise BundletoolMissing("bundletool not found: install it (for example `brew install bundletool`) "
                          "or set BUNDLETOOL_JAR")
    return [found]


def _secret_file(directory, name, value):
    path = os.path.join(directory, name)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(value)
    return path


def signing(keystore, alias, workdir, env=None):
    """Signing for the APKs built from a bundle.

    --ks with --ks-alias and DEVDROP_KS_PASS (DEVDROP_KEY_PASS defaults to it);
    without --ks, the Android debug keystore if it exists. devdrop never creates keys.
    """
    env = os.environ if env is None else env
    if keystore:
        if not alias:
            raise BundleError("--ks needs --ks-alias")
        if not os.path.isfile(keystore):
            raise BundleError(f"--ks {keystore}: not found")
        ks_pass = env.get("DEVDROP_KS_PASS")
        if ks_pass is None:
            raise BundleError("--ks needs the keystore password in DEVDROP_KS_PASS")
        key_pass = env.get("DEVDROP_KEY_PASS", ks_pass)
    else:
        keystore = os.path.expanduser(DEBUG_KEYSTORE)
        if not os.path.isfile(keystore):
            raise BundleError("an .aab needs a signing key: pass --ks and --ks-alias, or create the Android "
                              f"debug keystore at {DEBUG_KEYSTORE}")
        alias, ks_pass, key_pass = "androiddebugkey", "android", "android"
    return Signing(keystore, alias, _secret_file(workdir, "ks.pass", ks_pass),
                   _secret_file(workdir, "key.pass", key_pass))


def failure(text):
    """bundletool's own error line, without its version prefix."""
    for line in text.splitlines():
        m = re.search(r"Error: (.+)", line)
        if m:
            return m.group(1).strip()
    return None


def build_apks(bundletool, adb, aab, serial, sign, out):
    argv = bundletool + [
        "build-apks", f"--bundle={aab}", f"--output={out}", "--overwrite", "--connected-device",
        f"--device-id={serial}", f"--adb={adb}", f"--ks={sign.keystore}", f"--ks-key-alias={sign.alias}",
        f"--ks-pass=file:{sign.ks_pass_file}", f"--key-pass=file:{sign.key_pass_file}"]
    return run(argv, timeout=INSTALL_TIMEOUT)


def install_apks(bundletool, adb, apks, serial):
    argv = bundletool + ["install-apks", f"--apks={apks}", f"--device-id={serial}", f"--adb={adb}"]
    return run(argv, timeout=INSTALL_TIMEOUT)
