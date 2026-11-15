# Security

devdrop reads build files and runs the platform tools already on your machine (`adb`, `xcrun
devicectl`, `xcrun simctl`, the Android emulator and, for `.aab` files, bundletool). It installs and
starts apps on the devices you select. It does not run the app on your computer.

## What devdrop does to keep a run contained

- **Builds are untrusted input.** `inspect` reads `.apk`, `.aab`, `.ipa` and `.app` files with the
  standard library and bounds-checks every offset. A damaged or crafted build is reported as
  unreadable; it does not crash devdrop. The readers are fuzz-tested.
- **Unpacking an `.ipa`** writes only inside a private temporary directory: entries with `..` or an
  absolute path are skipped, and no symlinks are created. The directory is removed after the run.
- **Tools are run directly, never through a shell,** with arguments passed as a list.
- **Keystore passwords** for `.aab` installs reach bundletool through files only you can read (mode
  0600), never on the command line, so they do not show in a process list. devdrop never creates
  signing keys.
- **Text from devices** (names, models) is escaped before it is printed, so a device cannot send
  control sequences to your terminal.
- **`--logs DIR`** saves each device's log for the watch window. Logs can contain whatever the apps on
  the device write; keep them as private as the device.

## Reporting a vulnerability

Please report security problems privately, through GitHub's "Report a vulnerability" button on the
repository's Security tab. Please do not open a public issue.
