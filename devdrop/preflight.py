# SPDX-License-Identifier: Apache-2.0
"""Why a build cannot go on a device, decided before trying."""

from devdrop.device import READY


def check(report, device):
    """A plain reason the install would fail, or None if it should work."""
    if device.platform != report["platform"]:
        return f"{report['type']} build cannot go on an {device.platform} device"
    if device.state != READY:
        return f"device is {device.state}"
    if device.platform == "android":
        return _android(report, device)
    return _ios(report, device)


def _android(report, device):
    if not report.get("signing"):
        return "APK is not signed"
    min_sdk = report.get("min_sdk")
    if isinstance(min_sdk, str):
        return f"preview build (minSdk {min_sdk}) only installs on that preview"
    if device.api is not None and isinstance(min_sdk, int) and device.api < min_sdk:
        return f"API {device.api} < minSdk {min_sdk}"
    abis = report.get("abis") or []
    if abis and device.abis and not set(abis) & set(device.abis):
        return f"no native code for {', '.join(device.abis)} (build has {', '.join(abis)})"
    return None


def _version(text):
    parts = []
    for p in str(text).split("."):
        if not p.isdigit():
            break
        parts.append(int(p))
    return tuple(parts) + (0,) * (3 - len(parts))


def _ios(report, device):
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
        return None
    profile = report.get("profile")
    if profile is None:
        return "no provisioning profile (build is not signed for devices)"
    if profile["kind"] == "app-store":
        return "App Store profile cannot install on devices"
    if profile["expired"]:
        return f"profile expired {profile['expires'][:10]}"
    if not profile["all_devices"] and device.id not in profile["devices"]:
        return "UDID not in provisioning profile"
    return None
