# SPDX-License-Identifier: Apache-2.0
import datetime
import json
import os
import plistlib
import subprocess
import sys
import tempfile
import unittest

from devdrop import artifact, axml
from tests import builders as b

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def run_cli(*args):
    env = dict(os.environ, PYTHONPATH=REPO)
    return subprocess.run([sys.executable, "-m", "devdrop", *args], capture_output=True,
                          text=True, env=env, timeout=60)


class Tmp(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = self._tmp.name

    def path(self, name):
        return os.path.join(self.dir, name)


class Axml(unittest.TestCase):
    def test_utf8_and_utf16_pools_decode_the_same(self):
        for utf8 in (False, True):
            with self.subTest(utf8=utf8):
                root = axml.parse(b.axml(b.manifest(), utf8=utf8))
                self.assertEqual(root.tag, "manifest")
                self.assertEqual(root.attrs["package"], "com.example.app")
                self.assertEqual(root.attrs["versionCode"], 7)

    def test_long_and_non_ascii_strings(self):
        long_name = "com.example." + "x" * 300            # two-byte UTF-8 length
        huge = "é" * 40000                                 # two-word UTF-16 length
        for utf8, value in ((True, long_name), (False, huge), (True, "日本語.アプリ")):
            with self.subTest(utf8=utf8, n=len(value)):
                root = axml.parse(b.axml(b.manifest(package=value), utf8=utf8))
                self.assertEqual(root.attrs["package"], value)

    def test_stripped_attribute_names_resolve_through_resource_map(self):
        root = axml.parse(b.axml(b.manifest(), strip_names=True))
        sdk = next(root.find_all("uses-sdk"))
        self.assertEqual(sdk.attrs["minSdkVersion"], 26)
        self.assertEqual(root.attrs["versionName"], "1.2.3")

    def test_truncation_at_every_length_is_an_error_not_a_crash(self):
        data = b.axml(b.manifest())
        for n in range(0, len(data), 7):
            with self.subTest(n=n):
                try:
                    axml.parse(data[:n])
                except axml.AxmlError:
                    pass

    def test_not_binary_xml(self):
        with self.assertRaises(axml.AxmlError):
            axml.parse(b'<?xml version="1.0"?><manifest/>')


class Apk(Tmp):
    def test_report(self):
        p = self.path("app.apk")
        b.apk(p, b.axml(b.manifest()), libs=("arm64-v8a", "x86_64"), v1=True, v2=True)
        r = artifact.inspect(p)
        self.assertEqual((r["id"], r["version"], r["build"]), ("com.example.app", "1.2.3", "7"))
        self.assertEqual((r["min_sdk"], r["target_sdk"]), (26, 35))
        self.assertEqual(r["abis"], ["arm64-v8a", "x86_64"])
        self.assertTrue(r["debuggable"])
        self.assertEqual(r["launcher"], "com.example.app.MainActivity")
        self.assertEqual(r["signing"], ["v1", "v2+"])

    def test_unsigned_release_without_launcher_or_native_code(self):
        p = self.path("lib.apk")
        b.apk(p, b.axml(b.manifest(debuggable=False, launcher="")))
        r = artifact.inspect(p)
        self.assertEqual((r["signing"], r["abis"], r["launcher"], r["debuggable"]), ([], [], "", False))

    def test_launcher_skips_disabled_alias_and_keeps_manifest_order(self):
        # Shape of a real store client: a disabled alias comes first, an enabled one next.
        before = (b.launcher_alias(".CalculatorAlias", False), b.launcher_alias(".IconAlias", True))
        for strip in (False, True):
            with self.subTest(strip_names=strip):
                p = self.path(f"alias{strip}.apk")
                b.apk(p, b.axml(b.manifest(before=before), strip_names=strip))
                self.assertEqual(artifact.inspect(p)["launcher"], "com.example.app.IconAlias")

    def test_preview_codename_min_sdk(self):
        p = self.path("preview.apk")
        b.apk(p, b.axml(b.manifest(min_sdk="Baklava", min_type=b.STR)))
        self.assertEqual(artifact.inspect(p)["min_sdk"], "Baklava")

    def test_bad_inputs_have_one_line_reasons(self):
        cases = {
            "notzip.apk": b"hello",
            "nomanifest.apk": None,
        }
        p = self.path("notzip.apk")
        with open(p, "wb") as f:
            f.write(cases["notzip.apk"])
        with self.assertRaisesRegex(artifact.ArtifactError, "not a valid zip"):
            artifact.inspect(p)
        p = self.path("nomanifest.apk")
        b.ipa(p, b.info_plist())
        with self.assertRaisesRegex(artifact.ArtifactError, "no AndroidManifest.xml"):
            artifact.inspect(p)
        with self.assertRaisesRegex(artifact.ArtifactError, "not a valid zip"):
            open(self.path("x.aab"), "wb").close()
            artifact.inspect(self.path("x.aab"))
        with self.assertRaisesRegex(artifact.ArtifactError, "not found"):
            artifact.inspect(self.path("missing.apk"))


class Ipa(Tmp):
    NOW = datetime.datetime(2026, 11, 14, 12, 0)

    def test_development_profile(self):
        p = self.path("app.ipa")
        b.ipa(p, b.info_plist(), b.mobileprovision())
        r = artifact.inspect(p, now=self.NOW)
        self.assertEqual((r["id"], r["version"], r["build"], r["min_os"]),
                         ("com.example.app", "1.2.3", "42", "17.0"))
        self.assertEqual((r["families"], r["target"]), (["iphone", "ipad"], "device"))
        prof = r["profile"]
        self.assertEqual((prof["kind"], prof["team"], prof["expired"]), ("development", "ABCDE12345", False))
        self.assertEqual(prof["devices"], ["00008150-0000000000000001"])
        self.assertEqual(prof["expires"], "2030-01-01T00:00:00Z")

    def test_expired_yesterday(self):
        p = self.path("old.ipa")
        b.ipa(p, b.info_plist(), b.mobileprovision(expires=self.NOW - datetime.timedelta(days=1)))
        self.assertTrue(artifact.inspect(p, now=self.NOW)["profile"]["expired"])

    def test_profile_kinds(self):
        cases = [
            (dict(get_task_allow=False), "ad-hoc"),
            (dict(all_devices=True, get_task_allow=False), "enterprise"),
            (dict(devices=None, get_task_allow=False), "app-store"),
        ]
        for kwargs, kind in cases:
            with self.subTest(kind=kind):
                p = self.path(f"{kind}.ipa")
                b.ipa(p, b.info_plist(), b.mobileprovision(**kwargs))
                self.assertEqual(artifact.inspect(p, now=self.NOW)["profile"]["kind"], kind)

    def test_simulator_app_directory(self):
        app = self.path("Example.app")
        os.mkdir(app)
        with open(os.path.join(app, "Info.plist"), "wb") as f:
            f.write(b.info_plist(simulator=True, families=(1,)))
        r = artifact.inspect(app)
        self.assertEqual((r["type"], r["target"], r["families"], r["profile"]),
                         ("app", "simulator", ["iphone"], None))

    def test_bad_inputs(self):
        p = self.path("empty.ipa")
        b.apk(p, b.axml(b.manifest()))
        with self.assertRaisesRegex(artifact.ArtifactError, "no Payload"):
            artifact.inspect(p)
        p = self.path("badprofile.ipa")
        b.ipa(p, b.info_plist(), b"\x30\x80garbage")
        with self.assertRaisesRegex(artifact.ArtifactError, "no property list"):
            artifact.inspect(p)
        p = self.path("badplist.ipa")
        b.ipa(p, b"not a plist")
        with self.assertRaises(artifact.ArtifactError):
            artifact.inspect(p)


class InspectCommand(Tmp):
    def test_json_and_text(self):
        p = self.path("app.apk")
        b.apk(p, b.axml(b.manifest()), v2=True)
        r = run_cli("inspect", p, "--json")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(json.loads(r.stdout)["min_sdk"], 26)
        r = run_cli("inspect", p)
        self.assertIn("min_sdk:     26", r.stdout)

    def test_ipa_text_shows_expired_profile_and_udids(self):
        p = self.path("app.ipa")
        b.ipa(p, b.info_plist(), b.mobileprovision(expires=datetime.datetime(2020, 1, 1)))
        r = run_cli("inspect", p)
        self.assertIn("EXPIRED", r.stdout)
        self.assertIn("00008150-0000000000000001", r.stdout)

    def test_corrupt_file_exits_2_without_traceback(self):
        p = self.path("trunc.apk")
        b.apk(p, b.axml(b.manifest()))
        with open(p, "rb") as f:
            data = f.read()
        with open(p, "wb") as f:
            f.write(data[: len(data) // 2])
        r = run_cli("inspect", p)
        self.assertEqual(r.returncode, 2)
        self.assertNotIn("Traceback", r.stderr)
        self.assertEqual(len(r.stderr.strip().splitlines()), 1)


if __name__ == "__main__":
    unittest.main()
