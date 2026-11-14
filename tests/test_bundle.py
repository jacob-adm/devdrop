# SPDX-License-Identifier: Apache-2.0
"""Android App Bundles: protobuf manifest, inspect, signing and install through bundletool."""

import json
import os
import stat
import tempfile
import unittest

from devdrop import artifact, axml, bundle, protoxml
from tests import builders as b
from tests.fakes import FakeTools
from tests.test_devices import devdrop
from tests.test_install import adb_rules
from tests.test_pages import elf64


class ProtoXml(unittest.TestCase):
    def test_same_tree_as_the_binary_xml_reader(self):
        for strip in (False, True):
            with self.subTest(strip_names=strip):
                m = b.manifest(before=(b.launcher_alias(".Off", False), b.launcher_alias(".On", True)))
                proto = protoxml.parse(b.proto_xml(m, strip))
                binary = axml.parse(b.axml(m, strip_names=strip))

                def flat(e):
                    return [(e.tag, e.attrs)] + [x for c in e.children for x in flat(c)]
                self.assertEqual(flat(proto), flat(binary))

    def test_typed_values(self):
        root = protoxml.parse(b.proto_xml(("manifest", [("versionCode", b.INT, 7), ("flag", b.BOOL, False),
                                                        ("ref", b.REF, 0x7F010001), ("hex", b.HEX, 0xFFFFFFFF)], [])))
        self.assertEqual(root.attrs, {"versionCode": 7, "flag": False, "ref": "@0x7f010001", "hex": -1})

    def test_damaged_input(self):
        good = b.proto_xml(b.manifest())
        for bad in (good[:5], good[:-3], b"\x0f\x00", b"\xff" * 12, b"\x12\x00"):
            with self.subTest(bad=bad[:8]):
                with self.assertRaises(axml.AxmlError):
                    protoxml.parse(bad)
        deep = b""
        for _ in range(100):
            deep = b._pb(1, 2, b._pb(3, 2, b"x") + b._pb(5, 2, deep))
        with self.assertRaisesRegex(axml.AxmlError, "too deeply"):
            protoxml.parse(deep)


class InspectBundle(unittest.TestCase):
    def setUp(self):
        self._t = tempfile.TemporaryDirectory()
        self.addCleanup(self._t.cleanup)
        self.path = os.path.join(self._t.name, "app.aab")

    def test_report(self):
        b.aab(self.path, b.manifest(min_sdk=26, version_code=9), libs=[
            ("arm64-v8a/libok.so", elf64(0x4000)), ("x86_64/lib4k.so", elf64(0x1000)),
            ("armeabi-v7a/lib32.so", b"\x7fELF\x01")])
        r = artifact.inspect(self.path)
        self.assertEqual((r["type"], r["id"], r["build"], r["min_sdk"], r["launcher"], r["signing"]),
                         ("aab", "com.example.app", "9", 26, "com.example.app.MainActivity", None))
        self.assertEqual(r["abis"], ["arm64-v8a", "armeabi-v7a", "x86_64"])
        self.assertEqual((r["page_16k"]["ready"], [x["ready"] for x in r["page_16k"]["libraries"]]),
                         (False, [True, False]))
        fake = FakeTools()
        self.addCleanup(fake.close)
        self.assertIn("signing:     at install (bundletool)", devdrop(fake, "inspect", self.path).stdout)

    def test_not_a_bundle(self):
        b.apk(self.path, b.axml(b.manifest()))
        with self.assertRaisesRegex(artifact.ArtifactError, "not an App Bundle"):
            artifact.inspect(self.path)


class Signing(unittest.TestCase):
    def setUp(self):
        self._t = tempfile.TemporaryDirectory()
        self.addCleanup(self._t.cleanup)
        self.dir = self._t.name
        self.ks = os.path.join(self.dir, "k.jks")
        open(self.ks, "w").close()

    def test_keystore_passwords_go_to_private_files(self):
        s = bundle.signing(self.ks, "a", self.dir, env={"DEVDROP_KS_PASS": "s3cret"})
        self.assertEqual((s.keystore, s.alias), (self.ks, "a"))
        for f, want in ((s.ks_pass_file, "s3cret"), (s.key_pass_file, "s3cret")):
            with open(f) as fh:
                self.assertEqual(fh.read(), want)
            if os.name != "nt":
                self.assertEqual(stat.S_IMODE(os.stat(f).st_mode), 0o600)
        s = bundle.signing(self.ks, "a", self.dir, env={"DEVDROP_KS_PASS": "x", "DEVDROP_KEY_PASS": "y"})
        with open(s.key_pass_file) as fh:
            self.assertEqual(fh.read(), "y")

    def test_errors(self):
        cases = [((self.ks, None, {}), "needs --ks-alias"),
                 ((self.ks + "x", "a", {}), "not found"),
                 ((self.ks, "a", {}), "DEVDROP_KS_PASS")]
        for args, msg in cases:
            with self.subTest(msg=msg), self.assertRaisesRegex(bundle.BundleError, msg):
                bundle.signing(args[0], args[1], self.dir, env=args[2])

    def test_debug_keystore_or_a_clear_error(self):
        home = os.environ.get("HOME")
        try:
            os.environ["HOME"] = self.dir
            with self.assertRaisesRegex(bundle.BundleError, "needs a signing key"):
                bundle.signing(None, None, self.dir, env={})
            os.makedirs(os.path.join(self.dir, ".android"))
            open(os.path.join(self.dir, ".android", "debug.keystore"), "w").close()
            s = bundle.signing(None, None, self.dir, env={})
            self.assertEqual(s.alias, "androiddebugkey")
        finally:
            os.environ["HOME"] = home

    def test_find_bundletool(self):
        fake = FakeTools()
        self.addCleanup(fake.close)
        jar = os.path.join(self.dir, "bt.jar")
        empty = {"PATH": fake.bin}
        with self.assertRaisesRegex(bundle.BundletoolMissing, "BUNDLETOOL_JAR"):
            bundle.find_bundletool(dict(empty, BUNDLETOOL_JAR=jar))
        open(jar, "w").close()
        with self.assertRaisesRegex(bundle.BundletoolMissing, "java is not on PATH"):
            bundle.find_bundletool(dict(empty, BUNDLETOOL_JAR=jar))
        with self.assertRaisesRegex(bundle.BundletoolMissing, "bundletool not found"):
            bundle.find_bundletool(empty)
        java = fake.add("java", [{"match": []}])
        self.assertEqual(bundle.find_bundletool(dict(empty, BUNDLETOOL_JAR=jar)), [java, "-jar", jar])
        tool = fake.add("bundletool", [{"match": []}])
        self.assertEqual(bundle.find_bundletool(empty), [tool])


class InstallBundle(unittest.TestCase):
    def setUp(self):
        self.fake = FakeTools()
        self.addCleanup(self.fake.close)
        self.aab = os.path.join(self.fake.root, "app.aab")
        b.aab(self.aab, b.manifest(min_sdk=26), libs=[("arm64-v8a/libok.so", elf64(0x4000))])
        self.ks = os.path.join(self.fake.root, "k.jks")
        open(self.ks, "w").close()
        self.env = {"DEVDROP_KS_PASS": "pw"}
        self.fake.add("adb", adb_rules())

    def run_install(self, *extra):
        return devdrop(self.fake, "install", self.aab, "--ks", self.ks, "--ks-alias", "a", "--json", *extra,
                       env_extra=self.env)

    def test_acceptance_per_device_apk_sets_and_skip(self):
        self.fake.add("bundletool", [
            {"match": ["build-apks"], "stdout": ""},
            {"match": ["install-apks"], "stdout": "The APKs have been extracted in the directory: /tmp/x\n"}])
        out = json.loads(self.run_install("--launch").stdout)
        recs = {r["id"]: r for r in out["results"]}
        self.assertEqual((recs["AAA111"]["result"], recs["AAA111"]["stage"]), ("ok", "launch"))
        self.assertEqual(recs["CCC333"]["reason"], "API 23 < minSdk 26")
        calls = self.fake.calls("bundletool")
        built = sorted(next(a for a in c if a.startswith("--device-id=")) for c in calls if c[0] == "build-apks")
        self.assertEqual(built, ["--device-id=AAA111", "--device-id=BBB222"])
        for c in calls:
            self.assertFalse(any("pw" == a.split(":", 1)[-1] for a in c), "password leaked into argv")
        self.assertTrue(any(a.startswith("--ks-pass=file:") for a in calls[0]))

    def test_bundletool_and_install_failures(self):
        self.fake.add("bundletool", [
            {"match": ["build-apks", "--device-id=AAA111"], "rc": 1,
             "stderr": "[BT:1.18.3] Error: Incorrect keystore password.\n"},
            {"match": ["build-apks"], "stdout": ""},
            {"match": ["install-apks", "--device-id=BBB222"], "rc": 1,
             "stderr": "Failure [INSTALL_FAILED_UPDATE_INCOMPATIBLE: signatures do not match]\n"}])
        recs = {r["id"]: r for r in json.loads(self.run_install().stdout)["results"]}
        self.assertEqual((recs["AAA111"]["stage"], recs["AAA111"]["reason"]),
                         ("build-apks", "Incorrect keystore password."))
        self.assertEqual(recs["BBB222"]["stage"], "install")
        self.assertIn("use --clean", recs["BBB222"]["reason"])

    def test_missing_tool_or_key_stops_before_any_device(self):
        r = self.run_install()
        self.assertEqual(r.returncode, 4)
        self.assertIn("bundletool not found", r.stderr)
        self.fake.add("bundletool", [{"match": []}])
        r = devdrop(self.fake, "install", self.aab, "--ks", self.ks, env_extra=self.env)
        self.assertEqual((r.returncode, "needs --ks-alias" in r.stderr), (2, True))
        self.assertEqual(self.fake.calls("bundletool"), [])


if __name__ == "__main__":
    unittest.main()
