# SPDX-License-Identifier: Apache-2.0
"""Build APK and IPA files for tests, byte by byte, in the real formats."""

import io
import plistlib
import struct
import zipfile

# Res_value types
REF, STR, INT, HEX, BOOL = 0x01, 0x03, 0x10, 0x11, 0x12

ANDROID_NS = "http://schemas.android.com/apk/res/android"
IDS = {"name": 0x01010003, "enabled": 0x0101000E, "debuggable": 0x0101000F, "minSdkVersion": 0x0101020C,
       "targetSdkVersion": 0x01010270, "versionCode": 0x0101021B, "versionName": 0x0101021C}


def _len8(n):
    return bytes([n]) if n < 0x80 else bytes([0x80 | (n >> 8), n & 0xFF])


def _len16(n):
    return struct.pack("<H", n) if n < 0x8000 else struct.pack("<HH", 0x8000 | (n >> 16), n & 0xFFFF)


def string_pool(strings, utf8):
    data, offsets = b"", []
    for s in strings:
        offsets.append(len(data))
        if utf8:
            b = s.encode("utf-8")
            data += _len8(len(s)) + _len8(len(b)) + b + b"\x00"
        else:
            b = s.encode("utf-16-le")
            data += _len16(len(b) // 2) + b + b"\x00\x00"
    data += b"\x00" * (-len(data) % 4)
    header = 28
    strings_start = header + 4 * len(strings)
    size = strings_start + len(data)
    return (struct.pack("<HHIIIII", 0x0001, header, size, len(strings), 0,
                        (1 << 8) if utf8 else 0, strings_start) + struct.pack("<I", 0)
            + b"".join(struct.pack("<I", o) for o in offsets) + data)


def axml(root, utf8=False, strip_names=False):
    """root = (tag, [(attr, type, value)], [children]). Android attrs get resource ids.

    strip_names=True blanks android attribute names in the string pool, as
    shrinkers do; the resource map then carries the meaning.
    """
    android_attrs = []

    def walk(el):
        for a, _t, _v in el[1]:
            if a in IDS and a not in android_attrs:
                android_attrs.append(a)
        for c in el[2]:
            walk(c)
    walk(root)
    # Attribute names that have resource ids must come first, in map order.
    strings = [("" if strip_names else a) for a in android_attrs]
    index = {a: i for i, a in enumerate(android_attrs)}

    def sid(s):
        if s not in strings:
            strings.append(s)
        return strings.index(s)

    ns_uri = sid(ANDROID_NS)
    chunks = []

    def emit(el):
        tag, attrs, children = el
        body = b""
        for a, t, v in attrs:
            name = index[a] if a in index else sid(a)
            if t == STR:
                raw = data = sid(v)
            elif t == BOOL:
                raw, data = 0xFFFFFFFF, 0xFFFFFFFF if v else 0
            else:
                raw, data = 0xFFFFFFFF, v & 0xFFFFFFFF
            ns = ns_uri if a in IDS else 0xFFFFFFFF
            body += struct.pack("<IIIHBBI", ns, name, raw, 8, 0, t, data)
        ext = struct.pack("<IIHHHHHH", 0xFFFFFFFF, sid(tag), 20, 20, len(attrs), 0, 0, 0)
        chunk = ext + body
        chunks.append(struct.pack("<HHIII", 0x0102, 16, 16 + len(chunk), 1, 0xFFFFFFFF) + chunk)
        for c in children:
            emit(c)
        chunks.append(struct.pack("<HHIIIII", 0x0103, 16, 24, 1, 0xFFFFFFFF, 0xFFFFFFFF, sid(tag)))

    emit(root)
    pool = string_pool(strings, utf8)
    resmap = b"".join(struct.pack("<I", IDS[a]) for a in android_attrs)
    resmap = struct.pack("<HHI", 0x0180, 8, 8 + len(resmap)) + resmap
    body = pool + resmap + b"".join(chunks)
    return struct.pack("<HHI", 0x0003, 8, 8 + len(body)) + body


def manifest(package="com.example.app", min_sdk=26, target_sdk=35, version_code=7,
             version_name="1.2.3", debuggable=True, launcher=".MainActivity", min_type=INT,
             before=()):
    activity = ("activity", [("name", STR, launcher)], [
        ("intent-filter", [], [
            ("action", [("name", STR, "android.intent.action.MAIN")], []),
            ("category", [("name", STR, "android.intent.category.LAUNCHER")], []),
        ])])
    other = ("activity", [("name", STR, ".Settings")], [])
    return ("manifest", [("package", STR, package), ("versionCode", INT, version_code),
                         ("versionName", STR, version_name)], [
        ("uses-sdk", [("minSdkVersion", min_type, min_sdk), ("targetSdkVersion", INT, target_sdk)], []),
        ("application", [("debuggable", BOOL, debuggable)], [*before, other, activity] if launcher else [other]),
    ])


def launcher_alias(name, enabled):
    """An activity-alias the launcher would show, unless disabled."""
    return ("activity-alias", [("name", STR, name), ("enabled", BOOL, enabled)], [
        ("intent-filter", [], [
            ("action", [("name", STR, "android.intent.action.MAIN")], []),
            ("category", [("name", STR, "android.intent.category.LAUNCHER")], []),
        ])])


def apk(path, manifest_bytes, libs=(), v1=False, v2=False):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("AndroidManifest.xml", manifest_bytes)
        z.writestr("classes.dex", b"dex\n035\x00")
        for abi in libs:
            z.writestr(f"lib/{abi}/libnative.so", b"\x7fELF")
        if v1:
            z.writestr("META-INF/CERT.SF", b"Signature-Version: 1.0\n")
            z.writestr("META-INF/CERT.RSA", b"\x30\x82")
    data = buf.getvalue()
    if v2:
        # Insert an APK Signing Block before the central directory and fix the offset.
        eocd = data.rfind(b"PK\x05\x06")
        cd = struct.unpack_from("<I", data, eocd + 16)[0]
        block_body = struct.pack("<Q", 12) + struct.pack("<I", 0x7109871A) + b"\x00" * 8
        block = struct.pack("<Q", len(block_body) + 24) + block_body + \
            struct.pack("<Q", len(block_body) + 24) + b"APK Sig Block 42"
        data = data[:cd] + block + data[cd:]
        eocd += len(block)
        data = data[:eocd + 16] + struct.pack("<I", cd + len(block)) + data[eocd + 20:]
    with open(path, "wb") as f:
        f.write(data)


def info_plist(bundle_id="com.example.app", version="1.2.3", build="42", min_os="17.0",
               families=(1, 2), simulator=False):
    return plistlib.dumps({
        "CFBundleIdentifier": bundle_id,
        "CFBundleShortVersionString": version,
        "CFBundleVersion": build,
        "MinimumOSVersion": min_os,
        "UIDeviceFamily": list(families),
        "CFBundleSupportedPlatforms": ["iPhoneSimulator" if simulator else "iPhoneOS"],
    }, fmt=plistlib.FMT_BINARY)


def mobileprovision(devices=("00008150-0000000000000001",), expires=None, get_task_allow=True,
                    all_devices=False, name="Example Dev Profile", team="ABCDE12345"):
    import datetime
    p = {"Name": name, "TeamIdentifier": [team],
         "ExpirationDate": expires or datetime.datetime(2030, 1, 1),
         "Entitlements": {"get-task-allow": get_task_allow}}
    if all_devices:
        p["ProvisionsAllDevices"] = True
    elif devices is not None:
        p["ProvisionedDevices"] = list(devices)
    # A CMS SignedData envelope wraps the XML plist; bytes around it are opaque here.
    return b"\x30\x80\x06\x09\x2a\x86\x48\x86\xf7\x0d\x01\x07\x02" + plistlib.dumps(p) + b"\x00\x00\xa0\x82"


def ipa(path, info, profile=None, app="Example.app"):
    with zipfile.ZipFile(path, "w") as z:
        z.writestr(f"Payload/{app}/Info.plist", info)
        z.writestr(f"Payload/{app}/Example", b"\xcf\xfa\xed\xfe")
        if profile is not None:
            z.writestr(f"Payload/{app}/embedded.mobileprovision", profile)
