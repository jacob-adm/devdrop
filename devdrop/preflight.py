# SPDX-License-Identifier: Apache-2.0
"""Why a build cannot go on a device, decided before trying."""

import re

from devdrop.device import READY


def check(report, device):
    """A plain reason the install would fail, or None if it should work."""
    # Order matters: the first reason found is the one reported.
    if device.platform != report["platform"]:
        return f"{report['type']} build cannot go on an {device.platform} device"
    if device.state != READY:
        return f"device is {device.state}"
    if device.platform == "android":
        return _android(report, device)
    return _ios(report, device)


def _android(report, device):
    """Android checks: signature (APK only), minSdk, and native ABIs."""
    # Android refuses unsigned APKs. An .aab is signed later, when bundletool builds its APKs.
    if report.get("type") == "apk" and not report.get("signing"):
        return "APK is not signed"
    min_sdk = report.get("min_sdk")
    if isinstance(min_sdk, str):        # codename such as "VanillaIceCream"
        return f"preview build (minSdk {min_sdk}) only installs on that preview"
    if device.api is not None and isinstance(min_sdk, int) and device.api < min_sdk:
        return f"API {device.api} < minSdk {min_sdk}"
    abis = report.get("abis") or []
    # A build with no native code (no abis) runs on any CPU.
    if abis and device.abis and not set(abis) & set(device.abis):
        return f"no native code for {', '.join(device.abis)} (build has {', '.join(abis)})"
    return None


SIGNATURE_API = 28   # Android 9: the first version whose dumpsys names the installed certificate


def installed(report, device, state, clean=False):
    """A reason the device will refuse this build because of what it already holds, or None.

    state is what the device reported (deploy.Android.installed): the
    installed copy's versionCode and signing certificates, and free space.
    Anything the device did not report is not judged. With clean the app is
    removed first, so its version and key no longer matter.
    """
    if not state or report.get("type") not in ("apk", "aab"):
        return None
    apk = report["type"] == "apk"
    if not clean:
        theirs, ours = state.get("cert_ids"), report.get("cert_ids")
        # An .aab is signed later; old Android prints a number that is not the certificate's.
        if apk and theirs and ours and (device.api or 0) >= SIGNATURE_API and not set(theirs) & set(ours):
            return "installed app is signed with a different key; use --clean"
        have, new = state.get("version_code"), str(report.get("build", ""))
        if have is not None and new.isdigit() and have > int(new):
            return f"installed app has a newer versionCode ({have} > {new}); use --clean"
        free = state.get("free_kb")
        # Android needs more room than the file itself, so this only catches the certain case.
        if apk and free is not None and report.get("size", 0) > free * 1024:
            return (f"not enough storage on the device ({free // 1024} MB free, "
                    f"build is {report['size'] // (1024 * 1024)} MB)")
    return None


def _version(text):
    """Version as a 3-part tuple for comparison: "17" -> (17, 0, 0), "18.5b" -> (18, 5, 0)."""
    parts = []
    for p in str(text).split("."):
        m = re.match(r"\d+", p)
        if not m:
            break
        parts.append(int(m.group()))
        if m.end() < len(p):        # "5b2": keep 5, stop at the suffix
            break
    return tuple(parts) + (0,) * (3 - len(parts))


def _ios(report, device):
    """iOS checks: build target, minimum iOS, iPad-only, and for devices the provisioning profile."""
    simulator = device.kind == "simulator"
    if report.get("target") == "simulator" and not simulator:
        return "simulator build -> physical device"
    if report.get("target") == "device" and simulator:
        return "device build -> simulator"
    if report.get("min_os") and device.os_version and \
            _version(device.os_version) < _version(report["min_os"]):
        return f"iOS {device.os_version} < minimum {report['min_os']}"
    if report.get("families") == ["ipad"] and "ipad" not in f"{device.model} {device.name}".lower():
        return "iPad-only app -> iPhone"
    if simulator:
        return None                     # simulators need no signing or profile
    profile = report.get("profile")
    if profile is None:
        return "no provisioning profile (build is not signed for devices)"
    if profile["kind"] == "app-store":
        return "App Store profile cannot install on devices"
    if profile["expired"]:
        return f"profile expired {profile['expires'][:10]}"
    # Enterprise profiles allow every device; others list UDIDs.
    if not profile["all_devices"] and device.id not in profile["devices"]:
        return "UDID not in provisioning profile"
    return None


def warnings(report, device):
    """Things that will not stop the install but that the operator should know."""
    page = report.get("page_16k") or {}
    # Only a 16 KB Android device with a build known not to be ready gets a warning.
    if device.platform != "android" or device.page_size != 16384 or page.get("ready", True):
        return []
    bad = [f"{lib['path']} ({_why(lib)})" for lib in page.get("libraries", []) if not lib["ready"]]
    return ["16 KB page-size device: native code is not 16 KB ready, so the app runs in "
            "page-size compatibility mode or may fail to load: " + ", ".join(bad)]


def _why(lib):
    """Why one native library is not 16 KB ready, in words."""
    if lib["elf_align"] is None:
        return "not a readable 64-bit ELF"
    if lib["elf_align"] < 16384:
        return f"ELF aligned to {lib['elf_align'] // 1024} KB"
    return "not on a 16 KB boundary in the APK"
