# devdrop

Install one mobile build on every attached Android and iOS device.

devdrop drives the platform tools you already have: `adb` for Android
phones, tablets and emulators, and `xcrun devicectl` / `xcrun simctl` for
iPhones, iPads and iOS simulators. It is a single Python package with no
dependencies.

Status: early development. `devices` and `doctor` work; `inspect`,
`install`, `launch` and `uninstall` are next.

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
```

`devices` lists each device once: id, name, platform, kind (physical,
emulator, simulator), model, OS version and state. Only `ready` devices
take installs; any other state names what needs doing, and `doctor` gives
the fix. Simulators are listed when booted; `--all` adds shut-down ones.

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
| 1 | a platform tool failed, or `doctor` found something to fix |
| 2 | usage or configuration error |
| 4 | neither `adb` nor `xcrun` was found |

## License

Apache-2.0
