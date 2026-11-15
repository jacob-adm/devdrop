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

_PROP = re.compile(r"^\[([^\]]*)\]: \[(.*)\]$")   # one `getprop` line: [key]: [value]


def parse_devices(text):
    """Parse `adb devices -l` into Device records (no properties yet)."""
    devices = []
    for line in text.splitlines():
        line = line.strip()
        # Skip the header and adb's own "* daemon started" chatter.
        if not line or line.startswith("List of devices") or line.startswith("*"):
            continue
        serial = line.split()[0]
        rest = line[len(serial):].strip()
        # Linux without udev rules: adb prints a sentence here instead of a state word.
        if rest.startswith("no permissions"):
            state, attrs = "no-permissions", {}
        else:
            word, _, tail = rest.partition(" ")
            state = _STATES.get(word, word or "unknown")
            attrs = dict(t.split(":", 1) for t in tail.split() if ":" in t)  # model:Pixel_8 usb:1-1 ...
        devices.append(Device(
            id=serial,
            platform="android",
            kind="emulator" if serial.startswith("emulator-") else "physical",
            state=state,
            model=attrs.get("model", "").replace("_", " "),
            # host:port, or an mDNS name, means the device is connected over Wi-Fi.
            transport="wifi" if (":" in serial or "._adb-tls-connect." in serial) else "usb",
        ))
    return devices


def parse_getprop(text):
    """`adb shell getprop` output as a {property: value} dict; other lines are ignored."""
    props = {}
    for line in text.splitlines():
        m = _PROP.match(line.strip())
        if m:
            props[m.group(1)] = m.group(2)
    return props


def _apply_props(dev, props):
    """Fill a Device from its system properties: model, maker, OS, API level, ABIs, AVD name."""
    dev.model = props.get("ro.product.model") or dev.model
    dev.manufacturer = props.get("ro.product.manufacturer", "")
    dev.serial_number = props.get("ro.serialno") or props.get("ro.boot.serialno", "")
    dev.os_version = props.get("ro.build.version.release", "")
    sdk = props.get("ro.build.version.sdk", "")
    dev.api = int(sdk) if sdk.isdigit() else None   # missing or odd value: unknown, not 0
    dev.abis = [a for a in props.get("ro.product.cpu.abilist", "").split(",") if a]
    # An emulator reached over TCP has no "emulator-" serial; qemu properties still say what it is.
    if props.get("ro.kernel.qemu") == "1" or props.get("ro.boot.qemu") == "1":
        dev.kind = "emulator"
    if dev.kind == "emulator":
        # AVD name: ro.boot.* on newer images, ro.kernel.* on older ones.
        dev.name = props.get("ro.boot.qemu.avd_name") or props.get("ro.kernel.qemu.avd_name") or dev.name


def list_devices(adb):
    """Every device adb sees, ready ones filled in with their properties; Wi-Fi duplicates dropped.

    Raises ToolError when adb itself fails. A device that does not answer
    getprop is kept and marked "not-responding" rather than failing the list.
    """
    res = run([adb, "devices", "-l"])
    if res.rc != 0:
        raise ToolError(f"adb devices: {res.err.strip() or 'exit ' + str(res.rc)}")
    devices = parse_devices(res.out)

    def fill(dev):
        """Read one device's properties; on any failure mark it not-responding."""
        try:
            r = run([adb, "-s", dev.id, "shell", "getprop"])
        except ToolError:
            dev.state = "not-responding"
            return
        if r.rc != 0:
            dev.state = "not-responding"
            return
        _apply_props(dev, parse_getprop(r.out))
        if dev.api is not None and dev.api >= 35:          # 16 KB pages exist from Android 15
            try:
                page = run([adb, "-s", dev.id, "shell", "getconf", "PAGE_SIZE"]).out.strip()
            except ToolError:
                page = ""
            dev.page_size = int(page) if page.isdigit() else None

    # Query devices in parallel (at most 8 at once): one slow phone must not stall the list.
    ready = [d for d in devices if d.state == READY]
    if ready:
        with ThreadPoolExecutor(max_workers=min(8, len(ready))) as pool:
            list(pool.map(fill, ready))
    return one_per_device(devices)


def one_per_device(devices):
    """Drop a Wi-Fi entry that is the same device as a USB or emulator entry.

    Matching is deliberately narrow: ro.serialno is not unique everywhere (every
    emulator reports the emulator version, and some cheap phones share
    0123456789ABCDEF), so only a Wi-Fi entry is ever dropped, and only when
    serial, model and (for emulators) AVD name all match a wired entry.
    """
    def key(d):
        """Identity of a physical device across transports."""
        return (d.serial_number, d.model, d.name if d.kind == "emulator" else "")

    wired = {key(d) for d in devices if d.state == READY and d.serial_number and d.transport != "wifi"}
    return [d for d in devices
            if not (d.state == READY and d.transport == "wifi" and d.serial_number and key(d) in wired)]


def parse_mdns(text):
    """(service name, type, host:port) from `adb mdns services`."""
    services = []
    for line in text.splitlines():
        parts = line.split()
        if len(parts) >= 3 and parts[1].startswith("_adb") and ":" in parts[2]:
            services.append((parts[0], parts[1].rstrip("."), parts[2]))
    return services


def pair(adb, address, code):
    """Pair with a phone's wireless debugging; returns adb's confirmation line or raises ToolError."""
    r = run([adb, "pair", address, code])
    out = (r.out + r.err).strip()
    # adb can exit 0 on failure, so success is read from its text.
    if r.rc != 0 or "Successfully paired" not in out:
        raise ToolError(out.splitlines()[-1] if out else f"adb pair exit {r.rc}")
    return out.splitlines()[-1]


def connect(adb, address):
    """Connect to one host:port; returns adb's confirmation line or raises ToolError."""
    r = run([adb, "connect", address])
    out = (r.out + r.err).strip()
    # adb exits 0 even for "failed to connect", so the message decides.
    if r.rc != 0 or not (out.startswith("connected to") or out.startswith("already connected")):
        raise ToolError(out.splitlines()[-1] if out else f"adb connect exit {r.rc}")
    return out.splitlines()[-1]


def discover(adb):
    """Addresses of devices advertising wireless debugging (connect, not pairing)."""
    r = run([adb, "mdns", "services"])
    if r.rc != 0:
        raise ToolError(f"adb mdns services: {(r.err or r.out).strip() or 'exit ' + str(r.rc)}")
    # _adb-tls-pairing._tcp entries are phones waiting for a pairing code; skip them.
    return [addr for _, kind, addr in parse_mdns(r.out) if kind == "_adb-tls-connect._tcp"]
