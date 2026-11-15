# SPDX-License-Identifier: Apache-2.0
"""The device record shared by both platforms."""

from dataclasses import asdict, dataclass, field

# States. Only READY devices take installs; every other state names what
# the operator has to do.
READY = "ready"


@dataclass
class Device:
    """One attached device, emulator or simulator, as every command sees it.

    Built by the platform listers (android.py, ios.py, virtual.py); read by
    selection, preflight checks, deploy steps and every report.
    """

    id: str                 # adb serial, or iOS UDID
    platform: str           # "android" | "ios"
    kind: str               # "physical" | "emulator" | "simulator"
    state: str
    model: str = ""
    manufacturer: str = ""  # "Samsung", "Google", "Apple", ...
    os_version: str = ""
    api: int | None = None  # Android API level
    abis: list = field(default_factory=list)  # Android CPU ABIs, preferred first
    page_size: int | None = None  # Android: memory page size in bytes (16384 on 16 KB devices)
    serial_number: str = ""  # Android ro.serialno: the same phone over USB and Wi-Fi shares it
    transport: str = ""     # "usb" | "wifi" | "local" | ""
    name: str = ""          # alias from devdrop.toml, else the device's own name

    def to_json(self):
        """Every field as a plain dict, for --json output."""
        return asdict(self)
