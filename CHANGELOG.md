# Changelog

## 0.1.0 - 2026-11-15

First release. devdrop installs one mobile build on every attached
Android and iOS device, emulator and simulator, with the platform tools
already on the machine and no Python dependencies.

- `devices` and `doctor`: one list of Android devices (adb) and iPhones,
  iPads and simulators (devicectl, simctl), with each device's state and
  what to fix when it is not ready.
- `inspect`: what an `.apk`, `.aab`, `.ipa` or `.app` contains, read with
  the standard library: versions, SDK and iOS minimums, native ABIs and
  16 KB page-size readiness, signing, the launcher activity, and the
  provisioning profile's type, devices and expiry.
- `install`, `launch`, `uninstall` on every selected device at once, with
  a check before each install that skips devices the build cannot go on
  and says why, and plain reasons for install failures.
- App Bundles (`.aab`) through bundletool: each device gets the APK set
  built for it.
- `boot` and `shutdown` for emulators and simulators; `install --boot`.
- `pair` and `connect` over Wi-Fi; `install --watch` installs on devices
  as they appear.
- `--to` selectors (platform, kind, API or iOS version, names, groups,
  ids, patterns) and `devdrop.toml` aliases and groups.
- `--logs DIR` on `install --launch` and `launch`: watches each app after
  it starts, reports a crash with its cause, and saves each device's log.
- `--junit FILE`: one JUnit testcase per device, for CI dashboards.
- Tested end to end on Android emulators (API 23 to 37, including 16 KB
  page-size images), iOS 18.5 and 26.3 simulators, and a physical iPhone
  with a development-signed build.
