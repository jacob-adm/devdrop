# Commands

What each devdrop command does, what `install` checks first, and the exit codes.

```
devdrop devices [--json] [--all]   list attached devices and their state
devdrop doctor                     check the toolchain; say what to fix on each device
devdrop inspect PATH [--json]      show what an .apk, .aab, .ipa or .app contains
devdrop install PATH [--to SEL] [--launch] [--clean] [--dry-run] [--jobs N] [--json]
                [--ks KEYSTORE --ks-alias ALIAS]      (.aab only)
                [--logs DIR [--log-seconds N]] [--watch [--watch-timeout S]]
                [--screenshot DIR [--screenshot-seconds N]] [--url URL]
devdrop launch APP_ID [--to SEL] [--logs DIR [--log-seconds N]] [--jobs N] [--json]
                [--screenshot DIR [--screenshot-seconds N]] [--url URL]
devdrop uninstall APP_ID [--to SEL] [--jobs N] [--json]
devdrop pair HOST:PORT CODE        pair with an Android device over Wi-Fi
devdrop connect [HOST:PORT ...]    connect over Wi-Fi (default: every paired device found by mDNS)
devdrop boot --to SEL [--show] [--boot-timeout S] [--json]
devdrop shutdown [--to SEL] [--json]
```

## devices and doctor

`devices` lists each device once: id, name, platform, kind (physical,
emulator, simulator), model, OS version and state. Only `ready` devices
take installs; any other state names what needs doing, and `doctor` gives
the fix. Simulators are listed when booted; `--all` adds shut-down ones.

On a Mac, `doctor` also shows which Xcode is selected (`xcode-select -p`;
every `xcrun` call uses that one) and the installed iOS simulator
runtimes. With none installed no simulator can be listed; `doctor` then
names the command that downloads one, `xcodebuild -downloadPlatform iOS`.

## inspect

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

## install

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

With `--dry-run`, `install` stops after those checks. Each device that
would take the build is reported `ok` at stage `preflight` with
`would install; nothing was sent (dry run)` (and `"dry_run": true` in
`--json`); the others are skipped with their reason. Nothing is
installed, uninstalled or launched, and the exit code follows the usual
rule, so a CI job can fail before a release when a device in the lab
would be left out. `--dry-run` cannot be combined with `--watch`;
`--boot` still starts the devices it names.

Each device's result prints as it
finishes, followed by a count. With `--json`, every result also names
the device's maker, model, kind, OS version and Android API level, so a
failure can be traced to a device family.

iOS devices are driven with `xcrun devicectl` and simulators with
`xcrun simctl`; an `.ipa` is unpacked to its `.app` first. Only booted
simulators are targets. `launch` and `uninstall` take an Android package
name or an iOS bundle id and act on every selected device.

## Launch logs

With `--logs DIR`, `install --launch` and `launch` watch the app for
`--log-seconds` (default 5) after starting it: an app that is no longer
running is reported as failed at stage `running`, with the first crash
line found (an Android `FATAL EXCEPTION`, a Swift `Fatal error`, an
uncaught Objective-C exception), and each device's log for that window
is saved as `DIR/<device>.log` (Android: logcat main and crash buffers;
iOS simulator: the app's process in the unified log). On a physical
iPhone devdrop checks that the app is running but saves no log.

## Opening a link

With `--url URL`, `install --launch` and `launch` open that link in the
app instead of its start screen, to test a deep link on every device at
once. It works with `--logs` and `--screenshot`.

- Android: a `VIEW` intent for the URL, limited to the app's package. If
  no activity of the app declares the link, the device is reported as
  failed with `the app has no activity that opens this URL`.
- iOS simulator: the app is started, then `simctl openurl` hands the URL
  to iOS, which gives it to whichever installed app claims it. A link no
  app claims fails the launch.
- Physical iPhone or iPad: the URL is passed with the launch
  (`devicectl ... --payload-url`).

## Screenshots

With `--screenshot DIR`, `install --launch` and `launch` save what each
device shows `--screenshot-seconds` (default 3) after the app starts, as
`DIR/<device>.png`. With `--logs` as well, the picture is taken after the
log window if that is longer. Android devices and emulators use
`screencap`; iOS simulators use `simctl io`. A physical iPhone or iPad
has no screenshot command in `devicectl`, so its result carries the
warning `no screenshot: not available on a physical iPhone or iPad`.

A screenshot that cannot be taken never changes a result: the device
stays `ok` and the reason is in its warnings. The picture is taken even
when `--logs` finds that the app has exited, so it shows what a user
would see. Each `--json` record names its file in `screenshot`.

## App Bundles (.aab)

An `.aab` is installed through
[bundletool](https://developer.android.com/tools/bundletool) (on `PATH`,
or `BUNDLETOOL_JAR` with Java): for each device devdrop builds the APK
set that device needs and installs it. The APKs are signed with `--ks`
and `--ks-alias`, with the keystore password in `DEVDROP_KS_PASS` (and
`DEVDROP_KEY_PASS` if the key's differs); without `--ks`, the Android
debug keystore (`~/.android/debug.keystore`) is used if it exists.
Passwords reach bundletool through files only the user can read, never
on the command line. devdrop does not create keys.

## Settings

`DEVDROP_TOOL_TIMEOUT` sets how many seconds devdrop waits for a single
`adb` or `xcrun` call before treating the device as not responding
(default 30). Installs and boots have their own, longer limits.

## Exit codes

| Code | Meaning |
|---|---|
| 0 | success |
| 1 | a device was skipped or failed, a platform tool failed, or `doctor` found something to fix |
| 2 | usage or configuration error, or a build devdrop cannot read |
| 3 | no device matched `--to` |
| 4 | the platform tools needed were not found (adb, xcrun, or bundletool for an .aab) |
