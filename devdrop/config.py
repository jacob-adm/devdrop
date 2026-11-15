# SPDX-License-Identifier: Apache-2.0
"""devdrop.toml: device aliases and groups.

    [devices]
    pixel8 = "37201FDH2000CK"
    iphone = "00008150-0000000000000000"

    [groups]
    lab = ["pixel8", "iphone"]
"""

import os
import tomllib
from dataclasses import dataclass, field

NAME = "devdrop.toml"  # looked for in the current directory, then the user config directory


class ConfigError(Exception):
    """devdrop.toml is missing, unreadable or invalid; the message names the file and the key."""


@dataclass
class Config:
    """Validated aliases and groups. An empty Config means no file was found."""

    path: str = ""
    devices: dict = field(default_factory=dict)   # alias -> id
    groups: dict = field(default_factory=dict)    # group -> [alias]

    def alias_for(self, device_id):
        """The alias that names this device id, or "" when it has none."""
        for alias, did in self.devices.items():
            if did == device_id:
                return alias
        return ""


def default_paths(cwd=None, env=None):
    """Where devdrop.toml is looked for, first match wins: ./devdrop.toml, then
    $XDG_CONFIG_HOME/devdrop/devdrop.toml (default ~/.config)."""
    env = os.environ if env is None else env
    base = env.get("XDG_CONFIG_HOME") or os.path.join(os.path.expanduser("~"), ".config")
    return [os.path.join(cwd or os.getcwd(), NAME), os.path.join(base, "devdrop", NAME)]


def load(path=None, cwd=None, env=None):
    """Load the explicit path, else the first default path that exists."""
    if path is None:
        path = next((p for p in default_paths(cwd, env) if os.path.isfile(p)), None)
        if path is None:
            return Config()             # no file anywhere: run with no aliases
    try:
        with open(path, "rb") as f:
            data = tomllib.load(f)
    except FileNotFoundError:
        raise ConfigError(f"{path}: not found")
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"{path}: {e}")
    return parse(data, path)


def parse(data, path=""):
    """Validate parsed TOML and build a Config; raise ConfigError on the first problem.

    Rejects unknown tables, empty or non-string ids, two aliases for one id,
    a group named like an alias, and groups naming unknown aliases.
    """
    unknown = set(data) - {"devices", "groups"}
    if unknown:
        raise ConfigError(f"{path}: unknown table(s): {', '.join(sorted(unknown))}")
    devices = data.get("devices", {})
    groups = data.get("groups", {})
    if not isinstance(devices, dict) or not isinstance(groups, dict):
        raise ConfigError(f"{path}: [devices] and [groups] must be tables")
    for alias, did in devices.items():
        if not isinstance(did, str) or not did:
            raise ConfigError(f"{path}: devices.{alias} must be a non-empty string")
    seen = {}                           # id -> first alias, to catch duplicates
    for alias, did in devices.items():
        if did in seen:
            raise ConfigError(f"{path}: devices.{alias} and devices.{seen[did]} name the same id")
        seen[did] = alias
    for group, members in groups.items():
        if group in devices:
            raise ConfigError(f"{path}: groups.{group} has the same name as a device alias")
        if not isinstance(members, list) or not all(isinstance(m, str) for m in members):
            raise ConfigError(f"{path}: groups.{group} must be a list of aliases")
        missing = [m for m in members if m not in devices]
        if missing:
            raise ConfigError(f"{path}: groups.{group} names unknown alias(es): {', '.join(missing)}")
    return Config(path=path, devices=dict(devices), groups={g: list(m) for g, m in groups.items()})
