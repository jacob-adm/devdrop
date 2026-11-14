# SPDX-License-Identifier: Apache-2.0
"""Read what an APK, IPA or .app contains, with the standard library only."""

import datetime
import os
import plistlib
import posixpath
import struct
import zipfile

from devdrop import axml


class ArtifactError(ValueError):
    """The file is not a build devdrop can read."""


def _text(value):
    """Plist values are untyped input: report them as text."""
    return value if isinstance(value, str) else str(value)


SIG_BLOCK_MAGIC = b"APK Sig Block 42"


def inspect(path, now=None):
    if os.path.isdir(path) and not path.rstrip("/\\").endswith(".app"):
        raise ArtifactError(f"{path}: a directory must be an .app bundle")
    if not os.path.exists(path):
        raise ArtifactError(f"{path}: not found")
    lower = path.lower()
    try:
        if os.path.isdir(path):
            return _app_dir(path, now)
        if lower.endswith(".apk"):
            return _apk(path)
        if lower.endswith(".ipa"):
            return _ipa(path, now)
    except ArtifactError:
        raise
    except zipfile.BadZipFile as e:
        raise ArtifactError(f"{path}: not a valid zip archive ({e})")
    except axml.AxmlError as e:
        raise ArtifactError(f"{path}: AndroidManifest.xml: {e}")
    except Exception as e:
        # A build is untrusted input: damaged zips, plists and manifests raise
        # many exception types (zlib.error, RuntimeError for "encrypted" entries,
        # ExpatError, TypeError for wrongly typed plist values). Report, never crash.
        raise ArtifactError(f"{path}: unreadable build ({type(e).__name__}: {e})")
    if lower.endswith(".aab"):
        raise ArtifactError(f"{path}: Android App Bundles (.aab) are not supported yet; build an APK")
    raise ArtifactError(f"{path}: unknown build type (expected .apk, .ipa or .app)")


# ---------------------------------------------------------------- Android

def _int_or_codename(value):
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.isdigit():
        return int(value)
    return value  # preview codename such as "VanillaIceCream", or a reference


def _apk_signing(path, names):
    v1 = any(n.startswith("META-INF/") and n.upper().endswith(".SF") for n in names)
    v2 = False
    with open(path, "rb") as f:
        f.seek(0, os.SEEK_END)
        size = f.tell()
        f.seek(max(0, size - 65557))
        tail = f.read()
    eocd = tail.rfind(b"PK\x05\x06")
    if eocd >= 0 and eocd + 20 <= len(tail):
        cd_offset = struct.unpack_from("<I", tail, eocd + 16)[0]
        if cd_offset >= 16:
            with open(path, "rb") as f:
                f.seek(cd_offset - 16)
                v2 = f.read(16) == SIG_BLOCK_MAGIC
    schemes = (["v1"] if v1 else []) + (["v2+"] if v2 else [])
    return schemes


def _launcher(app):
    """First enabled activity or alias, in manifest order, that the launcher shows."""
    for act in app.children:
        if act.tag not in ("activity", "activity-alias") or act.attrs.get("enabled") is False:
            continue
        for f in act.find_all("intent-filter"):
            actions = {a.attrs.get("name") for a in f.find_all("action")}
            cats = {c.attrs.get("name") for c in f.find_all("category")}
            if "android.intent.action.MAIN" in actions and "android.intent.category.LAUNCHER" in cats:
                return act.attrs.get("name", "")
    return ""


def _apk(path):
    with zipfile.ZipFile(path) as z:
        names = z.namelist()
        if "AndroidManifest.xml" not in names:
            raise ArtifactError(f"{path}: no AndroidManifest.xml (not an APK)")
        root = axml.parse(z.read("AndroidManifest.xml"))
    if root.tag != "manifest":
        raise ArtifactError(f"{path}: AndroidManifest.xml root is <{root.tag}>, not <manifest>")
    sdk = next(root.find_all("uses-sdk"), None)
    app = next(root.find_all("application"), None)
    package = root.attrs.get("package", "")
    launcher = _launcher(app) if app else ""
    if launcher.startswith("."):
        launcher = package + launcher
    elif launcher and "." not in launcher:      # bare class name: relative to the package
        launcher = f"{package}.{launcher}"
    abis = sorted({n.split("/")[1] for n in names
                   if n.startswith("lib/") and n.endswith(".so") and n.count("/") >= 2})
    min_sdk = _int_or_codename(sdk.attrs.get("minSdkVersion", 1)) if sdk else 1
    return {
        "type": "apk",
        "platform": "android",
        "path": path,
        "id": package,
        "version": str(root.attrs.get("versionName", "")),
        "build": str(root.attrs.get("versionCode", "")),
        "min_sdk": min_sdk,
        "target_sdk": _int_or_codename(sdk.attrs.get("targetSdkVersion", min_sdk)) if sdk else min_sdk,
        "abis": abis,                       # empty: no native code, runs on any ABI
        "debuggable": bool(app.attrs.get("debuggable", False)) if app else False,
        "launcher": launcher,
        "split": str(root.attrs.get("split", "")),
        "signing": _apk_signing(path, names),
    }


# ---------------------------------------------------------------- iOS

def profile_from_bytes(data, now=None):
    """The plist inside an embedded.mobileprovision (a CMS envelope around XML)."""
    start = data.find(b"<?xml")
    end = data.find(b"</plist>", start)
    if start < 0 or end < 0:
        raise ArtifactError("embedded.mobileprovision: no property list found")
    try:
        p = plistlib.loads(data[start:end + len(b"</plist>")])
    except Exception as e:
        raise ArtifactError(f"embedded.mobileprovision: unreadable property list ({type(e).__name__}: {e})")
    if not isinstance(p, dict):
        raise ArtifactError("embedded.mobileprovision: property list is not a dictionary")
    now = now or datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
    expires = p.get("ExpirationDate")
    devices = p.get("ProvisionedDevices")
    ent = p.get("Entitlements", {})
    if p.get("ProvisionsAllDevices"):
        kind = "enterprise"
    elif devices is None:
        kind = "app-store"
    elif ent.get("get-task-allow"):
        kind = "development"
    else:
        kind = "ad-hoc"
    team = p.get("TeamIdentifier") or [""]
    return {
        "name": _text(p.get("Name", "")),
        "kind": kind,
        "team": _text(team[0] if isinstance(team, list) else team),
        "devices": [_text(d) for d in devices] if isinstance(devices, list) else [],
        "all_devices": bool(p.get("ProvisionsAllDevices")),
        "expires": expires.isoformat() + "Z" if isinstance(expires, datetime.datetime) else "",
        "expired": isinstance(expires, datetime.datetime) and expires <= now,
    }


def _ios_report(kind, path, info, profile):
    platforms = info.get("CFBundleSupportedPlatforms") or [info.get("DTPlatformName", "")]
    simulator = any("simulator" in str(p).lower() for p in platforms)
    families = {1: "iphone", 2: "ipad"}
    family_ids = info.get("UIDeviceFamily", [1])
    if not isinstance(family_ids, list):
        family_ids = [family_ids]
    return {
        "type": kind,
        "platform": "ios",
        "path": path,
        "id": _text(info.get("CFBundleIdentifier", "")),
        "version": _text(info.get("CFBundleShortVersionString", "")),
        "build": _text(info.get("CFBundleVersion", "")),
        "min_os": _text(info.get("MinimumOSVersion", "")),
        "families": [families.get(f, _text(f)) for f in family_ids],
        "target": "simulator" if simulator else "device",
        "profile": profile,
    }


def _ipa(path, now):
    with zipfile.ZipFile(path) as z:
        apps = sorted({n.split("/")[1] for n in z.namelist()
                       if n.startswith("Payload/") and n.split("/")[1].endswith(".app")})
        if not apps:
            raise ArtifactError(f"{path}: no Payload/*.app (not an IPA)")
        base = posixpath.join("Payload", apps[0])
        try:
            info = plistlib.loads(z.read(base + "/Info.plist"))
        except KeyError:
            raise ArtifactError(f"{path}: {base}/Info.plist missing")
        try:
            prov = z.read(base + "/embedded.mobileprovision")
        except KeyError:
            prov = None
    return _ios_report("ipa", path, info, profile_from_bytes(prov, now) if prov else None)


def _app_dir(path, now):
    try:
        with open(os.path.join(path, "Info.plist"), "rb") as f:
            info = plistlib.load(f)
    except FileNotFoundError:
        raise ArtifactError(f"{path}: Info.plist missing")
    except plistlib.InvalidFileException as e:
        raise ArtifactError(f"{path}: Info.plist: {e}")
    prov_path = os.path.join(path, "embedded.mobileprovision")
    profile = None
    if os.path.isfile(prov_path):
        with open(prov_path, "rb") as f:
            profile = profile_from_bytes(f.read(), now)
    return _ios_report("app", path, info, profile)
