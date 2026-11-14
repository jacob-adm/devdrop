# SPDX-License-Identifier: Apache-2.0
"""16 KB page-size readiness: ELF alignment, zip alignment, device page size, warnings."""

import io
import json
import os
import struct
import tempfile
import unittest
import zipfile

from devdrop import artifact, preflight
from devdrop.device import Device
from tests import builders as b
from tests.fakes import FakeTools
from tests.test_devices import devdrop
from tests.test_install import LAB, PROPS, adb_rules


def elf64(align, loads=3, machine=0xB7):
    """A minimal 64-bit little-endian ELF with `loads` PT_LOAD headers of the given alignment."""
    phoff, entsize = 64, 56
    header = (b"\x7fELF" + bytes([2, 1, 1, 0]) + b"\0" * 8
              + struct.pack("<HHIQQQIHHHHHH", 3, machine, 1, 0, phoff, 0, 0, 64, entsize, loads, 0, 0, 0))
    phdrs = b"".join(struct.pack("<IIQQQQQQ", 1, 5, i * 0x1000, i * 0x1000, i * 0x1000, 0x10, 0x10, align)
                     for i in range(loads))
    return header + phdrs


def apk_with_libs(path, libs, data_at=None, shift=0):
    """libs: [(zip path, bytes, compressed)]. Stored entries get an extra field so their
    data starts at a multiple of `data_at`, plus `shift` bytes."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("AndroidManifest.xml", b.axml(b.manifest()))
        for name, data, compressed in libs:
            info = zipfile.ZipInfo(name)
            info.compress_type = zipfile.ZIP_DEFLATED if compressed else zipfile.ZIP_STORED
            if data_at and not compressed:
                base = buf.tell() + 30 + len(name)
                pad = (shift - base) % data_at
                if pad < 4:
                    pad += data_at
                info.extra = struct.pack("<HH", 0xD935, pad - 4) + b"\0" * (pad - 4)
            z.writestr(info, data)
    with open(path, "wb") as f:
        f.write(buf.getvalue())


class ElfAlign(unittest.TestCase):
    def test_reads_smallest_load_alignment(self):
        self.assertEqual(artifact.elf_load_align(elf64(0x4000)), 0x4000)
        self.assertEqual(artifact.elf_load_align(elf64(0x1000)), 0x1000)
        self.assertIsNone(artifact.elf_load_align(elf64(0x4000, loads=0)))
        self.assertIsNone(artifact.elf_load_align(b"\x7fELF\x01\x01" + b"\0" * 80))   # 32-bit
        self.assertIsNone(artifact.elf_load_align(b"not elf"))
        truncated = elf64(0x4000)[:64 + 56 + 10]                                   # second header cut off
        self.assertEqual(artifact.elf_load_align(truncated), 0x4000)


class ApkPages(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def page(self, libs, **kw):
        p = os.path.join(self.tmp.name, "a.apk")
        apk_with_libs(p, libs, **kw)
        return artifact.inspect(p)["page_16k"]

    def test_stored_16k_elf_on_a_16k_boundary_is_ready(self):
        r = self.page([("lib/arm64-v8a/libok.so", elf64(0x4000), False)], data_at=16384)
        self.assertEqual((r["native_64bit"], r["ready"]), (True, True))
        self.assertEqual(r["libraries"][0]["zip_aligned_16k"], True)

    def test_stored_16k_elf_4k_off_the_boundary_is_not_ready(self):
        r = self.page([("lib/x86_64/libz.so", elf64(0x4000), False)], data_at=16384, shift=4096)
        lib = r["libraries"][0]
        self.assertEqual((lib["zip_aligned_16k"], lib["ready"], r["ready"]), (False, False, False))
        self.assertEqual(preflight._why(lib), "not on a 16 KB boundary in the APK")

    def test_compressed_4k_elf_is_not_ready_and_has_no_zip_check(self):
        r = self.page([("lib/arm64-v8a/lib4k.so", elf64(0x1000), True)])
        lib = r["libraries"][0]
        self.assertEqual((r["ready"], lib["elf_align"], lib["stored"], lib["zip_aligned_16k"]),
                         (False, 0x1000, False, None))

    def test_compressed_16k_elf_is_ready(self):
        self.assertTrue(self.page([("lib/arm64-v8a/lib.so", elf64(0x4000), True)])["ready"])

    def test_32_bit_and_other_paths_are_ignored(self):
        r = self.page([("lib/armeabi-v7a/lib32.so", elf64(0x1000), True),
                       ("lib/arm64-v8a/sub/libx.so", elf64(0x1000), True),
                       ("assets/lib.so", elf64(0x1000), True)])
        self.assertEqual(r, {"native_64bit": False, "ready": True, "libraries": []})


class DevicePageSize(unittest.TestCase):
    def test_getconf_on_android_15_and_later_only(self):
        fake = FakeTools()
        self.addCleanup(fake.close)
        lab = [("NEW35", 35, "15", "Pixel 9"), ("OLD34", 34, "14", "Pixel 8")]
        rules = adb_rules(devices=lab, extra=[
            {"match": ["-s", "NEW35", "getconf", "PAGE_SIZE"], "stdout": "16384\n"},
            {"match": ["-s", "OLD34", "getconf"], "stdout": "4096\n"}])
        fake.add("adb", rules)
        out = json.loads(devdrop(fake, "devices", "--json").stdout)
        pages = {d["id"]: d["page_size"] for d in out["devices"]}
        self.assertEqual(pages, {"NEW35": 16384, "OLD34": None})
        self.assertFalse(any("OLD34" in c and "getconf" in c for c in fake.calls("adb")))
        self.assertIn("16 KB pages", devdrop(fake, "devices").stdout)


class Warnings(unittest.TestCase):
    PAGE = {"native_64bit": True, "ready": False, "libraries": [
        {"path": "lib/arm64-v8a/libfoo.so", "elf_align": 4096, "stored": True, "zip_aligned_16k": True,
         "ready": False}]}

    def test_only_16k_android_devices_with_unready_code(self):
        report = {"platform": "android", "page_16k": self.PAGE}
        dev16 = Device("A", "android", "physical", "ready", page_size=16384)
        dev4 = Device("B", "android", "physical", "ready", page_size=4096)
        (w,) = preflight.warnings(report, dev16)
        self.assertIn("lib/arm64-v8a/libfoo.so (ELF aligned to 4 KB)", w)
        self.assertEqual(preflight.warnings(report, dev4), [])
        self.assertEqual(preflight.warnings({"platform": "android", "page_16k": dict(self.PAGE, ready=True)},
                                            dev16), [])
        self.assertEqual(preflight._why(dict(self.PAGE["libraries"][0], elf_align=None)),
                         "not a readable 64-bit ELF")

    def test_install_record_and_text_carry_the_warning(self):
        fake = FakeTools()
        self.addCleanup(fake.close)
        fake.add("adb", adb_rules(devices=[("P16", 35, "15", "Pixel 9")],
                                  extra=[{"match": ["getconf", "PAGE_SIZE"], "stdout": "16384\n"}]))
        apk = os.path.join(fake.root, "n.apk")
        apk_with_libs(apk, [("lib/arm64-v8a/libfoo.so", elf64(0x1000), True)])
        with zipfile.ZipFile(apk, "a") as z:
            z.writestr("META-INF/CERT.SF", b"x")
        rec = json.loads(devdrop(fake, "install", apk, "--json").stdout)["results"][0]
        self.assertEqual((rec["result"], rec["page_size"]), ("ok", 16384))
        self.assertEqual(len(rec["warnings"]), 1)
        text = devdrop(fake, "install", apk).stdout
        self.assertIn("warning: 16 KB page-size device", text)
        self.assertIn("16k_pages:   NOT ready", devdrop(fake, "inspect", apk).stdout)


if __name__ == "__main__":
    unittest.main()
