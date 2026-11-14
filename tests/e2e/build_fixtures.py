# SPDX-License-Identifier: Apache-2.0
"""Build the real apps the end-to-end tests install.

Android: APKs built without Gradle (javac, d8, aapt2, zipalign, apksigner
from the SDK build-tools). iOS: apps compiled with swiftc and signed
ad hoc. Every variant exists to prove one behaviour.

    python3 tests/e2e/build_fixtures.py [OUT_DIR]

OUT_DIR defaults to $DEVDROP_E2E_CACHE or ~/.cache/devdrop-e2e. Nothing
here is committed; the script prints a JSON index of what it built.
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import zipfile

PACKAGE = "dev.devdrop.e2e"

ACTIVITY = """package dev.devdrop.e2e;
public class Main extends android.app.Activity {
    @Override protected void onCreate(android.os.Bundle b) {
        super.onCreate(b);
        android.widget.TextView t = new android.widget.TextView(this);
        t.setText("devdrop e2e");
        setContentView(t);
    }
}
"""

NATIVE_ACTIVITY = ACTIVITY.replace(
    "public class Main extends android.app.Activity {",
    "public class Main extends android.app.Activity {\n"
    "    static { System.loadLibrary(\"e2e\"); }   // the app cannot start unless its native code loads")
NATIVE_C = "int devdrop_e2e(void) { return 42; }\n"
# name: (ELF page size, zipalign page flags) - arm64 library stored uncompressed and loaded in place
NATIVE = {"app-native4k": (4096, ["-p"]), "app-native16k": (16384, ["-P", "16"])}

MANIFEST = """<?xml version="1.0" encoding="utf-8"?>
<manifest xmlns:android="http://schemas.android.com/apk/res/android" package="{package}">
  <application android:label="devdrop e2e" android:debuggable="{debuggable}" android:hasCode="true"{extract}>
    <activity android:name=".Main" android:exported="true">{main_filter}</activity>{aliases}
  </application>
</manifest>
"""
LAUNCHER = """
      <intent-filter>
        <action android:name="android.intent.action.MAIN"/>
        <category android:name="android.intent.category.LAUNCHER"/>
      </intent-filter>"""
ALIASES = """
    <activity-alias android:name=".Hidden" android:targetActivity=".Main" android:enabled="false" android:exported="true">{f}</activity-alias>
    <activity-alias android:name=".Shown" android:targetActivity=".Main" android:exported="true">{f}</activity-alias>""".format(f=LAUNCHER)

# name: (versionCode, minSdk, key or None, extra)
ANDROID = {
    "app-v1": (1, 21, "a", {}),
    "app-v2": (2, 21, "a", {}),
    "app-keyb": (3, 21, "b", {}),
    "app-unsigned": (1, 21, None, {}),
    "app-min35": (1, 35, "a", {}),
    "app-x86only": (1, 21, "a", {"abi": "x86_64"}),
    "app-alias": (1, 21, "a", {"alias": True}),
}

SWIFT = """import UIKit
class Delegate: UIResponder, UIApplicationDelegate {
    var window: UIWindow?
    func application(_ a: UIApplication, didFinishLaunchingWithOptions o: [UIApplication.LaunchOptionsKey: Any]?) -> Bool {
        window = UIWindow(frame: UIScreen.main.bounds)
        let vc = UIViewController(); vc.view.backgroundColor = .systemTeal
        window?.rootViewController = vc; window?.makeKeyAndVisible(); return true
    }
}
UIApplicationMain(CommandLine.argc, CommandLine.unsafeArgv, nil, NSStringFromClass(Delegate.self))
"""
# name: (sdk, minimum iOS in Info.plist, device families)
IOS = {
    "ios-sim": ("iphonesimulator", "17.0", [1, 2]),
    "ios-sim-min99": ("iphonesimulator", "99.0", [1, 2]),
    "ios-sim-ipadonly": ("iphonesimulator", "17.0", [2]),
    "ios-device": ("iphoneos", "17.0", [1, 2]),
}


def sh(*argv, **kw):
    r = subprocess.run(argv, capture_output=True, text=True, **kw)
    if r.returncode != 0:
        raise SystemExit(f"failed: {' '.join(map(str, argv))}\n{r.stdout}{r.stderr}")
    return r.stdout


def sdk_dir():
    for root in (os.environ.get("ANDROID_HOME"), os.environ.get("ANDROID_SDK_ROOT"),
                 os.path.expanduser("~/Library/Android/sdk"), os.path.expanduser("~/Android/Sdk")):
        if root and os.path.isdir(os.path.join(root, "build-tools")):
            return root
    return None


def newest(path):
    names = sorted(os.listdir(path), key=lambda n: [int(x) if x.isdigit() else -1 for x in n.replace("-", ".").split(".")])
    return os.path.join(path, names[-1])


def keystore(work, name):
    path = os.path.join(work, f"{name}.jks")
    if not os.path.exists(path):
        sh("keytool", "-genkeypair", "-keystore", path, "-alias", name, "-keyalg", "RSA", "-keysize", "2048",
           "-validity", "10000", "-storepass", "devdrop", "-keypass", "devdrop", "-dname", f"CN=devdrop e2e {name}")
    return path


def build_android(out, work):
    sdk = sdk_dir()
    if sdk is None:
        print("android: no SDK with build-tools; skipped", file=sys.stderr)
        return {}
    bt = newest(os.path.join(sdk, "build-tools"))
    jar = os.path.join(newest(os.path.join(sdk, "platforms")), "android.jar")
    src = os.path.join(work, "src", *PACKAGE.split("."))
    os.makedirs(src, exist_ok=True)
    with open(os.path.join(src, "Main.java"), "w") as f:
        f.write(ACTIVITY)
    classes = os.path.join(work, "classes")
    sh("javac", "--release", "11", "-classpath", jar, "-d", classes, os.path.join(src, "Main.java"))
    class_files = [os.path.join(dp, n) for dp, _, ns in os.walk(classes) for n in ns if n.endswith(".class")]
    sh(os.path.join(bt, "d8"), "--min-api", "21", "--lib", jar, "--output", work, *class_files)
    dex = os.path.join(work, "classes.dex")
    built = {}
    for name, (code, min_sdk, key, extra) in ANDROID.items():
        manifest = os.path.join(work, f"{name}.xml")
        with open(manifest, "w") as f:
            f.write(MANIFEST.format(package=PACKAGE, debuggable="true", extract="",
                                    main_filter="" if extra.get("alias") else LAUNCHER,
                                    aliases=ALIASES if extra.get("alias") else ""))
        raw = os.path.join(work, f"{name}.raw.apk")
        sh(os.path.join(bt, "aapt2"), "link", "-o", raw, "-I", jar, "--manifest", manifest,
           "--min-sdk-version", str(min_sdk), "--target-sdk-version", "35",
           "--version-code", str(code), "--version-name", f"1.0.{code}")
        with zipfile.ZipFile(raw, "a") as z:
            z.write(dex, "classes.dex")
            if extra.get("abi"):
                z.writestr(f"lib/{extra['abi']}/libe2e.so", b"\x7fELF\x02\x01\x01" + b"\0" * 57)
        aligned = os.path.join(work, f"{name}.aligned.apk")
        sh(os.path.join(bt, "zipalign"), "-f", "-p", "4", raw, aligned)
        dest = os.path.join(out, f"{name}.apk")
        if key:
            sh(os.path.join(bt, "apksigner"), "sign", "--ks", keystore(work, key), "--ks-pass", "pass:devdrop",
               "--key-pass", "pass:devdrop", "--out", dest, aligned)
        else:
            shutil.copy(aligned, dest)
        built[name] = {"path": dest, "version_code": code, "min_sdk": min_sdk, "key": key, **extra}
    built.update(build_native(out, work, bt, jar))
    built.update(build_bundle(out, work, bt, jar))
    shutil.copy(keystore(work, "a"), os.path.join(out, "e2e-signing.jks"))   # alias a, password devdrop
    return built


def build_bundle(out, work, bt, jar):
    """A real .aab: aapt2 proto-format manifest, dex and the 16 KB arm64 library, via bundletool."""
    bundletool = shutil.which("bundletool")
    lib = os.path.join(work, "native", "app-native16k.so")
    if bundletool is None or not os.path.exists(lib):
        print("android bundle: needs bundletool and the native library; skipped", file=sys.stderr)
        return {}
    manifest = os.path.join(work, "bundle.xml")
    with open(manifest, "w") as f:
        f.write(MANIFEST.format(package=PACKAGE, debuggable="true", extract="", main_filter=LAUNCHER,
                                aliases="").replace("<manifest ", '<manifest android:versionCode="4" ', 1))
    proto = os.path.join(work, "bundle.proto.apk")
    sh(os.path.join(bt, "aapt2"), "link", "--proto-format", "-o", proto, "-I", jar, "--manifest", manifest,
       "--min-sdk-version", "23", "--target-sdk-version", "35", "--version-code", "4", "--version-name", "1.0.4")
    module = os.path.join(work, "base.zip")
    with zipfile.ZipFile(proto) as src, zipfile.ZipFile(module, "w") as z:
        for name in src.namelist():
            target = "manifest/AndroidManifest.xml" if name == "AndroidManifest.xml" else name
            z.writestr(target, src.read(name))
        z.write(os.path.join(work, "classes.dex"), "dex/classes.dex")
        z.write(lib, "lib/arm64-v8a/libe2e.so")
    dest = os.path.join(out, "app-bundle.aab")
    if os.path.exists(dest):
        os.remove(dest)
    sh(bundletool, "build-bundle", f"--modules={module}", f"--output={dest}")
    return {"app-bundle": {"path": dest, "version_code": 4, "min_sdk": 23, "key": None, "abi": "arm64-v8a",
                           "bundle": True}}


def build_native(out, work, bt, jar):
    """APKs whose app loads an arm64 library at start, aligned to 4 KB and to 16 KB."""
    lld = os.path.join(bt, "lld-bin", "lld")
    if shutil.which("xcrun") is None and shutil.which("clang") is None or not os.path.exists(lld):
        print("android native: no clang or lld; skipped", file=sys.stderr)
        return {}
    clang = ["xcrun", "clang"] if shutil.which("xcrun") else ["clang"]
    linker = os.path.join(work, "lldbin", "ld.lld")
    os.makedirs(os.path.dirname(linker), exist_ok=True)
    if not os.path.exists(linker):
        os.symlink(lld, linker)                # lld picks its flavour from the name it is run as
    src = os.path.join(work, "native", "src", *PACKAGE.split("."))
    os.makedirs(src, exist_ok=True)
    with open(os.path.join(src, "Main.java"), "w") as f:
        f.write(NATIVE_ACTIVITY)
    with open(os.path.join(work, "native", "e2e.c"), "w") as f:
        f.write(NATIVE_C)
    classes = os.path.join(work, "native", "classes")
    sh("javac", "--release", "11", "-classpath", jar, "-d", classes, os.path.join(src, "Main.java"))
    class_files = [os.path.join(dp, n) for dp, _, ns in os.walk(classes) for n in ns if n.endswith(".class")]
    dex_dir = os.path.join(work, "native")
    sh(os.path.join(bt, "d8"), "--min-api", "23", "--lib", jar, "--output", dex_dir, *class_files)
    manifest = os.path.join(work, "native", "AndroidManifest.xml")
    with open(manifest, "w") as f:
        f.write(MANIFEST.format(package=PACKAGE, debuggable="true", extract=' android:extractNativeLibs="false"',
                                main_filter=LAUNCHER, aliases=""))
    built = {}
    for name, (page, zipalign_flags) in NATIVE.items():
        so = os.path.join(work, "native", f"{name}.so")
        sh(*clang, "--target=aarch64-linux-android23", "-nostdlib", "-shared", "-fPIC", f"-fuse-ld={linker}",
           f"-Wl,-z,max-page-size={page}", "-o", so, os.path.join(work, "native", "e2e.c"))
        raw = os.path.join(work, f"{name}.raw.apk")
        sh(os.path.join(bt, "aapt2"), "link", "-o", raw, "-I", jar, "--manifest", manifest,
           "--min-sdk-version", "23", "--target-sdk-version", "35", "--version-code", "1", "--version-name", "1.0.1")
        with zipfile.ZipFile(raw, "a") as z:
            z.write(os.path.join(dex_dir, "classes.dex"), "classes.dex", zipfile.ZIP_DEFLATED)
            z.write(so, "lib/arm64-v8a/libe2e.so", zipfile.ZIP_STORED)
        aligned = os.path.join(work, f"{name}.aligned.apk")
        sh(os.path.join(bt, "zipalign"), "-f", *zipalign_flags, "4", raw, aligned)
        dest = os.path.join(out, f"{name}.apk")
        sh(os.path.join(bt, "apksigner"), "sign", "--ks", keystore(work, "a"), "--ks-pass", "pass:devdrop",
           "--key-pass", "pass:devdrop", "--out", dest, aligned)
        built[name] = {"path": dest, "version_code": 1, "min_sdk": 23, "key": "a", "abi": "arm64-v8a",
                       "elf_page": page}
    return built


def build_ios(out, work):
    if shutil.which("xcrun") is None:
        print("ios: no Xcode; skipped", file=sys.stderr)
        return {}
    main = os.path.join(work, "main.swift")
    with open(main, "w") as f:
        f.write(SWIFT)
    built = {}
    for name, (sdk, min_os, families) in IOS.items():
        app = os.path.join(out, f"{name}.app")
        shutil.rmtree(app, ignore_errors=True)
        os.makedirs(app)
        sdk_path = sh("xcrun", "--sdk", sdk, "--show-sdk-path").strip()
        target = "arm64-apple-ios17.0" + ("-simulator" if sdk == "iphonesimulator" else "")
        sh("xcrun", "--sdk", sdk, "swiftc", "-target", target, "-sdk", sdk_path, main, "-o", os.path.join(app, "E2E"))
        info = {
            "CFBundleIdentifier": PACKAGE, "CFBundleExecutable": "E2E", "CFBundleName": "E2E",
            "CFBundlePackageType": "APPL", "CFBundleShortVersionString": "1.0", "CFBundleVersion": "1",
            "MinimumOSVersion": min_os, "UIDeviceFamily": families, "UILaunchScreen": {},
            "CFBundleSupportedPlatforms": ["iPhoneSimulator" if sdk == "iphonesimulator" else "iPhoneOS"],
        }
        import plistlib
        with open(os.path.join(app, "Info.plist"), "wb") as f:
            plistlib.dump(info, f)
        if sdk == "iphonesimulator":
            sh("codesign", "-s", "-", "--force", app)
        ipa = os.path.join(out, f"{name}.ipa")
        with zipfile.ZipFile(ipa, "w", zipfile.ZIP_DEFLATED) as z:
            for dp, _, names in os.walk(app):
                for n in names:
                    full = os.path.join(dp, n)
                    info_z = zipfile.ZipInfo(os.path.join("Payload", f"{name}.app", os.path.relpath(full, app)))
                    info_z.external_attr = (os.stat(full).st_mode & 0xFFFF) << 16
                    with open(full, "rb") as fh:
                        z.writestr(info_z, fh.read(), zipfile.ZIP_DEFLATED)
        built[name] = {"path": app, "ipa": ipa, "sdk": sdk, "min_os": min_os, "families": families}
    return built


def main(argv):
    out = argv[1] if len(argv) > 1 else (os.environ.get("DEVDROP_E2E_CACHE")
                                         or os.path.expanduser("~/.cache/devdrop-e2e"))
    os.makedirs(out, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="devdrop-e2e-") as work:
        index = {"package": PACKAGE, "android": build_android(out, work), "ios": build_ios(out, work)}
    with open(os.path.join(out, "index.json"), "w") as f:
        json.dump(index, f, indent=2)
    print(json.dumps(index, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
