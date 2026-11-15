# devdrop

Install one mobile build on every attached Android and iOS device.

devdrop drives the platform tools you already have: `adb` for Android
phones, tablets and emulators, and `xcrun devicectl` / `xcrun simctl` for
iPhones, iPads and iOS simulators. It is a single Python package with no
dependencies.

Status: early development (0.1.0 not yet released).

## Install

From a clone of this repository:

```
python3 -m pip install .
```

## Requirements

- Python 3.11 or later
- Android: Android SDK platform-tools (`adb`). devdrop looks in
  `$ANDROID_HOME`, `$ANDROID_SDK_ROOT`, the default SDK location for your
  OS, then `PATH`.
- iOS: macOS with Xcode. Physical devices need Xcode 15 or later
  (`devicectl`); simulators work with any Xcode that has `simctl`.

Either platform works without the other.

## Commands

```
devdrop devices [--json] [--all]   list attached devices and their state
devdrop doctor                     check the toolchain; say what to fix on each device
devdrop inspect PATH [--json]      show what an .apk, .aab, .ipa or .app contains
devdrop install PATH [--to SEL] [--launch] [--clean] [--jobs N] [--json]
                [--ks KEYSTORE --ks-alias ALIAS]      (.aab only)
                [--logs DIR [--log-seconds N]] [--watch [--watch-timeout S]]
devdrop launch APP_ID [--to SEL] [--logs DIR [--log-seconds N]] [--jobs N] [--json]
devdrop uninstall APP_ID [--to SEL] [--jobs N] [--json]
devdrop pair HOST:PORT CODE        pair with an Android device over Wi-Fi
devdrop connect [HOST:PORT ...]    connect over Wi-Fi (default: every paired device found by mDNS)
devdrop boot --to SEL [--show] [--boot-timeout S] [--json]
devdrop shutdown [--to SEL] [--json]
```

`install` sends the build to every selected device at once. Before
touching a device it checks that the build can go there and skips the
device with a plain reason if not: the device is not ready, its API
level is below the app's minSdk, the build has no native code for its
CPU, or the APK is unsigned. For iOS: a simulator build sent to a device
or the reverse, an iOS version below the app's minimum, an iPad-only app
on an iPhone, and for devices a missing, expired or App Store profile,
or a device whose UDID the profile does not list. Installing an APK whose
native code is not 16 KB ready on a 16 KB page-size device goes ahead
with a warning naming each library: on the 16 KB emulator images tested
(Android 15 and 17), such apps installed and ran, on Android 16+ in the
page-size compatibility mode, which shows a warning on first launch.
Install failures from the device are translated too (for example a signing-key clash says to use `--clean`).
`--clean` uninstalls the app first, which also removes its data;
`--launch` starts it afterwards.

With `--logs DIR`, `install --launch` and `launch` watch the app for
`--log-seconds` (default 5) after starting it: an app that is no longer
running is reported as failed at stage `running`, with the first crash
line found (an Android `FATAL EXCEPTION`, a Swift `Fatal error`, an
uncaught Objective-C exception), and each device's log for that window
is saved as `DIR/<device>.log` (Android: logcat main and crash buffers;
iOS simulator: the app's process in the unified log). On a physical
iPhone devdrop checks that the app is running but saves no log.

An `.aab` is installed through
[bundletool](https://developer.android.com/tools/bundletool) (on `PATH`,
or `BUNDLETOOL_JAR` with Java): for each device devdrop builds the APK
set that device needs and installs it. The APKs are signed with `--ks`
and `--ks-alias`, with the keystore password in `DEVDROP_KS_PASS` (and
`DEVDROP_KEY_PASS` if the key's differs); without `--ks`, the Android
debug keystore (`~/.android/debug.keystore`) is used if it exists.
Passwords reach bundletool through files only the user can read, never
on the command line. devdrop does not create keys.

Each device's result prints as it
finishes, followed by a count. With `--json`, every result also names
the device's maker, model, kind, OS version and Android API level, so a
failure can be traced to a device family.

iOS devices are driven with `xcrun devicectl` and simulators with
`xcrun simctl`; an `.ipa` is unpacked to its `.app` first. Only booted
simulators are targets. `launch` and `uninstall` take an Android package
name or an iOS bundle id and act on every selected device.

## Wi-Fi and devices that come and go

On Android 11 and later, turn on Developer options > Wireless debugging,
tap "Pair device with pairing code", and run `devdrop pair HOST:PORT
CODE` with the address and code the phone shows. After that, `devdrop
connect` finds paired phones on the network (mDNS) and connects to them;
`devdrop connect HOST:PORT` connects to one address. A phone attached by
both USB and Wi-Fi is listed and installed to once, over USB.

`install --watch` keeps running after the first pass: every device that
matches `--to` and becomes ready later (plugged in, booted, connected
over Wi-Fi) gets the build once, and its result prints as it finishes.
Ctrl-C, or `--watch-timeout SECONDS`, ends the run with the usual
summary and exit code.

## Emulators and simulators

Running Android emulators and booted iOS simulators are targets like any
other device (`--to emulator`, `--to sim`). `devices --all` also lists
stopped Android virtual devices (with the API level from their config)
and shut-down simulators.

`boot --to SEL` starts the stopped ones that `SEL` names and waits until
each has finished booting; `install --boot --to SEL` does the same before
installing. Emulators start without a window and keep running after
devdrop exits; `--show` opens their windows (and Simulator.app). Booting
always needs an explicit `--to`, so a default run never starts every
simulator on the machine. `shutdown` stops running emulators and
simulators and never touches physical devices.

`inspect` reads the build directly, without the platform tools:

- APK: package, version name and code, minimum and target SDK, native
  ABIs, whether it is debuggable, the launcher activity, and signing
  schemes (v1, v2 or later).
- Android App Bundle (.aab): the same fields, read from the bundle's
  protobuf manifest. A bundle is signed later, when APKs are built from it.
- APK native code and 16 KB memory pages: for each 64-bit library,
  whether every ELF LOAD segment is 16 KB aligned and, if it is stored
  uncompressed, whether it starts on a 16 KB boundary in the APK.
  Newer Android hardware uses 16 KB pages, and Google Play requires apps
  targeting Android 15 or later to support them.
- IPA or .app: bundle id, version and build, minimum iOS version, device
  families, whether it is built for devices or the simulator, and the
  embedded provisioning profile: type, team, the device UDIDs it allows
  and when it expires.

`devices` lists each device once: id, name, platform, kind (physical,
emulator, simulator), model, OS version and state. Only `ready` devices
take installs; any other state names what needs doing, and `doctor` gives
the fix. Simulators are listed when booted; `--all` adds shut-down ones.

## Choosing devices

`--to` takes comma-separated terms; a device is chosen if any term
matches. The default is `all`.

| Term | Chooses |
|---|---|
| `all` | every device |
| `android`, `ios` | one platform |
| `physical`, `emulator`, `simulator` (`sim`) | one kind |
| `api>=30`, `api<24`, ... | Android API level (`>=` `<=` `>` `<` `=`) |
| `ios>=18`, `ios=26.1`, ... | iOS version |
| `pixel8`, `lab` | an alias or group from `devdrop.toml` |
| `emulator-5554`, `"iPhone 16 Pro"` | a device id, or an exact model or name |
| `'Pixel*'`, `'*ipad*'` | model or name pattern (case-insensitive) |

## Device names

An optional `devdrop.toml` (in the current directory, else
`~/.config/devdrop/devdrop.toml`, or `--config PATH`) names devices and
groups them:

```toml
[devices]
pixel8 = "37201FDH2000CK"
iphone = "00008150-0000000000000001"

[groups]
lab = ["pixel8", "iphone"]
```

## Settings

`DEVDROP_TOOL_TIMEOUT` sets how many seconds devdrop waits for a single
`adb` or `xcrun` call before treating the device as not responding
(default 30). Installs and boots have their own, longer limits.

## Reports for CI

`--json` prints every device's result. `--junit FILE` (install, launch,
uninstall, boot, shutdown) also writes JUnit XML with one testcase per
device: skipped and failed devices carry devdrop's reason, and the
device's details, warnings and log path are in its output section, so
any CI dashboard shows which devices failed and why.

## Exit codes

| Code | Meaning |
|---|---|
| 0 | success |
| 1 | a device was skipped or failed, a platform tool failed, or `doctor` found something to fix |
| 2 | usage or configuration error, or a build devdrop cannot read |
| 3 | no device matched `--to` |
| 4 | the platform tools needed were not found (adb, xcrun, or bundletool for an .aab) |

## Testing

```
python3 -m unittest discover -s tests          # unit, CLI and fuzz tests (no devices needed)
DEVDROP_E2E=1 python3 -m unittest tests.e2e.test_e2e -v   # end to end on real devices
```

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

- Android emulators (Google system images, arm64): API 23, 24, 29 (also
  with 1 GB of RAM), 34, 35, 36 and 37, and 16 KB page-size images of
  API 35 and 37.
- iOS simulators: iOS 18.5 and 26.3.
- A physical iPhone on iOS 26: device listing, state checks and launch.
  Installing a signed build on a physical iPhone has not been tested yet.
- Not yet tested on physical Android phones, including vendor builds such
  as Samsung One UI or Xiaomi HyperOS, whose install prompts and
  restrictions differ from Google's images.

## License

Apache-2.0
