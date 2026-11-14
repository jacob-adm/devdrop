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
    return None


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
