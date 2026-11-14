# SPDX-License-Identifier: Apache-2.0
"""--to selectors: which devices a command acts on.

A selector is a comma-separated list; a device is chosen when any term
matches it. Terms:

    all                       every device
    android, ios              by platform
    physical, emulator, simulator (or sim)
    api>=N  api<N  ...        Android API level (operators: >= <= > < =)
    ios>=X.Y ...              iOS version
    an alias or group from devdrop.toml
    a device id, or a device's exact model or name (case-insensitive)
    a pattern with * or ?     matched against model and name
"""

import fnmatch
import operator
import re

KINDS = {"physical": "physical", "emulator": "emulator", "simulator": "simulator", "sim": "simulator"}
_OPS = {">=": operator.ge, "<=": operator.le, ">": operator.gt, "<": operator.lt, "=": operator.eq}
_CMP = re.compile(r"^(api|ios)(>=|<=|>|<|=)(\d+(?:\.\d+)*)$")


class SelectorError(ValueError):
    pass


def _version(text):
    parts = []
    for p in str(text).split("."):
        m = re.match(r"\d+", p)
        if not m:
            break
        parts.append(int(m.group()))
        if m.end() < len(p):        # "5b2": keep 5, stop at the suffix
            break
    return tuple(parts)


def _term(term, cfg):
    """A predicate for one term, or None if the term names nothing known."""
    t = term.strip()
    low = t.lower()
    if low == "all":
        return lambda d: True
    if low in ("android", "ios"):
        return lambda d: d.platform == low
    if low in KINDS:
        return lambda d: d.kind == KINDS[low]
    m = _CMP.match(low)
    if m:
        field, op, value = m.group(1), _OPS[m.group(2)], m.group(3)
        if field == "api":
            return lambda d: d.platform == "android" and d.api is not None and op(d.api, int(value))
        want = _version(value)
        n = len(want)

        def ios_cmp(d):
            if d.platform != "ios" or not _version(d.os_version):
                return False
            have = (_version(d.os_version) + (0,) * n)[:n]
            return op(have, want)
        return ios_cmp
    if t in cfg.groups:
        ids = {cfg.devices[a] for a in cfg.groups[t]}
        return lambda d: d.id in ids
    if t in cfg.devices:
        return lambda d: d.id == cfg.devices[t]
    if any(c in t for c in "*?["):
        return lambda d: fnmatch.fnmatchcase(d.model.lower(), low) or fnmatch.fnmatchcase(d.name.lower(), low)
    return lambda d: d.id == t or low in (d.model.lower(), d.name.lower())


def select(devices, selector, cfg):
    """Devices matching the selector, in input order."""
    terms = [t for t in (selector or "all").split(",") if t.strip()]
    if not terms:
        raise SelectorError("empty --to selector")
    preds = [_term(t, cfg) for t in terms]
    return [d for d in devices if any(p(d) for p in preds)]
