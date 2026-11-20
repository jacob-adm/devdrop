# Testing

How devdrop is tested, and on which devices.

```
python3 -m unittest discover -s tests          # unit, CLI and fuzz tests (no devices needed)
DEVDROP_E2E=1 python3 -m unittest tests.e2e.test_e2e -v   # end to end on emulators and simulators
DEVDROP_E2E=1 DEVDROP_E2E_IPA=app.ipa DEVDROP_E2E_ANCHOR=com.example.other \
    python3 -m unittest tests.e2e.test_physical -v           # end to end on an attached iPhone
```

The iPhone test needs a build signed with a development profile that
lists the phone, and a second app from the same team left installed:
iOS withdraws the trust in a team when its last app is removed. The
test's docstring explains both.

`ci/linux.sh` runs the unit suite on Linux in Docker (Python 3.11 and
3.13 by default, `DEVDROP_LINUX_PYTHONS` to change), using only the files
git tracks and no network.

The unit suite runs devdrop as a separate process against stand-in
`adb`, `xcrun` and `emulator` programs. `DEVDROP_FUZZ_N` sets the number
of fuzzed builds per reader (default 10,000). Branch coverage, measured
with coverage.py (development only):

```
python3 -m coverage run -m unittest discover -s tests
python3 -m coverage combine && python3 -m coverage report
```

The end-to-end suite installs devdrop into a fresh virtual environment,
builds real test apps (`tests/e2e/build_fixtures.py`: APKs with the
Android build-tools, iOS apps with `swiftc`), boots the device matrix
with `devdrop boot`, runs every command and failure case, checks each
result on the device with `adb` or `simctl`, and shuts down what it
started. It needs the Android SDK build-tools and emulator, the AVDs
named in `DEVDROP_E2E_AVDS` and, on macOS, an iOS simulator runtime.
The default AVDs are chosen where `adb` behaves differently rather than
by market share: API 23 (no `cmd package`, so launch uses the launcher
read from the manifest), 24, 29, 29 with 1 GB of RAM, 34, 35, 36 and 37,
plus 16 KB page-size images of 35 and 37.
On iOS it uses one iPhone simulator per installed runtime; install an
older runtime too (for example `xcodebuild -downloadPlatform iOS
-buildVersion 18.5`) to test more than the current iOS. Quick-boot
snapshots of the emulators the run started are deleted afterwards, so
repeated runs do not fill the disk. A
per-device report is written to `$DEVDROP_E2E_CACHE/e2e-report.json`.

## Tested on

Every row below passes. "End-to-end suite" means real test apps built for
the run, installed with devdrop, and each result checked on the device
itself with `adb`, `simctl` or `devicectl`.

| What | Where | How |
| --- | --- | --- |
| Reading builds, device lists, selection, pre-install checks, reports, every command against stand-in tools | Linux, macOS and Windows; Python 3.11 and 3.13 | Unit suite, in CI on every pull request. On Windows the stand-in tools are real `.exe` files, as the Android SDK's are. |
| Install, upgrade, downgrade, signing-key clash, launch, uninstall, launch logs and crash detection, App Bundles, boot and shutdown, `connect`, `--watch` | Android emulators (Google system images, arm64): API 23, 24, 29 (also with 1 GB of RAM), 34, 35, 36 and 37 | End-to-end suite |
| Refusals predicted before sending: a downgrade, and a different signing key on API 35 and 37 | Android emulators API 23, 35 and 37 | End-to-end suite: after devdrop skips the device, the same APK is given to `adb install` directly and the device must refuse it |
| 16 KB page-size readiness | 16 KB page-size images of API 35 and 37 | End-to-end suite |
| Install, launch, uninstall, launch logs and crash detection, boot and shutdown | iOS simulators: iOS 18.5 and 26.3 | End-to-end suite |
| `--screenshot` and `--url` | Android emulators API 23, 35 and 37; iOS simulator 26.3 | End-to-end suite: each record's PNG is read back; on Android the started activity shows the full link |
| A development-signed build inspected (its real provisioning profile), installed, launched with `--logs` and `--junit`, uninstalled | A physical iPhone, iOS 26 | `tests/e2e/test_physical.py`, each step checked on the phone with `devicectl` |
| `launch --url`, and the `--screenshot` warning without pymobiledevice3 | A physical iPhone, iOS 26 | By hand: the launch is accepted and the app runs |
| `launch --logs --screenshot` with pymobiledevice3 11.61 | A physical iPhone on USB, iOS 26 | By hand: the saved log holds only the app's lines and the PNG shows the app's screen |

Not tested:

- Crash detection from a physical iPhone's log (no crashing test app is
  signed for a device): unit suite only. The same goes for
  pymobiledevice3 with an iPhone attached over the network.
- The low-storage check on a device that is actually full: it is covered
  by the unit suite only.
- Reading the installed signing certificate on Android 9 to 14. The
  signing-key check is therefore off below Android 15.
- Physical Android phones, including vendor builds such as Samsung One UI
  or Xiaomi HyperOS, whose install prompts and restrictions differ from
  Google's images.
- Whether an app on a physical iPhone or an iOS simulator receives the
  link passed with `--url`: the test apps do not show it.
