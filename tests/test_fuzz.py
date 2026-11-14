# SPDX-License-Identifier: Apache-2.0
"""Seeded fuzzing: damaged builds must give devdrop's own errors, never a crash.

DEVDROP_FUZZ_N sets the number of cases per reader (default 10000).
"""

import datetime
import os
import random
import struct
import tempfile
import unittest

from devdrop import artifact, axml
from tests import builders as b

N = int(os.environ.get("DEVDROP_FUZZ_N", "10000"))


def mutate(rng, data):
    data = bytearray(data)
    for _ in range(rng.randint(1, 8)):
        op = rng.randrange(5)
        if op == 0 and data:                                   # flip bits
            i = rng.randrange(len(data))
            data[i] ^= 1 << rng.randrange(8)
        elif op == 1:                                          # truncate
            del data[rng.randrange(len(data) + 1):]
        elif op == 2 and len(data) >= 4:                       # huge or zero length field
            i = rng.randrange(len(data) - 3)
            struct.pack_into("<I", data, i, rng.choice((0, 1, 0x7FFFFFFF, 0xFFFFFFFF, len(data))))
        elif op == 3:                                          # insert junk
            i = rng.randrange(len(data) + 1)
            data[i:i] = rng.randbytes(rng.randint(1, 16))
        elif op == 4 and data:                                 # duplicate a slice
            i = rng.randrange(len(data))
            data[i:i] = data[i:i + rng.randint(1, 64)]
    return bytes(data)


class Fuzz(unittest.TestCase):
    def test_binary_xml(self):
        rng = random.Random(1)
        seeds = [b.axml(b.manifest(), utf8=False), b.axml(b.manifest(), utf8=True),
                 b.axml(b.manifest(), strip_names=True)]
        for i in range(N):
            data = mutate(rng, rng.choice(seeds))
            try:
                axml.parse(data)
            except axml.AxmlError:
                pass
            except Exception as e:   # pragma: no cover - a failure prints the case
                self.fail(f"case {i}: {type(e).__name__}: {e} (input {data[:64].hex()}...)")

    def test_protobuf_xml(self):
        from devdrop import protoxml
        rng = random.Random(4)
        seeds = [b.proto_xml(b.manifest()), b.proto_xml(b.manifest(), strip_names=True)]
        for i in range(N):
            data = mutate(rng, rng.choice(seeds))
            try:
                protoxml.parse(data)
            except axml.AxmlError:
                pass
            except Exception as e:   # pragma: no cover
                self.fail(f"case {i}: {type(e).__name__}: {e} (input {data[:64].hex()}...)")

    def test_provisioning_profile(self):
        rng = random.Random(2)
        seed = b.mobileprovision()
        now = datetime.datetime(2026, 1, 1)
        for i in range(N):
            data = mutate(rng, seed)
            try:
                artifact.profile_from_bytes(data, now)
            except artifact.ArtifactError:
                pass
            except Exception as e:   # pragma: no cover
                self.fail(f"case {i}: {type(e).__name__}: {e}")

    def test_whole_files(self):
        rng = random.Random(3)
        with tempfile.TemporaryDirectory() as tmp:
            apk, ipa = os.path.join(tmp, "s.apk"), os.path.join(tmp, "s.ipa")
            b.apk(apk, b.axml(b.manifest()), libs=("arm64-v8a",), v1=True, v2=True)
            b.ipa(ipa, b.info_plist(), b.mobileprovision())
            aab = os.path.join(tmp, "s.aab")
            b.aab(aab, b.manifest(), libs=[("arm64-v8a/lib.so", b"\x7fELF\x02\x01" + b"\0" * 120)])
            seeds = []
            for path in (apk, ipa, aab):
                with open(path, "rb") as f:
                    seeds.append((os.path.splitext(path)[1], f.read()))
            target = {ext: os.path.join(tmp, "case" + ext) for ext, _ in seeds}
            for i in range(N):
                ext, seed = rng.choice(seeds)
                with open(target[ext], "wb") as f:
                    f.write(mutate(rng, seed))
                try:
                    artifact.inspect(target[ext])
                except artifact.ArtifactError:
                    pass
                except Exception as e:   # pragma: no cover
                    self.fail(f"case {i} ({ext}): {type(e).__name__}: {e}")

    def test_wrongly_typed_plist_values(self):
        values = [1, -1, 2 ** 40, "text", "", [], ["x"], [1, 2], {}, {"a": 1}, True, b"\x00",
                  datetime.datetime(1970, 1, 1), 1.5]
        info_keys = ["CFBundleIdentifier", "CFBundleShortVersionString", "CFBundleVersion", "MinimumOSVersion",
                     "UIDeviceFamily", "CFBundleSupportedPlatforms", "DTPlatformName"]
        prof_keys = ["Name", "TeamIdentifier", "ExpirationDate", "ProvisionedDevices",
                     "ProvisionsAllDevices", "Entitlements"]
        import plistlib
        with tempfile.TemporaryDirectory() as tmp:
            ipa = os.path.join(tmp, "t.ipa")
            app = os.path.join(tmp, "T.app")
            os.mkdir(app)
            for key in info_keys + prof_keys:
                for value in values:
                    info = plistlib.loads(b.info_plist())
                    prof = {"Name": "n", "TeamIdentifier": ["T"], "ExpirationDate": datetime.datetime(2030, 1, 1),
                            "ProvisionedDevices": ["U"], "Entitlements": {}}
                    (info if key in info_keys else prof)[key] = value
                    profile = b"\x30" + plistlib.dumps(prof)
                    b.ipa(ipa, plistlib.dumps(info), profile)
                    with open(os.path.join(app, "Info.plist"), "wb") as f:
                        f.write(plistlib.dumps(info))
                    with open(os.path.join(app, "embedded.mobileprovision"), "wb") as f:
                        f.write(profile)
                    for path in (ipa, app):
                        with self.subTest(key=key, value=value, path=os.path.basename(path)):
                            try:
                                report = artifact.inspect(path)
                            except artifact.ArtifactError:
                                continue
                            json_safe = __import__("json").dumps(report)   # report must be serialisable
                            self.assertTrue(json_safe)


if __name__ == "__main__":
    unittest.main()
