# SPDX-License-Identifier: Apache-2.0
"""The device record shared by both platforms."""

from dataclasses import asdict, dataclass, field

# States. Only READY devices take installs; every other state names what
# the operator has to do.
READY = "ready"


@dataclass
class Device:
    id: str                 # adb serial, or iOS UDID
    platform: str           # "android" | "ios"
    kind: str               # "physical" | "emulator" | "simulator"
    state: str
    model: str = ""
    manufacturer: str = ""  # "Samsung", "Google", "Apple", ...
    os_version: str = ""
    api: int | None = None  # Android API level
    abis: list = field(default_factory=list)
    transport: str = ""     # "usb" | "wifi" | "local" | ""
    name: str = ""          # alias from devdrop.toml, else the device's own name

    def to_json(self):
        return asdict(self)
