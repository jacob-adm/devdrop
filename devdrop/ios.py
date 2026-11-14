# SPDX-License-Identifier: Apache-2.0
"""iOS devices through xcrun devicectl (physical) and xcrun simctl (simulators)."""

import json
import os
import tempfile

from devdrop.device import READY, Device
from devdrop.tools import ToolError, run


def parse_devicectl(doc):
    """Devices from `devicectl list devices --json-output` (iPhone and iPad only)."""
    devices = []
    for d in doc.get("result", {}).get("devices", []):
        hw = d.get("hardwareProperties", {})
        props = d.get("deviceProperties", {})
        conn = d.get("connectionProperties", {})
        if hw.get("platform") != "iOS" or hw.get("reality", "physical") != "physical":
            continue
        if conn.get("pairingState") != "paired":
            state = "unpaired"
        elif conn.get("tunnelState") == "unavailable":
            state = "offline"
        elif props.get("developerModeStatus") == "disabled":
            state = "developer-mode-off"
        else:
            state = READY
        transport = {"wired": "usb", "localNetwork": "wifi"}.get(conn.get("transportType", ""), "")
        cpu = [c.get("name") for c in hw.get("supportedCPUTypes", []) if c.get("name")]
        devices.append(Device(
            id=hw.get("udid") or d.get("identifier", ""),
            platform="ios",
            kind="physical",
            state=state,
            model=hw.get("marketingName") or hw.get("productType", ""),
            os_version=props.get("osVersionNumber", ""),
            abis=cpu,
            transport=transport,
            name=props.get("name", ""),
        ))
    return devices


def parse_simctl(doc, include_shutdown=False):
    """iOS simulators from `simctl list -j devices available`."""
    devices = []
    for runtime, sims in doc.get("devices", {}).items():
        tail = runtime.rsplit(".", 1)[-1]          # e.g. "iOS-18-2"
        if not tail.startswith("iOS-"):
            continue
        version = tail[4:].replace("-", ".")
        for s in sims:
            if not s.get("isAvailable", True):
                continue
            booted = s.get("state") == "Booted"
            if not booted and not include_shutdown:
                continue
            devices.append(Device(
                id=s.get("udid", ""),
                platform="ios",
                kind="simulator",
                state=READY if booted else "shutdown",
                model=s.get("name", ""),
                os_version=version,
                transport="local",
                name=s.get("name", ""),
            ))
    return devices


def has_devicectl(xcrun):
    try:
        return run([xcrun, "--find", "devicectl"]).rc == 0
    except ToolError:
        return False


def list_physical(xcrun):
    fd, path = tempfile.mkstemp(prefix="devdrop-", suffix=".json")
    os.close(fd)
    try:
        res = run([xcrun, "devicectl", "list", "devices", "--json-output", path, "-q"])
        if res.rc != 0:
            raise ToolError(f"devicectl list devices: {res.err.strip() or 'exit ' + str(res.rc)}")
        try:
            with open(path, encoding="utf-8") as f:
                doc = json.load(f)
        except ValueError as e:
            raise ToolError(f"devicectl list devices: unreadable JSON ({e})")
    finally:
        os.unlink(path)
    return parse_devicectl(doc)


def list_simulators(xcrun, include_shutdown=False):
    res = run([xcrun, "simctl", "list", "-j", "devices", "available"])
    if res.rc != 0:
        raise ToolError(f"simctl list: {res.err.strip() or 'exit ' + str(res.rc)}")
    try:
        doc = json.loads(res.out)
    except ValueError as e:
        raise ToolError(f"simctl list: unreadable JSON ({e})")
    return parse_simctl(doc, include_shutdown)
