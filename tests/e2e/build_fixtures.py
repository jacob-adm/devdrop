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

MANIFEST = """<?xml version="1.0" encoding="utf-8"?>
<manifest xmlns:android="http://schemas.android.com/apk/res/android" package="{package}">
  <application android:label="devdrop e2e" android:debuggable="{debuggable}" android:hasCode="true">
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
            f.write(MANIFEST.format(package=PACKAGE, debuggable="true",
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
