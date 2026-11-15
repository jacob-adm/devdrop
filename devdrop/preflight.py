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
