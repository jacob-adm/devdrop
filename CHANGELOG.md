# Changelog

## 0.3.2 - 2026-11-20

- The signing-key check before an install now covers Android 9 and
  later (it was Android 15 and later).

## 0.3.1 - 2026-11-20

- `install --watch`: an install under way when Ctrl-C arrives now
  finishes and is in the summary. It used to complete on the device but
  be missing from the results.
- `--logs` on a physical iPhone with pymobiledevice3: an app that
  crashed is reported as failed when its log holds the crash line. iOS
  can list a process for the app again right after a crash, which made
  the crash read as "running".

## 0.3.0 - 2026-11-20

- `install --dry-run`: checks the build against every selected device
  and reports which would take it and which would be skipped, without
  installing or launching anything.
- `install` asks each Android device what it already holds and skips it
  before sending when the installed copy has a newer `versionCode`, is
  signed with a different key (APKs on Android 15 and later), or the
  device has less free storage than the APK's size. These were reported
  only after the device refused the upload.
- `inspect` shows an APK's signing certificates (`certs`, `cert_ids`)
  and file size.
- `install` accepts an Android build and an iOS build in one run:
  `devdrop install app.apk app.ipa`. Each device gets its platform's
  build and one report covers both. Every `--json` install record now
  has `artifact`, the path of the build it was for.
- New page, `docs/json.md`: every `--json` field of every command, what
  stays stable between releases, and a short sequence for scripts and
  coding agents. A test keeps the page and the output in step.
- `--logs` and `--screenshot` on a physical iPhone or iPad, when the
  optional `pymobiledevice3` tool is on `PATH`: the app's lines of the
  device log are saved and checked for a crash, and the screen is saved
  as a PNG. Without the tool nothing changes. `doctor` says whether it
  was found.

## 0.2.0 - 2026-11-20

- `doctor` shows which Xcode `xcode-select` points at and the installed
  iOS simulator runtimes, with the command to download one when there
  is none.
- `--url URL` on `install --launch` and `launch`: opens a link in the app
  on every device, for testing deep links.
- `--screenshot DIR` on `install --launch` and `launch`: saves what each
  Android device, emulator and iOS simulator shows after the app starts,
  one PNG per device. A physical iPhone gets a warning instead.

## 0.1.1 - 2026-11-19

- `install --watch` waits until a newly seen Android device has finished
  booting. adb lists a device as ready a few seconds before Android can
  take an install; the watch used to install in that gap, fail with
  "Can't find service: package" and not try that device again.

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
