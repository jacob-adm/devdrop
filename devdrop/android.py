# SPDX-License-Identifier: Apache-2.0
"""Android devices through adb."""

import re
from concurrent.futures import ThreadPoolExecutor

from devdrop.device import READY, Device
from devdrop.tools import ToolError, run

# adb's own state words, mapped to devdrop states.
_STATES = {
    "device": READY,
    "unauthorized": "unauthorized",
    "offline": "offline",
    "authorizing": "authorizing",
    "connecting": "connecting",
    "recovery": "recovery",
    "rescue": "recovery",
    "sideload": "sideload",
    "bootloader": "bootloader",
}

_PROP = re.compile(r"^\[([^\]]*)\]: \[(.*)\]$")


def parse_devices(text):
    """Parse `adb devices -l` into Device records (no properties yet)."""
    devices = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("List of devices") or line.startswith("*"):
            continue
        serial = line.split()[0]
        rest = line[len(serial):].strip()
        if rest.startswith("no permissions"):
            state, attrs = "no-permissions", {}
        else:
            word, _, tail = rest.partition(" ")
            state = _STATES.get(word, word or "unknown")
            attrs = dict(t.split(":", 1) for t in tail.split() if ":" in t)
        devices.append(Device(
            id=serial,
            platform="android",
            kind="emulator" if serial.startswith("emulator-") else "physical",
            state=state,
            model=attrs.get("model", "").replace("_", " "),
            transport="wifi" if (":" in serial or "._adb-tls-connect." in serial) else "usb",
        ))
    return devices


def parse_getprop(text):
    props = {}
    for line in text.splitlines():
        m = _PROP.match(line.strip())
        if m:
            props[m.group(1)] = m.group(2)
    return props


def _apply_props(dev, props):
    dev.model = props.get("ro.product.model") or dev.model
    dev.manufacturer = props.get("ro.product.manufacturer", "")
    dev.os_version = props.get("ro.build.version.release", "")
    sdk = props.get("ro.build.version.sdk", "")
    dev.api = int(sdk) if sdk.isdigit() else None
    dev.abis = [a for a in props.get("ro.product.cpu.abilist", "").split(",") if a]
    if props.get("ro.kernel.qemu") == "1" or props.get("ro.boot.qemu") == "1":
        dev.kind = "emulator"
    if dev.kind == "emulator":
        dev.name = props.get("ro.boot.qemu.avd_name") or props.get("ro.kernel.qemu.avd_name") or dev.name


def list_devices(adb):
    res = run([adb, "devices", "-l"])
    if res.rc != 0:
        raise ToolError(f"adb devices: {res.err.strip() or 'exit ' + str(res.rc)}")
    devices = parse_devices(res.out)

    def fill(dev):
        try:
            r = run([adb, "-s", dev.id, "shell", "getprop"])
        except ToolError:
            dev.state = "not-responding"
            return
        if r.rc == 0:
            _apply_props(dev, parse_getprop(r.out))
        else:
            dev.state = "not-responding"

    ready = [d for d in devices if d.state == READY]
    if ready:
        with ThreadPoolExecutor(max_workers=min(8, len(ready))) as pool:
            list(pool.map(fill, ready))
    return devices
