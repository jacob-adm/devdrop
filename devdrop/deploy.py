# SPDX-License-Identifier: Apache-2.0
"""Run install / launch / uninstall on many devices at once."""

import json
import os
import re
import shlex
import stat
import subprocess
import tempfile
import time
import zipfile
from urllib.parse import unquote
from dataclasses import dataclass
from concurrent.futures import ThreadPoolExecutor, as_completed, wait

from devdrop import bundle, preflight
from devdrop.tools import ToolError, run, start

INSTALL_TIMEOUT = 600  # seconds for one install; large apps over Wi-Fi take minutes
DRY_RUN = "would install; nothing was sent (dry run)"   # the reason on an ok record from --dry-run

# adb failure codes and what to do about them.
ANDROID_FAILURES = {
    "INSTALL_FAILED_UPDATE_INCOMPATIBLE": "installed app is signed with a different key; use --clean",
    "INSTALL_FAILED_VERSION_DOWNGRADE": "installed app has a newer versionCode; use --clean",
    "INSTALL_FAILED_OLDER_SDK": "device API level is below the app's minSdk",
    "INSTALL_FAILED_NO_MATCHING_ABIS": "no native code for this device's CPU",
    "INSTALL_FAILED_INSUFFICIENT_STORAGE": "not enough storage on the device",
    "INSTALL_FAILED_USER_RESTRICTED": "install over USB was refused on the device; allow it in developer options",
    "INSTALL_FAILED_TEST_ONLY": "test-only APK refused",
    "INSTALL_PARSE_FAILED_NO_CERTIFICATES": "APK is not signed",
    "INSTALL_FAILED_ALREADY_EXISTS": "app already installed; use --clean",
}
# "Failure [INSTALL_FAILED_X: detail]" from adb install, or a bare INSTALL_* code from bundletool.
_FAILURE = re.compile(r"Failure \[([A-Z_]+)(?::[^\]]*)?\]|(INSTALL_[A-Z_]+)")
# package/activity as printed by `cmd package resolve-activity --brief`.
_COMPONENT = re.compile(r"^[A-Za-z][\w.]*/[\w.$]+$")


@dataclass
class LaunchLog:
    """--logs: where device logs go and how long to watch a launched app."""
    directory: str
    seconds: float


@dataclass
class Screenshot:
    """--screenshot: where the images go and how long after launch each one is taken."""
    directory: str
    seconds: float


PNG = b"\x89PNG\r\n\x1a\n"              # the first bytes of every PNG file
DEVICE_SHOT = "/data/local/tmp/devdrop-screenshot.png"   # a path the adb shell user can write


def _file_name(dev, ext):
    """File name for a device: its id with path-unsafe characters replaced."""
    return re.sub(r"[^\w.-]", "_", dev.id) + ext


def _log_name(dev):
    """Log file name for a device."""
    return _file_name(dev, ".log")


def _require_png(path):
    """Raise StepError unless path is a PNG; old Android exits 0 even when screencap fails."""
    try:
        with open(path, "rb") as f:
            ok = f.read(len(PNG)) == PNG
    except OSError:
        ok = False
    if not ok:
        if os.path.exists(path):
            os.remove(path)
        raise StepError("screenshot", "the device returned no image")


def _android_crash(text, package):
    """(crashed, reason) from logcat: a FATAL EXCEPTION block for this package's process.

    The reason is the root cause (the last "Caused by:" in the block), else the
    first exception line; newer Android splits the message across the two.
    """
    lines = text.splitlines()
    for i, line in enumerate(lines):
        if "FATAL EXCEPTION" not in line:
            continue
        # Strip logcat prefixes; a stack trace fits well within 120 lines.
        block = [ln.split("AndroidRuntime: ", 1)[-1].strip() for ln in lines[i + 1:i + 120]]
        if not any(b.startswith(f"Process: {package},") for b in block[:3]):
            continue                    # another app crashed: not ours
        causes = [b[len("Caused by: "):] for b in block if b.startswith("Caused by: ")]
        if causes:
            return True, causes[-1][:300]
        first = next((b for b in block[1:6] if ("Exception" in b or "Error" in b)
                      and not b.startswith("Process:")), "")
        return True, first[:300]
    return False, ""


def _ios_crash_line(text):
    """First Swift fatal error or uncaught Objective-C exception in a log, or ""."""
    for line in text.splitlines():
        for marker in ("Fatal error:", "Terminating app due to uncaught exception"):
            if marker in line:
                return line[line.index(marker):].strip()[:300]
    return ""


class StepError(Exception):
    """One device failed at a named stage (install, launch, running, ...) for a plain reason."""

    def __init__(self, stage, reason):
        super().__init__(reason)
        self.stage = stage
        self.reason = reason


def record(dev, result, stage, reason, started, warnings=(), extra=None):
    """One device's result as a dict: device facts, ok/skipped/failed, stage, reason, seconds.

    Every report (text, --json, --junit) is built from these.
    """
    return (extra or {}) | {"id": dev.id, "name": dev.name, "platform": dev.platform, "kind": dev.kind,
            "manufacturer": dev.manufacturer, "model": dev.model, "os_version": dev.os_version,
            "api": dev.api, "page_size": dev.page_size, "result": result, "stage": stage,
            "reason": reason, "warnings": list(warnings), "seconds": round(time.monotonic() - started, 1)}


def android_failure(text):
    """adb install output -> "CODE: what to do" when the code is known, else the code or last line."""
    m = _FAILURE.search(text)
    if not m:
        tail = text.strip().splitlines()
        return tail[-1] if tail else "unknown error"
    code = m.group(1) or m.group(2)
    return f"{code}: {ANDROID_FAILURES[code]}" if code in ANDROID_FAILURES else code


# Android 9 and later: "signatures=PackageSignatures{4fe50fc version:4, signatures:[15f777a2],
# past signatures:[1a2b3c flags: 17]}". Older versions print one number that is not the certificate's.
_SIGNATURES = re.compile(r"signatures=PackageSignatures\{\w+ version:\d+, signatures:\[([^\]]*)\]"
                         r"(?:, past signatures:\[([^\]]*)\])?")


def parse_installed(text, package):
    """versionCode and signing certificate ids of one package from `dumpsys package` output."""
    state = {"version_code": None, "cert_ids": None}
    start = text.find(f"Package [{package}]")       # absent when the app is not installed
    if start < 0:
        return state
    block = text[start:]
    m = re.search(r"\bversionCode=(\d+)", block)
    if m:
        state["version_code"] = int(m.group(1))
    m = _SIGNATURES.search(block)
    if m:
        past = re.findall(r"([0-9a-f]+)(?= flags)", m.group(2) or "")
        ids = [x.strip() for x in m.group(1).split(",") if x.strip()] + past
        state["cert_ids"] = ids or None
    return state


def _data_free(text):
    """Free space on /data in KB from `dumpsys diskstats` ("Data-Free: 8529084K / ..."), or None."""
    m = re.search(r"^Data-Free: (\d+)K", text, re.M)
    return int(m.group(1)) if m else None


class Android:
    """Install, launch, uninstall and launch-watching for Android devices through adb."""

    def __init__(self, adb, bundle=None):
        self.adb = adb
        self.bundle = bundle            # (bundletool argv, Signing, work dir) for .aab installs

    def _adb(self, dev, *args, timeout=None):
        """Run adb against one device (-s serial)."""
        return run([self.adb, "-s", dev.id, *args], timeout=timeout)

    def booted(self, dev):
        """True once Android has finished starting.

        adb lists a device as ready seconds before the package service
        exists; an install in that gap fails with "Can't find service:
        package". Android sets sys.boot_completed=1 when the gap is over.
        """
        try:
            return self._adb(dev, "shell", "getprop", "sys.boot_completed").out.strip() == "1"
        except ToolError:
            return False

    def installed(self, dev, package):
        """What the device holds that can make it refuse a build, or None if it cannot be asked.

        {"version_code": int or None, "cert_ids": [hex] or None, "free_kb": int or None};
        version_code and cert_ids are None when the app is not installed.
        """
        try:
            text = self._adb(dev, "shell", "dumpsys", "package", package).out
            disk = self._adb(dev, "shell", "dumpsys", "diskstats").out
        except ToolError:
            return None
        return parse_installed(text, package) | {"free_kb": _data_free(disk)}

    def install(self, dev, report):
        """Install an APK (or an .aab through bundletool); raise StepError with adb's reason."""
        if report["type"] == "aab":
            return self._install_bundle(dev, report)
        # -r: replace an installed copy, keeping data. -t: allow test-only (debug) APKs.
        r = self._adb(dev, "install", "-r", "-t", report["path"], timeout=INSTALL_TIMEOUT)
        out = r.out + r.err
        # Old adb versions exit 0 on failure: require the "Success" line too.
        if r.rc != 0 or "Success" not in out:
            raise StepError("install", android_failure(out))

    def _install_bundle(self, dev, report):
        """.aab: build this device's APK set, then install it. Stage says which half failed."""
        bundletool, sign, workdir = self.bundle
        apks = os.path.join(workdir, re.sub(r"[^\w.-]", "_", dev.id) + ".apks")
        r = bundle.build_apks(bundletool, self.adb, report["path"], dev.id, sign, apks)
        if r.rc != 0:
            raise StepError("build-apks", bundle.failure(r.out + r.err) or f"bundletool exit {r.rc}")
        r = bundle.install_apks(bundletool, self.adb, apks, dev.id)
        out = r.out + r.err
        if r.rc != 0 or "INSTALL_FAILED" in out or "INSTALL_PARSE_FAILED" in out:
            reason = android_failure(out) if _FAILURE.search(out) else bundle.failure(out)
            raise StepError("install", reason or f"bundletool exit {r.rc}")

    def screenshot(self, dev, path):
        """Save what the screen shows as a PNG: capture on the device, copy it over, tidy up."""
        self._adb(dev, "shell", "screencap", "-p", DEVICE_SHOT)
        r = self._adb(dev, "pull", DEVICE_SHOT, path)
        self._adb(dev, "shell", "rm", "-f", DEVICE_SHOT)
        if r.rc != 0:
            tail = (r.err or r.out).strip().splitlines()
            raise StepError("screenshot", tail[-1] if tail else f"adb pull exit {r.rc}")
        _require_png(path)

    def prepare_logs(self, dev):
        """Clear the main and crash log buffers so the watch window shows only this launch."""
        self._adb(dev, "logcat", "-b", "main", "-b", "crash", "-c")

    def discard_logs(self, dev):
        """Nothing to stop: Android's log is read once, after the watch."""

    def observe(self, dev, package, log):
        """After launch: is the app still running, and the device log for the window."""
        time.sleep(log.seconds)
        pids = self._adb(dev, "shell", "pidof", package).out.split()   # empty when not running
        text = self._adb(dev, "logcat", "-d", "-b", "main", "-b", "crash", "-v", "threadtime").out
        path = os.path.join(log.directory, _log_name(dev))
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
        # A crash in the window counts even if the process is alive again (or still starting).
        crashed, reason = _android_crash(text, package)
        running = bool(pids) and all(p.isdigit() for p in pids) and not crashed
        return running, path, reason

    def uninstall(self, dev, package, missing_ok=False):
        """Remove the app and its data. missing_ok: an absent app is not an error (--clean)."""
        r = self._adb(dev, "uninstall", package)
        out = r.out + r.err
        if "Success" in out:
            return
        # Android words "not installed" differently across versions.
        if missing_ok and ("DELETE_FAILED_INTERNAL_ERROR" in out or "Unknown package" in out):
            return
        reason = "not installed" if "DELETE_FAILED_INTERNAL_ERROR" in out or "Unknown package" in out \
            else android_failure(out)
        raise StepError("uninstall", reason)

    def launch(self, dev, package, fallback="", url=""):
        """Start the app's launcher activity, or open url in the app; raise StepError if Android refuses.

        Order: ask the device (resolve-activity), else the launcher read from the
        manifest (fallback), else monkey. The first two give a clear error from `am start`.
        """
        if url:
            # The package at the end limits the intent to this app. adb hands the line to the
            # device's shell, so the URL is quoted: "&" and "?" must reach am untouched.
            r = self._adb(dev, "shell", "am", "start", "-a", "android.intent.action.VIEW",
                          "-d", shlex.quote(url), package)
            out = r.out + r.err
            if "unable to resolve Intent" in out:
                raise StepError("launch", "the app has no activity that opens this URL (is it installed?)")
            if r.rc != 0 or "Error" in out:
                line = next((ln for ln in out.splitlines() if "Error" in ln), out.strip())
                raise StepError("launch", line.strip() or f"am start exit {r.rc}")
            return
        r = self._adb(dev, "shell", "cmd", "package", "resolve-activity", "--brief",
                      "-c", "android.intent.category.LAUNCHER", package)
        # Old Android (API 23 and below) has no `cmd`; its shell still exits 0 and
        # prints "/system/bin/sh: cmd: not found", so check the shape, not the exit code.
        lines = [ln.strip() for ln in r.out.splitlines() if ln.strip()]
        component = lines[-1] if lines and _COMPONENT.match(lines[-1]) else ""
        if not component and fallback:
            component = f"{package}/{fallback}"
        if not component:
            # Last resort on any Android version: let monkey fire the launcher intent.
            r = self._adb(dev, "shell", "monkey", "-p", package, "-c", "android.intent.category.LAUNCHER", "1")
            if "Events injected: 1" in r.out + r.err:
                return
            raise StepError("launch", "no launcher activity (is the app installed?)")
        r = self._adb(dev, "shell", "am", "start", "-n", component)
        out = r.out + r.err
        # am start exits 0 even when the activity does not exist; its "Error:" line says so.
        if r.rc != 0 or "Error" in out:
            line = next((ln for ln in out.splitlines() if "Error" in ln), out.strip())
            raise StepError("launch", line.strip() or f"am start exit {r.rc}")


# (text found anywhere in devicectl/simctl output, what to tell the operator). First match wins.
IOS_HINTS = (
    ("explicitly trusted by the user", "trust the developer on the device: Settings > General > "
                                       "VPN & Device Management"),
    ("Developer Mode", "turn on Developer Mode on the device"),
    ("locked", "unlock the device and retry"),
    ("provisioning profile", "the provisioning profile does not allow this device or app"),
    ("not installed", "not installed"),
)


def ios_failure(text):
    """devicectl/simctl failure -> its ERROR line, plus a fix when a known cause appears below it.

    devicectl puts a generic line first and the real cause deeper in the text.
    """
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    first = next((ln for ln in lines if ln.startswith("ERROR:")), lines[-1] if lines else "unknown error")
    first = first.removeprefix("ERROR:").strip()
    for needle, hint in IOS_HINTS:
        if needle.lower() in text.lower() and hint not in first:
            return f"{first} ({hint})"
    return first


def _pmd3_failure(text):
    """pymobiledevice3's last line without its "date host logger[pid] LEVEL" prefix."""
    lines = [ln.strip() for ln in text.splitlines() if ln.strip() and "Run again with -v" not in ln]
    last = lines[-1] if lines else "pymobiledevice3 failed"
    return re.sub(r"^\S+ \S+ \S+ \S+\[\d+\] [A-Z]+ ", "", last)[:300]


def unpack_ipa(ipa, dest):
    """Extract Payload/*.app from an IPA into dest, keeping file modes. Returns the .app path."""
    with zipfile.ZipFile(ipa) as z:
        app = None
        for info in z.infolist():
            parts = info.filename.split("/")
            if len(parts) < 2 or parts[0] != "Payload" or not parts[1].endswith(".app"):
                continue
            app = app or parts[1]
            # Only the first app; refuse ".." and absolute paths (zip-slip) so nothing lands outside dest.
            if parts[1] != app or ".." in parts or info.filename.startswith("/"):
                continue
            target = os.path.join(dest, *parts[1:])
            if info.is_dir():
                os.makedirs(target, exist_ok=True)
                continue
            os.makedirs(os.path.dirname(target), exist_ok=True)
            with z.open(info) as src, open(target, "wb") as out:
                out.write(src.read())
            # Keep the executable bit (zip stores Unix modes in the high 16 bits); never create symlinks.
            mode = (info.external_attr >> 16) & 0o777
            if stat.S_IFMT(info.external_attr >> 16) != stat.S_IFLNK and mode:
                os.chmod(target, mode)
    if app is None:
        raise StepError("unpack", "no Payload/*.app in the IPA")
    return os.path.join(dest, app)


LOG_START = 8.0   # seconds to wait for a physical iPhone's log stream to begin before launching
# A line of the device log: "2026-11-20 16:49:01.157354 Process{Image}[110] <NOTICE>: text".
_SYSLOG_PROCESS = re.compile(r"^\S+ \S+ ([^\[{]+)[\[{]")


def process_lines(text, names):
    """The lines of a device log written by any of the named processes."""
    keep = []
    for line in text.splitlines(keepends=True):
        m = _SYSLOG_PROCESS.match(line)
        if m and m.group(1).strip() in names:
            keep.append(line)
    return "".join(keep)


class Ios:
    """Install, launch, uninstall and launch-watching for iPhones (devicectl) and simulators (simctl).

    devicectl has no log or screenshot command. With pmd3, the path of the
    optional pymobiledevice3 tool, a physical device gets both.
    """

    def __init__(self, xcrun, pmd3=None):
        self.xcrun = xcrun
        self.pmd3 = pmd3
        self._captures = {}             # device id -> (log process, file it writes) during a launch watch

    def _run(self, *args, timeout=120):
        """Run an xcrun subcommand (devicectl or simctl)."""
        return run([self.xcrun, *args], timeout=timeout)

    def booted(self, dev):
        """A ready iPhone or simulator takes installs at once; only Android has a gap after "ready"."""
        return True

    def installed(self, dev, bundle_id):
        """iOS replaces an installed copy whatever its version or signer: nothing to ask."""
        return None

    def screenshot(self, dev, path):
        """Save the screen as a PNG: simctl for a simulator, pymobiledevice3 (if present) for a device."""
        if dev.kind == "simulator":
            self._check("screenshot", self._run("simctl", "io", dev.id, "screenshot", path))
        elif self.pmd3:
            # --userspace: the tunnel iOS 17 and later needs, without root.
            r = run([self.pmd3, "--no-color", "developer", "dvt", "screenshot", "--userspace",
                     "--udid", dev.id, path], timeout=60)
            if r.rc != 0:
                raise StepError("screenshot", _pmd3_failure(r.err or r.out))
        else:
            raise StepError("screenshot", "not available on a physical iPhone or iPad")
        _require_png(path)

    def _check(self, stage, r):
        """Raise StepError for a failed run, worded by ios_failure."""
        if r.rc != 0:
            raise StepError(stage, ios_failure(r.err or r.out))

    def install(self, dev, report):
        """Install the .app (an .ipa was unpacked to app_path first)."""
        app = report.get("app_path") or report["path"]
        if dev.kind == "simulator":
            r = self._run("simctl", "install", dev.id, app, timeout=INSTALL_TIMEOUT)
        else:
            r = self._run("devicectl", "device", "install", "app", "--device", dev.id, app,
                          timeout=INSTALL_TIMEOUT)
        self._check("install", r)

    def uninstall(self, dev, bundle_id, missing_ok=False):
        """Remove the app; an absent app raises "not installed" unless missing_ok."""
        if dev.kind == "simulator":
            installed = self._run("simctl", "get_app_container", dev.id, bundle_id).rc == 0
            if not installed:
                if missing_ok:
                    return
                raise StepError("uninstall", "not installed")
            r = self._run("simctl", "uninstall", dev.id, bundle_id)
        else:
            # devicectl reports success for an app that is not there, so ask the phone first.
            # When that query fails, the uninstall runs anyway and its own result counts.
            listed = self._json("device", "info", "apps", "--device", dev.id, "--bundle-id", bundle_id,
                                "--include-all-apps")
            if listed is not None and not listed.get("apps"):
                if missing_ok:
                    return
                raise StepError("uninstall", "not installed")
            r = self._run("devicectl", "device", "uninstall", "app", "--device", dev.id, bundle_id)
            if r.rc != 0 and missing_ok:
                return
        self._check("uninstall", r)

    def prepare_logs(self, dev):
        """Physical device with pymobiledevice3: start reading its log, so the launch is in it.

        Simulators need nothing here: their log is read back for a time window after launch.
        """
        if dev.kind == "simulator" or not self.pmd3:
            return
        fd, path = tempfile.mkstemp(prefix="devdrop-", suffix=".syslog")
        os.close(fd)
        try:
            proc = start([self.pmd3, "--no-color", "syslog", "live", "--no-debug", "--udid", dev.id], path)
        except ToolError:
            os.unlink(path)
            return
        self._captures[dev.id] = (proc, path)
        # The stream takes a moment to begin; a device always has something to say once it has.
        deadline = time.monotonic() + LOG_START
        while os.path.getsize(path) == 0 and proc.poll() is None and time.monotonic() < deadline:
            time.sleep(0.1)

    def discard_logs(self, dev):
        """Stop a log capture whose launch failed."""
        self._captured(dev)

    def _captured(self, dev):
        """Stop this device's log capture and return what it read ("" when there was none)."""
        proc, path = self._captures.pop(dev.id, (None, None))
        if proc is None:
            return None
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        try:
            with open(path, encoding="utf-8", errors="replace") as f:
                return f.read()
        finally:
            os.unlink(path)

    def observe(self, dev, bundle_id, log):
        """After launch: (running, log path, crash line). iPhone: running only, no log."""
        time.sleep(log.seconds)
        if dev.kind != "simulator":
            running, names = self._device_running(dev, bundle_id)
            text = self._captured(dev)
            if not text or not names:           # no pymobiledevice3, or it read nothing
                return running, None, ""
            text = process_lines(text, names)
            path = os.path.join(log.directory, _log_name(dev))
            with open(path, "w", encoding="utf-8") as f:
                f.write(text)
            return running, path, _ios_crash_line(text)
        listed = self._run("simctl", "spawn", dev.id, "launchctl", "list").out
        running = f"UIKitApplication:{bundle_id}[" in listed
        info = self._run("simctl", "appinfo", dev.id, bundle_id).out
        m = re.search(r'CFBundleExecutable = "?([^";\n]+)"?;', info)
        executable = m.group(1).strip() if m else ""
        text = ""
        if executable:
            text = self._run("simctl", "spawn", dev.id, "log", "show", "--style", "compact",
                             "--last", f"{int(log.seconds) + 5}s",
                             "--predicate", f'process == "{executable}"').out
        path = os.path.join(log.directory, _log_name(dev))
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
        return running, path, _ios_crash_line(text)

    def _json(self, *args):
        """Run a devicectl query that writes JSON to a file; its result, or None."""
        fd, path = tempfile.mkstemp(prefix="devdrop-", suffix=".json")
        os.close(fd)
        try:
            if self._run("devicectl", *args, "--json-output", path, "-q").rc != 0:
                return None
            with open(path, encoding="utf-8") as f:
                return json.load(f).get("result", {})
        except (OSError, ValueError):
            return None
        finally:
            os.unlink(path)

    def _device_running(self, dev, bundle_id):
        """Physical iPhone: (does a running process live inside this app's bundle?, its process names).

        Running is None when the phone could not be asked. The names are what
        the device log calls the app: its running executables, and the bundle
        folder's name for an app that has already exited.
        """
        apps = self._json("device", "info", "apps", "--device", dev.id, "--bundle-id", bundle_id,
                          "--include-all-apps")
        procs = self._json("device", "info", "processes", "--device", dev.id)
        if not apps or not apps.get("apps") or procs is None:
            return None, set()
        folder = apps["apps"][0].get("url", "").rstrip("/")
        if not folder:
            return None, set()
        inside = [p.get("executable", "") for p in procs.get("runningProcesses", [])
                  if p.get("executable", "").startswith(folder + "/")]
        names = {unquote(x.rsplit("/", 1)[-1]) for x in inside}
        names.add(unquote(folder.rsplit("/", 1)[-1]).removesuffix(".app"))
        return bool(inside), names

    def launch(self, dev, bundle_id, fallback="", url=""):
        """Start the app, restarting it if already running; with url, open it. fallback is unused on iOS."""
        if dev.kind == "simulator":
            r = self._run("simctl", "launch", "--terminate-running-process", dev.id, bundle_id)
            if r.rc == 0 and url:
                # simctl has no "open in this app": iOS gives the URL to whichever app claims it.
                r = self._run("simctl", "openurl", dev.id, url)
        else:
            link = ["--payload-url", url] if url else []
            r = self._run("devicectl", "device", "process", "launch", "--device", dev.id,
                          "--terminate-existing", *link, bundle_id)
        self._check("launch", r)


def _launch_and_watch(backend, dev, app_id, log, fallback="", shot=None, url=""):
    """Launch; with --logs, watch the app for a while; with --screenshot, save the screen.

    Returns (failure reason or "", extra record fields or None, warnings).
    """
    if log:
        backend.prepare_logs(dev)
    try:
        backend.launch(dev, app_id, fallback, url)
    except (StepError, ToolError):
        if log:
            backend.discard_logs(dev)       # nothing was launched: stop reading the device's log
        raise
    launched = time.monotonic()
    died, extra, notes = "", None, []
    if log:
        running, path, crash = backend.observe(dev, app_id, log)
        # running is None when it could not be told (iPhone query failed): not reported as a crash.
        extra = {"running": running, "log": path}
        if running is False:
            died = f"app exited within {log.seconds:g}s" + (f": {crash}" if crash else "")
    if shot:
        # Taken even when the app died: the screen then shows what the user would see.
        time.sleep(max(0.0, launched + shot.seconds - time.monotonic()))
        path = os.path.join(shot.directory, _file_name(dev, ".png"))
        try:
            backend.screenshot(dev, path)
            extra = (extra or {}) | {"screenshot": path}
        except (StepError, ToolError) as e:
            # A missing picture is not a failed install or launch: say so and keep the result.
            notes.append(f"no screenshot: {e.reason if isinstance(e, StepError) else e}")
    return died, extra, notes


def install_one(backend, dev, report, clean=False, launch=False, log=None, shot=None, url="",
                dry_run=False):
    """Full install on one device: preflight, optional clean, install, optional launch and watch.

    Never raises: every outcome becomes a record (ok, skipped at preflight, failed at a stage).
    dry_run stops after the preflight: the record says what would happen and nothing is sent.
    """
    started = time.monotonic()
    reason = preflight.check(report, dev)
    if reason:
        return record(dev, "skipped", "preflight", reason, started)
    reason = preflight.installed(report, dev, backend.installed(dev, report["id"]), clean)
    if reason:
        return record(dev, "skipped", "preflight", reason, started)
    notes = list(preflight.warnings(report, dev))
    if dry_run:
        return record(dev, "ok", "preflight", DRY_RUN, started, notes, {"dry_run": True})
    extra = None
    try:
        if clean:
            backend.uninstall(dev, report["id"], missing_ok=True)
        backend.install(dev, report)
        if launch:
            died, extra, more = _launch_and_watch(backend, dev, report["id"], log,
                                                  report.get("launcher", ""), shot, url)
            notes += more
            if died:
                return record(dev, "failed", "running", died, started, notes, extra)
    except StepError as e:
        return record(dev, "failed", e.stage, e.reason, started, notes, extra)
    except ToolError as e:
        return record(dev, "failed", "tool", str(e), started, notes, extra)
    return record(dev, "ok", "launch" if launch else "install", "", started, notes, extra)


def by_id_one(action, backend, dev, app_id, log=None, shot=None, url=""):
    """launch or uninstall an installed app on one device; never raises, returns a record."""
    started = time.monotonic()
    if dev.state != "ready":
        return record(dev, "skipped", "preflight", f"device is {dev.state}", started)
    extra, notes = None, []
    try:
        if action == "launch":
            died, extra, notes = _launch_and_watch(backend, dev, app_id, log, shot=shot, url=url)
            if died:
                return record(dev, "failed", "running", died, started, notes, extra)
        else:
            backend.uninstall(dev, app_id)
    except StepError as e:
        return record(dev, "failed", e.stage, e.reason, started, notes, extra)
    except ToolError as e:
        return record(dev, "failed", "tool", str(e), started, notes, extra)
    return record(dev, "ok", action, "", started, notes, extra)


class Interrupted(KeyboardInterrupt):
    """Ctrl-C arrived during a fan-out; records holds what the devices under way reported."""

    def __init__(self, records):
        super().__init__()
        self.records = records


def fan_out(devices, work, jobs=None, on_done=None):
    """Run work(device) on each device in parallel; records come back in device order.

    On Ctrl-C no further device is started, the ones under way finish, and
    Interrupted is raised with their records, so a run that is stopped still
    says what reached the devices. A second Ctrl-C ends the wait.
    """
    if not devices:
        return []
    jobs = max(1, jobs or min(8, len(devices)))   # default: up to 8 devices at once
    results = {}

    def collect(done):
        for f in done:
            if futures[f] not in results and not f.cancelled():
                rec = f.result()
                results[futures[f]] = rec
                if on_done:
                    on_done(rec)        # print each device as it finishes, not at the end

    with ThreadPoolExecutor(max_workers=jobs) as pool:
        futures = {pool.submit(work, d): i for i, d in enumerate(devices)}
        try:
            for f in as_completed(futures):
                collect([f])
        except KeyboardInterrupt:
            for f in futures:
                f.cancel()              # only takes effect on devices not started yet
            collect(wait(futures).done)
            raise Interrupted([results[i] for i in sorted(results)]) from None
    return [results[i] for i in range(len(devices))]
