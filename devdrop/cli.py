# SPDX-License-Identifier: Apache-2.0
"""devdrop command line: parse arguments, find devices, run a command, report, exit.

Every command returns one of the exit codes below; main() hands it to sys.exit.
"""

import argparse
import json
import os
import sys
import tempfile
import time
import zipfile

from devdrop import __version__, android, artifact, bundle, config, deploy, ios, junit, preflight, virtual
from devdrop.select import SelectorError, select
from devdrop.device import READY
from devdrop.tools import ToolError, find_adb, find_xcrun, run

# Exit codes (documented in docs/commands.md). PARTIAL: some device was skipped or failed.
OK, PARTIAL, USAGE, NO_DEVICES, NO_TOOLS = 0, 1, 2, 3, 4

# (platform, device state) -> the fix `doctor` prints next to a device that is not ready.
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


def _android_backend(include_shutdown=False):
    """Android devices and backend status {tool, error}. Missing adb is a status, not a crash."""
    adb = find_adb()
    if adb is None:
        return [], {"tool": None, "error": "adb not found (install Android SDK platform-tools)"}
    try:
        devices = android.list_devices(adb)
    except ToolError as e:
        return [], {"tool": adb, "error": str(e)}
    error = None
    if include_shutdown:
        emulator = virtual.find_emulator()
        if emulator:
            running = {d.name for d in devices if d.kind == "emulator"}
            try:
                devices += virtual.stopped_avds(emulator, running)
            except ToolError as e:
                error = str(e)
    return devices, {"tool": adb, "error": error}


def _ios_backend(include_shutdown):
    """iOS devices and simulators, and backend status. Each half fails on its own:
    no devicectl still lists simulators, a simctl error still lists phones."""
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
        found, backends["android"] = _android_backend(include_shutdown)
        devices += found
    if "ios" in platforms:
        found, backends["ios"] = _ios_backend(include_shutdown)
        devices += found
    return devices, backends


def _load_config(args):
    """devdrop.toml as a Config, or None after printing why it is invalid."""
    try:
        return config.load(args.config)
    except config.ConfigError as e:
        print(f"devdrop: {e}", file=sys.stderr)
        return None


def _name(cfg, dev):
    """Display name: the devdrop.toml alias when one exists, else the device's own name."""
    return cfg.alias_for(dev.id) or dev.name


def cmd_devices(args):
    """`devices`: one table (or JSON) of every device; backend errors go to stderr.

    Exit NO_TOOLS when neither adb nor xcrun exists, PARTIAL when a tool failed.
    """
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
        rows = [("ID", "NAME", "PLATFORM", "KIND", "MAKER", "MODEL", "OS", "STATE")]
        for d in devices:
            extra = ", ".join(x for x in (f"API {d.api}" if d.api else "",
                                          "16 KB pages" if d.page_size == 16384 else "") if x)
            os_text = " ".join(x for x in (d.os_version, f"({extra})" if extra else "") if x)
            rows.append((d.id, d.name, d.platform, d.kind, d.manufacturer, d.model, os_text, d.state))
        if devices:
            # Column widths from the widest cell, so the table lines up in a terminal.
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
    """(version text, error) from a tool's version command; error is None when it ran cleanly."""
    try:
        r = run(argv)
    except ToolError as e:
        return None, str(e)
    lines = (r.out or r.err).strip().splitlines()
    # adb prints the protocol version first; the platform-tools release is on "Version ...".
    line = next((ln for ln in lines if ln.startswith("Version ")), lines[0] if lines else "")
    return line, None if r.rc == 0 else f"exit {r.rc}"


def cmd_doctor(args):
    """`doctor`: check Python, config, adb, Xcode and devicectl, then every device's state.

    Prints "ok" or "!!" per line with a fix; exit PARTIAL when anything needs fixing.
    """
    cfg = _load_config(args)
    if cfg is None:
        return USAGE
    problems = 0                        # each "!!" line adds one

    def line(ok, text):
        """Print one check: "ok " or "!! " then the text."""
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
    # An alias whose device is not attached is normal (phone in a drawer): informational only.
    for alias, did in cfg.devices.items():
        if not any(d.id == did for d in devices):
            line(True, f"config alias {alias} ({did}): not attached")
    if adb is None and xcrun is None:
        return NO_TOOLS
    return PARTIAL if problems else OK


def cmd_inspect(args):
    """`inspect`: print what a build contains (or --json). An unreadable build exits USAGE."""
    try:
        report = artifact.inspect(args.path)
    except artifact.ArtifactError as e:
        print(f"devdrop: {safe(e)}", file=sys.stderr)
        return USAGE
    if args.json:
        json.dump(report, sys.stdout, indent=2)
        print()
        return OK
    # Nested parts get their own layout below; everything else prints as key: value.
    profile = report.pop("profile", None)
    page = report.pop("page_16k", None)
    for key, value in report.items():
        if key == "signing" and value is None:
            value = "at install (bundletool)"
        if isinstance(value, list):
            value = ", ".join(map(str, value)) or "-"
        print(f"{key + ':':12} {safe(value) if value != '' else '-'}")
    if page is not None:
        if not page["native_64bit"]:
            print(f"{'16k_pages:':12} ready (no 64-bit native code)")
        elif page["ready"]:
            print(f"{'16k_pages:':12} ready")
        else:
            print(f"{'16k_pages:':12} NOT ready")
            for lib in page["libraries"]:
                if not lib["ready"]:
                    print(f"{'':12} {safe(lib['path'])}: {preflight._why(lib)}")
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


def _summary(records, args):
    """Write --junit if asked, then print the counts (or JSON) and return the exit code.

    A JUnit write error is reported but does not change the result of the run.
    """
    if getattr(args, "junit", None):
        try:
            junit.write(args.junit, args.command, records)
        except OSError as e:
            print(f"devdrop: --junit {safe(args.junit)}: {safe(e)}", file=sys.stderr)
    return _counts(records, args.json)


def _counts(records, as_json):
    """Print "N ok, N skipped, N failed" (or the JSON results); OK only if every device is ok."""
    counts = {k: sum(r["result"] == k for r in records) for k in ("ok", "skipped", "failed")}
    if as_json:
        json.dump({"results": records, **counts}, sys.stdout, indent=2)
        print()
    else:
        print(f"{counts['ok']} ok, {counts['skipped']} skipped, {counts['failed']} failed")
    return OK if counts["ok"] == len(records) else PARTIAL


def _print_record(rec):
    """One result line per device, then any warnings and the log path; flushed immediately."""
    label = " ".join(x for x in (rec["id"], rec["name"]) if x)
    detail = f"{rec['stage']}: {rec['reason']}" if rec["reason"] else rec["stage"]
    print(f"{rec['result']:8} {safe(label)}  {safe(detail)}  ({rec['seconds']}s)", flush=True)
    for note in rec.get("warnings", []):
        print(f"{'':8} warning: {safe(note)}", flush=True)
    if rec.get("log"):
        print(f"{'':8} log: {safe(rec['log'])}", flush=True)
    if rec.get("screenshot"):
        print(f"{'':8} screenshot: {safe(rec['screenshot'])}", flush=True)


WATCH_POLL = 2.0  # seconds between device scans in install --watch


def _targets(args, cfg, platforms, allow_empty=False):
    """Selected devices on the given platforms and the tool for each, or an exit code."""
    devices, backends = inventory(platforms=platforms)
    tools = {p: b["tool"] for p, b in backends.items() if b["tool"]}
    for p, b in backends.items():
        if b["error"] and (b["tool"] or len(platforms) == 1):
            print(f"devdrop: {p}: {safe(b['error'])}", file=sys.stderr)
    if not tools:
        return NO_TOOLS
    for d in devices:
        d.name = _name(cfg, d)
    try:
        chosen = select(devices, args.to, cfg)
    except SelectorError as e:
        print(f"devdrop: {e}", file=sys.stderr)
        return USAGE
    if not chosen and not allow_empty:
        which = platforms[0] if len(platforms) == 1 else "attached"
        print(f"devdrop: no {which} device matches --to {safe(args.to)}", file=sys.stderr)
        return NO_DEVICES
    return chosen, {p: _backend(p, t) for p, t in tools.items()}


def _backend(platform, tool, bundle_setup=None):
    """The deploy backend for a platform, bound to its tool path."""
    return deploy.Android(tool, bundle_setup) if platform == "android" else deploy.Ios(tool)


def _boot_one(adb, xcrun, dev, timeout, show):
    """Boot one stopped emulator or simulator and wait for it; never raises, returns a record."""
    started = time.monotonic()
    try:
        if dev.platform == "android":
            emulator = virtual.find_emulator()
            if emulator is None:
                raise ToolError("emulator not found (install the Android SDK emulator package)")
            # Emulator output goes to a temp log so an early crash can be reported with its cause.
            log_path = os.path.join(tempfile.gettempdir(), f"devdrop-emulator-{dev.name}.log")
            proc = virtual.start_avd(emulator, dev.name, log_path, show)
            serial = virtual.wait_for_avd(adb, dev.name, timeout, lambda: android.list_devices(adb),
                                          proc, log_path)
            reason = f"running as {serial}"
        else:
            virtual.boot_simulator(xcrun, dev.id, timeout, show)
            reason = ""
    except ToolError as e:
        return deploy.record(dev, "failed", "boot", str(e), started)
    return deploy.record(dev, "ok", "boot", reason, started)


def _boot(args, cfg, platforms):
    """Boot the stopped virtual devices --to names. Returns (records, exit code or None)."""
    devices, backends = inventory(include_shutdown=True, platforms=platforms)
    for d in devices:
        # Stopped AVDs keep their AVD name: that is what `emulator -avd` needs.
        d.name = d.name if d.kind == "emulator" and d.state == "shutdown" else _name(cfg, d)
    try:
        chosen = select(devices, args.to, cfg)
    except SelectorError as e:
        print(f"devdrop: {e}", file=sys.stderr)
        return [], USAGE
    stopped = [d for d in chosen if d.state == "shutdown" and d.kind in ("emulator", "simulator")]
    if not chosen:
        print(f"devdrop: no device matches --to {safe(args.to)}", file=sys.stderr)
        return [], NO_DEVICES
    adb = (backends.get("android") or {}).get("tool")
    xcrun = (backends.get("ios") or {}).get("tool")
    records = deploy.fan_out(stopped, lambda d: _boot_one(adb, xcrun, d, args.boot_timeout, args.show),
                             args.jobs, None if args.json else _print_record)
    return records, None


def cmd_boot(args):
    """`boot`: start the stopped emulators and simulators --to names; running ones are left alone."""
    cfg = _load_config(args)
    if cfg is None:
        return USAGE
    records, code = _boot(args, cfg, ("android", "ios"))
    if code is not None:
        return code
    if not records and not args.json:
        print("nothing to boot: every matching device is already running")
    return _summary(records, args)


def cmd_shutdown(args):
    """`shutdown`: stop running emulators and simulators --to names. Physical devices are never touched."""
    cfg = _load_config(args)
    if cfg is None:
        return USAGE
    found = _targets(args, cfg, ("android", "ios"))
    if isinstance(found, int):
        return found
    chosen, backends = found
    virtual_devs = [d for d in chosen if d.kind in ("emulator", "simulator")]
    if not virtual_devs:
        print("devdrop: --to matches no running emulator or simulator", file=sys.stderr)
        return NO_DEVICES
    adb = backends["android"].adb if "android" in backends else None
    xcrun = backends["ios"].xcrun if "ios" in backends else None

    def stop(dev):
        """Stop one virtual device; returns a record."""
        started = time.monotonic()
        try:
            virtual.shutdown(adb, xcrun, dev)
        except ToolError as e:
            return deploy.record(dev, "failed", "shutdown", str(e), started)
        return deploy.record(dev, "ok", "shutdown", "", started)
    records = deploy.fan_out(virtual_devs, stop, args.jobs, None if args.json else _print_record)
    return _summary(records, args)


def cmd_install(args):
    """`install`: read the build, pick devices of its platform, install on all at once, report.

    Steps: inspect -> optional --boot -> select devices -> prepare (.aab signing,
    .ipa unpack) in a temp dir removed afterwards -> fan out -> optional --watch -> summary.
    """
    cfg = _load_config(args)
    if cfg is None:
        return USAGE
    try:
        report = artifact.inspect(args.path)
    except artifact.ArtifactError as e:
        print(f"devdrop: {safe(e)}", file=sys.stderr)
        return USAGE
    booted = []
    if args.boot:
        booted, code = _boot(args, cfg, (report["platform"],))
        if code is not None:
            return code
    if args.logs and not args.launch:
        print("devdrop: --logs needs --launch", file=sys.stderr)
        return USAGE
    if args.screenshot and not args.launch:
        print("devdrop: --screenshot needs --launch", file=sys.stderr)
        return USAGE
    log = _launch_log(args)
    shot = _launch_shot(args)
    # Only the build's platform is queried: an APK never waits on Xcode, an IPA never on adb.
    found = _targets(args, cfg, (report["platform"],), allow_empty=args.watch)
    if isinstance(found, int):
        return found
    chosen, backends = found
    backend = backends[report["platform"]]
    # With --watch, devices not ready or still booting are picked up later instead of reported now.
    chosen = [d for d in chosen if d.state == READY and backend.booted(d)] if args.watch else chosen
    with tempfile.TemporaryDirectory(prefix="devdrop-") as tmp:
        if report["type"] == "aab":
            try:
                backend.bundle = (bundle.find_bundletool(), bundle.signing(args.ks, args.ks_alias, tmp), tmp)
            except bundle.BundleError as e:
                print(f"devdrop: {safe(e)}", file=sys.stderr)
                return NO_TOOLS if isinstance(e, bundle.BundletoolMissing) else USAGE
        if report["type"] == "ipa":
            try:
                report["app_path"] = deploy.unpack_ipa(report["path"], tmp)
            except (deploy.StepError, OSError, zipfile.BadZipFile) as e:
                print(f"devdrop: {safe(args.path)}: {safe(e)}", file=sys.stderr)
                return USAGE
        def work(dev):
            """Install on one device with this run's options."""
            return deploy.install_one(backend, dev, report, args.clean, args.launch, log, shot)
        records = deploy.fan_out(chosen, work, args.jobs, None if args.json else _print_record)
        if args.watch:
            records += _watch(args, cfg, report["platform"], backend, {r["id"] for r in records}, work)
    if args.watch and not records:
        print("devdrop: no device appeared", file=sys.stderr)
        return NO_DEVICES
    # Failed boots count against the run; successful boots are not install results.
    return _summary([r for r in booted if r["result"] != "ok"] + records, args)


def _watch(args, cfg, platform, backend, handled, work):
    """Install on each matching device that becomes ready, once, until Ctrl-C or the timeout."""
    deadline = time.monotonic() + args.watch_timeout if args.watch_timeout else None
    if not args.json:
        print("watching for devices (Ctrl-C to stop)", flush=True)
    records = []
    try:
        while deadline is None or time.monotonic() < deadline:
            time.sleep(WATCH_POLL if deadline is None else max(0.0, min(WATCH_POLL, deadline - time.monotonic())))
            devices, _ = inventory(platforms=(platform,))
            for d in devices:
                d.name = _name(cfg, d)
            # A device still booting is left for a later scan, not installed on and not marked handled.
            new = [d for d in select(devices, args.to, cfg)
                   if d.state == READY and d.id not in handled and backend.booted(d)]
            if new:
                handled.update(d.id for d in new)
                records += deploy.fan_out(new, work, args.jobs, None if args.json else _print_record)
    except KeyboardInterrupt:
        pass                            # Ctrl-C ends the watch normally: summary and exit code follow
    return records


def cmd_pair(args):
    """`pair`: pair with an Android phone's wireless debugging using the code it shows."""
    adb = find_adb()
    if adb is None:
        print("devdrop: adb not found (install Android SDK platform-tools)", file=sys.stderr)
        return NO_TOOLS
    try:
        print(safe(android.pair(adb, args.address, args.code)))
    except ToolError as e:
        print(f"devdrop: pair {safe(args.address)}: {safe(e)}", file=sys.stderr)
        return PARTIAL
    return OK


def cmd_connect(args):
    """`connect`: connect to the given addresses, or to every paired phone found by mDNS."""
    adb = find_adb()
    if adb is None:
        print("devdrop: adb not found (install Android SDK platform-tools)", file=sys.stderr)
        return NO_TOOLS
    try:
        addresses = args.address or android.discover(adb)
    except ToolError as e:
        print(f"devdrop: {safe(e)}", file=sys.stderr)
        return PARTIAL
    if not addresses:
        print("devdrop: no device is advertising wireless debugging (pair it first)", file=sys.stderr)
        return NO_DEVICES
    failed = 0
    for address in addresses:
        try:
            print(safe(android.connect(adb, address)))
        except ToolError as e:
            print(f"devdrop: connect {safe(address)}: {safe(e)}", file=sys.stderr)
            failed += 1
    return PARTIAL if failed else OK


def cmd_by_id(args):
    """`launch` and `uninstall`: act on an installed app by package name or bundle id."""
    cfg = _load_config(args)
    if cfg is None:
        return USAGE
    found = _targets(args, cfg, ("android", "ios"))
    if isinstance(found, int):
        return found
    chosen, backends = found
    chosen = [d for d in chosen if d.platform in backends]   # drop devices whose tool is missing
    log = _launch_log(args) if args.command == "launch" else None
    shot = _launch_shot(args) if args.command == "launch" else None
    records = deploy.fan_out(
        chosen, lambda d: deploy.by_id_one(args.command, backends[d.platform], d, args.app_id, log, shot),
        args.jobs, None if args.json else _print_record)
    return _summary(records, args)


def _launch_log(args):
    """LaunchLog for --logs (creating the directory), or None when not asked for."""
    if not getattr(args, "logs", None):
        return None
    os.makedirs(args.logs, exist_ok=True)
    return deploy.LaunchLog(args.logs, args.log_seconds)


def _launch_shot(args):
    """Screenshot settings for --screenshot (creating the directory), or None when not asked for."""
    if not getattr(args, "screenshot", None):
        return None
    os.makedirs(args.screenshot, exist_ok=True)
    return deploy.Screenshot(args.screenshot, args.screenshot_seconds)


def _log_args(p):
    """Add --logs, --log-seconds, --screenshot and --screenshot-seconds to a subcommand."""
    p.add_argument("--screenshot", metavar="DIR",
                   help="after launching, save each device's screen to DIR/<device>.png")
    p.add_argument("--screenshot-seconds", type=_positive, default=3, metavar="N",
                   help="how long after launch the screenshot is taken (default 3)")
    p.add_argument("--logs", metavar="DIR",
                   help="after launching, watch the app and save each device's log to DIR/<device>.log")
    p.add_argument("--log-seconds", type=_positive, default=5, metavar="N",
                   help="how long to watch after launch (default 5)")


def _positive(text):
    """argparse type: an integer of 1 or more."""
    n = int(text)
    if n < 1:
        raise argparse.ArgumentTypeError("must be 1 or more")
    return n


def _boot_args(p, flag_help):
    """Add --show and --boot-timeout, plus --boot when flag_help is given (install only)."""
    if flag_help:
        p.add_argument("--boot", action="store_true", help=flag_help)
    p.add_argument("--show", action="store_true", help="show emulator and Simulator windows when booting")
    p.add_argument("--boot-timeout", type=_positive, default=300, metavar="SECONDS",
                   help="how long to wait for each device to boot (default 300)")


def _target_args(p, default="all"):
    """Add --to, --jobs, --json and --junit: the options every multi-device command shares."""
    p.add_argument("--to", default=default, metavar="SEL",
                   help="devices to act on: all (default), android, ios, physical, emulator, "
                        "simulator, api>=N, ios>=X, an alias or group, an id, or a model pattern; "
                        "comma-separated terms are combined")
    p.add_argument("--jobs", type=_positive, metavar="N", help="devices handled at once (default: up to 8)")
    p.add_argument("--json", action="store_true", help="machine-readable output")
    p.add_argument("--junit", metavar="FILE", help="also write the results as JUnit XML, one testcase per device")


def build_parser():
    """The argparse parser for every subcommand; each sets func to its cmd_* handler."""
    p = argparse.ArgumentParser(prog="devdrop",
                                description="Install one mobile build on every attached Android and iOS device.")
    p.add_argument("--version", action="version", version=f"devdrop {__version__}")
    p.add_argument("--config", metavar="PATH",
                   help="devdrop.toml to use (default: ./devdrop.toml, then ~/.config/devdrop/devdrop.toml)")
    sub = p.add_subparsers(dest="command", required=True)
    d = sub.add_parser("devices", help="list attached Android and iOS devices")
    d.add_argument("--json", action="store_true", help="machine-readable output")
    d.add_argument("--all", action="store_true",
                   help="include stopped Android virtual devices and shut-down simulators")
    d.set_defaults(func=cmd_devices)
    i = sub.add_parser("inspect", help="show what an .apk, .ipa or .app contains")
    i.add_argument("path")
    i.add_argument("--json", action="store_true", help="machine-readable output")
    i.set_defaults(func=cmd_inspect)
    ins = sub.add_parser("install", help="install a build on every selected device")
    ins.add_argument("path")
    ins.add_argument("--launch", action="store_true", help="start the app after installing")
    ins.add_argument("--clean", action="store_true", help="uninstall the app first (removes its data)")
    _log_args(ins)
    ins.add_argument("--watch", action="store_true",
                     help="keep running: install on each matching device as it connects (Ctrl-C to stop)")
    ins.add_argument("--watch-timeout", type=_positive, metavar="SECONDS",
                     help="with --watch: stop after this many seconds")
    ins.add_argument("--ks", metavar="KEYSTORE", help=".aab only: keystore to sign the built APKs "
                     "(password in DEVDROP_KS_PASS; default: the Android debug keystore)")
    ins.add_argument("--ks-alias", metavar="ALIAS", help=".aab only: key alias in --ks")
    _target_args(ins)
    _boot_args(ins, "start stopped emulators and simulators that --to names, then install")
    ins.set_defaults(func=cmd_install)
    for name, text in (("launch", "start an installed app"), ("uninstall", "remove an app")):
        c = sub.add_parser(name, help=f"{text} on every selected device")
        c.add_argument("app_id", help="Android package name or iOS bundle id")
        _target_args(c)
        if name == "launch":
            _log_args(c)
        c.set_defaults(func=cmd_by_id)
    pr = sub.add_parser("pair", help="pair with an Android device over Wi-Fi (adb pair)")
    pr.add_argument("address", help="HOST:PORT shown under Wireless debugging > Pair with pairing code")
    pr.add_argument("code", help="the six-digit pairing code")
    pr.set_defaults(func=cmd_pair)
    cn = sub.add_parser("connect", help="connect to Android devices over Wi-Fi (adb connect)")
    cn.add_argument("address", nargs="*", help="HOST:PORT; default: every paired device found by mDNS")
    cn.set_defaults(func=cmd_connect)
    bt = sub.add_parser("boot", help="start stopped emulators and simulators and wait until booted")
    _target_args(bt, default=None)
    _boot_args(bt, None)
    bt.set_defaults(func=cmd_boot)
    sd = sub.add_parser("shutdown", help="stop running emulators and simulators (never physical devices)")
    _target_args(sd)
    sd.set_defaults(func=cmd_shutdown)
    doc = sub.add_parser("doctor", help="check the toolchain and each device's state")
    doc.set_defaults(func=cmd_doctor)
    return p


def main(argv=None):
    """Parse argv, run the chosen command, return its exit code (130 on Ctrl-C)."""
    parser = build_parser()
    args = parser.parse_args(argv)
    # Refuse to boot "everything": a bare run could start every simulator on the machine.
    if (args.command == "boot" or getattr(args, "boot", False)) and args.to in (None, "all"):
        parser.error("booting needs an explicit --to (for example --to 'Pixel*' or --to sim)")
    try:
        return args.func(args)
    except KeyboardInterrupt:
        return 130
