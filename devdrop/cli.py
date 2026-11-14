# SPDX-License-Identifier: Apache-2.0
"""devdrop command line."""

import argparse
import json
import sys

from devdrop import __version__, android, artifact, config, deploy, ios
from devdrop.select import SelectorError, select
from devdrop.device import READY
from devdrop.tools import ToolError, find_adb, find_xcrun, run

OK, PARTIAL, USAGE, NO_DEVICES, NO_TOOLS = 0, 1, 2, 3, 4

HINTS = {
    ("android", "unauthorized"): "accept the USB debugging prompt on the device",
    ("android", "offline"): "reconnect the cable, or run: adb reconnect offline",
    ("android", "no-permissions"): "add a udev rule for the device (Linux) and replug it",
    ("android", "not-responding"): "the device did not answer; reconnect it",
    ("ios", "unpaired"): "connect by cable, unlock the device and tap Trust",
    ("ios", "offline"): "connect by cable, or join the same network as this computer",
    ("ios", "developer-mode-off"): "turn on Settings > Privacy & Security > Developer Mode",
}


def safe(text):
    """Escape control characters so device-supplied names cannot drive the terminal."""
    return "".join(c if c.isprintable() else f"\\x{ord(c):02x}" for c in str(text))


def _android_backend():
    adb = find_adb()
    if adb is None:
        return [], {"tool": None, "error": "adb not found (install Android SDK platform-tools)"}
    try:
        return android.list_devices(adb), {"tool": adb, "error": None}
    except ToolError as e:
        return [], {"tool": adb, "error": str(e)}


def _ios_backend(include_shutdown):
    xcrun = find_xcrun()
    if xcrun is None:
        return [], {"tool": None, "error": "unsupported: xcrun not found (needs macOS with Xcode)"}
    devices, errors = [], []
    if ios.has_devicectl(xcrun):
        try:
            devices += ios.list_physical(xcrun)
        except ToolError as e:
            errors.append(str(e))
    else:
        errors.append("devicectl not found (needs Xcode 15 or later); physical devices skipped")
    try:
        devices += ios.list_simulators(xcrun, include_shutdown)
    except ToolError as e:
        errors.append(str(e))
    return devices, {"tool": xcrun, "error": "; ".join(errors) or None}


def inventory(include_shutdown=False, platforms=("android", "ios")):
    """Devices from the requested backends, plus a status entry per backend."""
    devices, backends = [], {}
    if "android" in platforms:
        found, backends["android"] = _android_backend()
        devices += found
    if "ios" in platforms:
        found, backends["ios"] = _ios_backend(include_shutdown)
        devices += found
    return devices, backends


def _load_config(args):
    try:
        return config.load(args.config)
    except config.ConfigError as e:
        print(f"devdrop: {e}", file=sys.stderr)
        return None


def _name(cfg, dev):
    return cfg.alias_for(dev.id) or dev.name


def cmd_devices(args):
    cfg = _load_config(args)
    if cfg is None:
        return USAGE
    devices, backends = inventory(args.all)
    for dev in devices:
        dev.name = _name(cfg, dev)
    missing = all(b["tool"] is None for b in backends.values())
    failed = any(b["tool"] is not None and b["error"] for b in backends.values())
    if args.json:
        json.dump({"devices": [d.to_json() for d in devices], "backends": backends},
                  sys.stdout, indent=2)
        print()
    else:
        rows = [("ID", "NAME", "PLATFORM", "KIND", "MODEL", "OS", "STATE")]
        for d in devices:
            os_text = d.os_version + (f" (API {d.api})" if d.api else "")
            rows.append((d.id, d.name, d.platform, d.kind, d.model, os_text, d.state))
        if devices:
            widths = [max(len(safe(r[i])) for r in rows) for i in range(len(rows[0]))]
            for r in rows:
                print("  ".join(safe(v).ljust(w) for v, w in zip(r, widths)).rstrip())
        else:
            print("no devices")
        for platform, b in backends.items():
            if b["error"]:
                print(f"{platform}: {safe(b['error'])}", file=sys.stderr)
    if missing:
        return NO_TOOLS
    return PARTIAL if failed else OK


def _version_line(argv):
    try:
        r = run(argv)
    except ToolError as e:
        return None, str(e)
    lines = (r.out or r.err).strip().splitlines()
    # adb prints the protocol version first; the platform-tools release is on "Version ...".
    line = next((ln for ln in lines if ln.startswith("Version ")), lines[0] if lines else "")
    return line, None if r.rc == 0 else f"exit {r.rc}"


def cmd_doctor(args):
    cfg = _load_config(args)
    if cfg is None:
        return USAGE
    problems = 0

    def line(ok, text):
        print(f"{'ok ' if ok else '!! '} {safe(text)}")

    line(True, f"python {sys.version.split()[0]}")
    line(True, f"config: {cfg.path or 'none (optional)'}")
    adb = find_adb()
    if adb:
        version, err = _version_line([adb, "version"])
        line(err is None, f"adb {adb}: {err or version}")
        problems += err is not None
    else:
        line(False, "adb not found: install Android SDK platform-tools or set ANDROID_HOME")
        problems += 1
    xcrun = find_xcrun()
    if xcrun:
        version, err = _version_line(["xcodebuild", "-version"])
        line(err is None, f"xcode: {err or version}")
        problems += err is not None
        has = ios.has_devicectl(xcrun)
        line(has, "devicectl found" if has else "devicectl not found: needs Xcode 15 or later")
        problems += not has
    else:
        line(False, "xcrun not found: iOS needs macOS with Xcode (Android works without it)")
        problems += 1
    devices, backends = inventory()
    for platform, b in backends.items():
        if b["tool"] and b["error"]:
            line(False, f"{platform}: {b['error']}")
            problems += 1
    for d in devices:
        label = " ".join(x for x in (d.platform, d.id, _name(cfg, d) or d.model) if x)
        if d.state == READY:
            line(True, f"{label}: ready")
        else:
            hint = HINTS.get((d.platform, d.state), "")
            line(False, f"{label}: {d.state}" + (f" - {hint}" if hint else ""))
            problems += 1
    for alias, did in cfg.devices.items():
        if not any(d.id == did for d in devices):
            line(True, f"config alias {alias} ({did}): not attached")
    if adb is None and xcrun is None:
        return NO_TOOLS
    return PARTIAL if problems else OK


def cmd_inspect(args):
    try:
        report = artifact.inspect(args.path)
    except artifact.ArtifactError as e:
        print(f"devdrop: {safe(e)}", file=sys.stderr)
        return USAGE
    if args.json:
        json.dump(report, sys.stdout, indent=2)
        print()
        return OK
    profile = report.pop("profile", None)
    for key, value in report.items():
        if isinstance(value, list):
            value = ", ".join(map(str, value)) or "-"
        print(f"{key + ':':12} {safe(value) if value != '' else '-'}")
    if report["platform"] == "ios":
        if profile is None:
            print(f"{'profile:':12} none")
        else:
            devices = "all" if profile["all_devices"] else len(profile["devices"])
            status = "EXPIRED" if profile["expired"] else "valid"
            print(f"{'profile:':12} {safe(profile['name'])} ({profile['kind']}, team {profile['team']})")
            print(f"{'expires:':12} {profile['expires']} ({status})")
            print(f"{'devices:':12} {devices}")
            for udid in profile["devices"]:
                print(f"{'':12} {safe(udid)}")
    return OK


def _summary(records, as_json):
    counts = {k: sum(r["result"] == k for r in records) for k in ("ok", "skipped", "failed")}
    if as_json:
        json.dump({"results": records, **counts}, sys.stdout, indent=2)
        print()
    else:
        print(f"{counts['ok']} ok, {counts['skipped']} skipped, {counts['failed']} failed")
    return OK if counts["ok"] == len(records) else PARTIAL


def _print_record(rec):
    label = " ".join(x for x in (rec["id"], rec["name"]) if x)
    detail = f"{rec['stage']}: {rec['reason']}" if rec["reason"] else rec["stage"]
    print(f"{rec['result']:8} {safe(label)}  {safe(detail)}  ({rec['seconds']}s)", flush=True)


def _targets(args, cfg, platform):
    """Selected ready-or-not devices of one platform, or an exit code."""
    devices, backends = inventory(platforms=(platform,))
    b = backends[platform]
    if b["tool"] is None:
        print(f"devdrop: {safe(b['error'])}", file=sys.stderr)
        return NO_TOOLS
    if b["error"]:
        print(f"devdrop: {platform}: {safe(b['error'])}", file=sys.stderr)
    for d in devices:
        d.name = _name(cfg, d)
    try:
        chosen = select(devices, args.to, cfg)
    except SelectorError as e:
        print(f"devdrop: {e}", file=sys.stderr)
        return USAGE
    if not chosen:
        print(f"devdrop: no {platform} device matches --to {safe(args.to)}", file=sys.stderr)
        return NO_DEVICES
    return chosen, b["tool"]


def _backend(platform, tool):
    if platform == "android":
        return deploy.Android(tool)
    return None


def cmd_install(args):
    cfg = _load_config(args)
    if cfg is None:
        return USAGE
    try:
        report = artifact.inspect(args.path)
    except artifact.ArtifactError as e:
        print(f"devdrop: {safe(e)}", file=sys.stderr)
        return USAGE
    if report["platform"] == "ios":
        print("devdrop: installing iOS builds is not supported yet", file=sys.stderr)
        return USAGE
    found = _targets(args, cfg, report["platform"])
    if isinstance(found, int):
        return found
    chosen, tool = found
    backend = _backend(report["platform"], tool)
    records = deploy.fan_out(
        chosen, lambda d: deploy.install_one(backend, d, report, args.clean, args.launch),
        args.jobs, None if args.json else _print_record)
    return _summary(records, args.json)


def cmd_by_id(args):
    cfg = _load_config(args)
    if cfg is None:
        return USAGE
    found = _targets(args, cfg, "android")
    if isinstance(found, int):
        return found
    chosen, tool = found
    backend = _backend("android", tool)
    records = deploy.fan_out(
        chosen, lambda d: deploy.by_id_one(args.command, backend, d, args.app_id),
        args.jobs, None if args.json else _print_record)
    return _summary(records, args.json)


def _positive(text):
    n = int(text)
    if n < 1:
        raise argparse.ArgumentTypeError("must be 1 or more")
    return n


def _target_args(p):
    p.add_argument("--to", default="all", metavar="SEL",
                   help="devices to act on: all (default), android, ios, physical, emulator, "
                        "simulator, api>=N, ios>=X, an alias or group, an id, or a model pattern; "
                        "comma-separated terms are combined")
    p.add_argument("--jobs", type=_positive, metavar="N", help="devices handled at once (default: up to 8)")
    p.add_argument("--json", action="store_true", help="machine-readable output")


def build_parser():
    p = argparse.ArgumentParser(prog="devdrop",
                                description="Install one mobile build on every attached Android and iOS device.")
    p.add_argument("--version", action="version", version=f"devdrop {__version__}")
    p.add_argument("--config", metavar="PATH",
                   help="devdrop.toml to use (default: ./devdrop.toml, then ~/.config/devdrop/devdrop.toml)")
    sub = p.add_subparsers(dest="command", required=True)
    d = sub.add_parser("devices", help="list attached Android and iOS devices")
    d.add_argument("--json", action="store_true", help="machine-readable output")
    d.add_argument("--all", action="store_true", help="include simulators that are shut down")
    d.set_defaults(func=cmd_devices)
    i = sub.add_parser("inspect", help="show what an .apk, .ipa or .app contains")
    i.add_argument("path")
    i.add_argument("--json", action="store_true", help="machine-readable output")
    i.set_defaults(func=cmd_inspect)
    ins = sub.add_parser("install", help="install a build on every selected device")
    ins.add_argument("path")
    ins.add_argument("--launch", action="store_true", help="start the app after installing")
    ins.add_argument("--clean", action="store_true", help="uninstall the app first (removes its data)")
    _target_args(ins)
    ins.set_defaults(func=cmd_install)
    for name, text in (("launch", "start an installed app"), ("uninstall", "remove an app")):
        c = sub.add_parser(name, help=f"{text} on every selected device")
        c.add_argument("app_id", help="Android package name")
        _target_args(c)
        c.set_defaults(func=cmd_by_id)
    doc = sub.add_parser("doctor", help="check the toolchain and each device's state")
    doc.set_defaults(func=cmd_doctor)
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except KeyboardInterrupt:
        return 130
