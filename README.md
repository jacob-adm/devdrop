# devdrop

Install one mobile build on every attached Android and iOS device.

devdrop drives the platform tools you already have: `adb` for Android
phones, tablets and emulators, and `xcrun devicectl` / `xcrun simctl` for
iPhones, iPads and iOS simulators. It is a single Python package with no
dependencies.

Status: early development. All commands work for Android; installing
on iOS devices and simulators is next.

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
devdrop inspect PATH [--json]      show what an .apk, .ipa or .app contains
devdrop install PATH [--to SEL] [--launch] [--clean] [--jobs N] [--json]
devdrop launch APP_ID [--to SEL] [--jobs N] [--json]
devdrop uninstall APP_ID [--to SEL] [--jobs N] [--json]
```

`install` sends the build to every selected device at once. Before
touching a device it checks that the build can go there and skips the
device with a plain reason if not: the device is not ready, its API
level is below the app's minSdk, the build has no native code for its
CPU, or the APK is unsigned. Install failures from the device are
translated too (for example a signing-key clash says to use `--clean`).
`--clean` uninstalls the app first, which also removes its data;
`--launch` starts it afterwards. Each device's result prints as it
finishes, followed by a count.

`inspect` reads the build directly, without the platform tools:

- APK: package, version name and code, minimum and target SDK, native
  ABIs, whether it is debuggable, the launcher activity, and signing
  schemes (v1, v2 or later). Android App Bundles (.aab) are not read yet.
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
| `emulator-5554` | a device id |
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

## Exit codes

| Code | Meaning |
|---|---|
| 0 | success |
| 1 | a device was skipped or failed, a platform tool failed, or `doctor` found something to fix |
| 2 | usage or configuration error, or a build devdrop cannot read |
| 3 | no device matched `--to` |
| 4 | the platform tools needed were not found |

## License

Apache-2.0
