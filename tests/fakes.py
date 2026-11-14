# SPDX-License-Identifier: Apache-2.0
"""Stand-in adb / xcrun / xcodebuild executables for tests.

Each stand-in is a real executable on disk that devdrop runs through
subprocess, exactly as it runs the real tool. It answers from a rule list
(first rule whose tokens all appear in argv wins) and logs every argv.
Rules can depend on a marker file (when_file / unless_file) and create or
remove one (touch / remove), so one tool's action can change what another
reports, as when an emulator starts and adb then lists it.
"""

import json
import os
import sys
import tempfile

FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures")

_SCRIPT = r'''
import json, sys, time
rules = json.load(open(RULES, encoding="utf-8"))
argv = sys.argv[1:]
with open(LOG, "a", encoding="utf-8") as f:
    f.write(json.dumps(argv) + "\n")
import os
for r in rules:
    if "when_file" in r and not os.path.exists(r["when_file"]):
        continue
    if "unless_file" in r and os.path.exists(r["unless_file"]):
        continue
    if all(t in argv for t in r.get("match", [])):
        time.sleep(r.get("sleep", 0))
        if "touch" in r:
            open(r["touch"], "w").close()
        if "remove" in r and os.path.exists(r["remove"]):
            os.remove(r["remove"])
        if "write_after" in r:
            path = argv[argv.index(r["write_after"]) + 1]
            with open(path, "w", encoding="utf-8") as out:
                out.write(r["file"])
        sys.stdout.write(r.get("stdout", ""))
        sys.stderr.write(r.get("stderr", ""))
        sys.exit(r.get("rc", 0))
sys.stderr.write("fake: no rule for %r\n" % (argv,))
sys.exit(97)
'''


def fixture(name):
    with open(os.path.join(FIXTURES, name), encoding="utf-8") as f:
        return f.read()


class FakeTools:
    """A temp directory holding stand-in tools, plus their argv logs."""

    def __init__(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = self._tmp.name
        self.bin = os.path.join(self.root, "bin")
        os.makedirs(self.bin)

    def close(self):
        self._tmp.cleanup()

    def add(self, name, rules, directory=None):
        directory = directory or self.bin
        os.makedirs(directory, exist_ok=True)
        rules_path = os.path.join(self.root, f"{name}.rules.json")
        log_path = os.path.join(self.root, f"{name}.log")
        with open(rules_path, "w", encoding="utf-8") as f:
            json.dump(rules, f)
        script = os.path.join(self.root, f"{name}_fake.py")
        with open(script, "w", encoding="utf-8") as f:
            f.write(f"RULES = {rules_path!r}\nLOG = {log_path!r}\n{_SCRIPT}")
        if os.name == "nt":
            exe = os.path.join(directory, f"{name}.cmd")
            with open(exe, "w", encoding="utf-8") as f:
                f.write(f'@"{sys.executable}" "{script}" %*\r\n')
        else:
            exe = os.path.join(directory, name)
            with open(exe, "w", encoding="utf-8") as f:
                f.write(f"#!{sys.executable}\nimport runpy; runpy.run_path({script!r}, run_name='__main__')\n")
            os.chmod(exe, 0o755)
        return exe

    def calls(self, name):
        path = os.path.join(self.root, f"{name}.log")
        if not os.path.exists(path):
            return []
        with open(path, encoding="utf-8") as f:
            return [json.loads(line) for line in f]

    def env(self):
        """An environment where only the stand-ins exist: no real SDK or Xcode."""
        home = os.path.join(self.root, "home")
        os.makedirs(home, exist_ok=True)
        env = {k: v for k, v in os.environ.items()
               if k not in ("ANDROID_HOME", "ANDROID_SDK_ROOT", "XDG_CONFIG_HOME")}
        tmp = os.path.join(self.root, "tmp")
        os.makedirs(tmp, exist_ok=True)
        env.update(PATH=self.bin, HOME=home, USERPROFILE=home, LOCALAPPDATA=home,
                   TMPDIR=tmp, TMP=tmp, TEMP=tmp)
        if os.name == "nt":
            env["PATHEXT"] = ".CMD;.EXE"
        return env


def adb_rules(devices_text=None):
    return [
        {"match": ["devices", "-l"], "stdout": devices_text if devices_text is not None else fixture("adb_devices.txt")},
        {"match": ["-s", "emulator-5554", "getprop"], "stdout": fixture("getprop_emulator.txt")},
        {"match": ["-s", "37201FDH2000CK", "getprop"], "stdout": fixture("getprop_pixel8.txt")},
        {"match": ["-s", "adb-0A1B2C3D-Xy9zQw._adb-tls-connect._tcp", "getprop"],
         "stdout": fixture("getprop_pixel6.txt")},
        {"match": ["version"], "stdout": "Android Debug Bridge version 1.0.41\nVersion 36.0.0\n"},
    ]


def xcrun_rules(devicectl=True):
    rules = [
        {"match": ["simctl", "list"], "stdout": fixture("simctl_list.json")},
    ]
    if devicectl:
        rules = [
            {"match": ["--find", "devicectl"], "stdout": "/Applications/Xcode.app/devicectl\n"},
            {"match": ["devicectl", "list", "devices"], "write_after": "--json-output",
             "file": fixture("devicectl_list.json")},
        ] + rules
    else:
        rules.insert(0, {"match": ["--find", "devicectl"], "rc": 1,
                         "stderr": "xcrun: error: unable to find utility \"devicectl\"\n"})
    return rules


XCODEBUILD_RULES = [{"match": ["-version"], "stdout": "Xcode 26.2\nBuild version 17C52\n"}]
