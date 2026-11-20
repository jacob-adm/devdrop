# JSON output

The fields of every `--json` output, for scripts and coding agents.

What you can rely on:

- A field listed here is removed, renamed or given another type only in
  a release whose changelog entry says so under "Breaking".
- New fields can appear in any release. Ignore fields you do not know.
- Exit codes are part of the same contract: see
  [Exit codes](commands.md#exit-codes).
- `--json` writes one JSON document to standard output. Messages for
  people go to standard error.

## Results: install, launch, uninstall, boot, shutdown

```
{"results": [ ...one record per device... ], "ok": 2, "skipped": 1, "failed": 0}
```

`ok`, `skipped` and `failed` count the records by `result`. The exit
code is 0 only when every record is `ok`.

### Result record

| Field | Type | Meaning |
| --- | --- | --- |
| `id` | string | adb serial or iOS UDID |
| `name` | string | the `devdrop.toml` alias, else the device's own name |
| `platform` | string | `android` or `ios` |
| `kind` | string | `physical`, `emulator` or `simulator` |
| `manufacturer` | string | maker, such as `Google` or `Apple`; empty when unknown |
| `model` | string | model name; empty when unknown |
| `os_version` | string | Android or iOS version |
| `api` | number or null | Android API level; null on iOS |
| `page_size` | number or null | Android memory page size in bytes, where the device reports it |
| `result` | string | `ok`, `skipped` or `failed` |
| `stage` | string | the step the result is about: `preflight`, `build-apks`, `install`, `uninstall`, `launch`, `running`, `boot`, `shutdown` or `tool` |
| `reason` | string | why the device was skipped or failed; empty for most `ok` results |
| `warnings` | list of strings | things that did not change the result |
| `seconds` | number | time spent on this device |

### Result record: fields present only in some records

| Field | Type | Present when |
| --- | --- | --- |
| `artifact` | string | `install`: the path of the build this record is for |
| `dry_run` | boolean | `install --dry-run`: true on devices that would take the build |
| `running` | boolean or null | `--logs`: whether the app was still running after the watch; null when it could not be told |
| `log` | string or null | `--logs`: the saved log file; null where no log is saved (a physical iPhone without pymobiledevice3) |
| `screenshot` | string | `--screenshot`: the saved PNG, when one was taken |

## devices

```
{"devices": [ ...one entry per device... ], "backends": {"android": {...}, "ios": {...}}}
```

### Device

| Field | Type | Meaning |
| --- | --- | --- |
| `id` | string | adb serial or iOS UDID |
| `platform` | string | `android` or `ios` |
| `kind` | string | `physical`, `emulator` or `simulator` |
| `state` | string | `ready`, or what stands in the way; only `ready` devices take installs |
| `model` | string | model name |
| `manufacturer` | string | maker |
| `os_version` | string | Android or iOS version |
| `api` | number or null | Android API level |
| `abis` | list of strings | Android CPU ABIs, preferred first |
| `page_size` | number or null | Android memory page size in bytes |
| `serial_number` | string | Android hardware serial: the same over USB and Wi-Fi |
| `transport` | string | `usb`, `wifi`, `local` or empty |
| `name` | string | the `devdrop.toml` alias, else the device's own name |

### Backend

| Field | Type | Meaning |
| --- | --- | --- |
| `tool` | string or null | path of `adb` or `xcrun`; null when not found |
| `error` | string or null | why the tool could not list devices |

## inspect

### Android build

| Field | Type | Meaning |
| --- | --- | --- |
| `type` | string | `apk` or `aab` |
| `platform` | string | `android` |
| `path` | string | the file |
| `id` | string | package name |
| `version` | string | `versionName` |
| `build` | string | `versionCode` |
| `min_sdk` | number or string | `minSdkVersion`; a codename string for a preview build |
| `target_sdk` | number or string | `targetSdkVersion` |
| `abis` | list of strings | native ABIs; empty when there is no native code |
| `debuggable` | boolean | the manifest's `debuggable` |
| `launcher` | string | launcher activity, fully qualified; empty when none |
| `split` | string | split name; empty for a base APK |
| `signing` | list of strings or null | `v1`, `v2+`; empty when unsigned; null for an `.aab` |
| `certs` | list of strings | SHA-256 of each signing certificate (v2 or later signature) |
| `cert_ids` | list of strings | the same certificates as `dumpsys package` prints them |
| `size` | number | file size in bytes |
| `page_16k` | object | 16 KB page-size readiness, below |
| `page_size_compat` | string | the manifest's `pageSizeCompat`; empty when not set |

### 16 KB readiness

| Field | Type | Meaning |
| --- | --- | --- |
| `native_64bit` | boolean | whether the build has 64-bit native libraries |
| `ready` | boolean | whether every one of them is 16 KB ready |
| `libraries` | list of objects | one entry per 64-bit library, below |

### Native library

| Field | Type | Meaning |
| --- | --- | --- |
| `path` | string | the library inside the build |
| `elf_align` | number or null | smallest LOAD segment alignment in bytes; null when unreadable |
| `stored` | boolean or null | stored uncompressed; null in an `.aab` |
| `zip_aligned_16k` | boolean or null | starts on a 16 KB boundary; null when compressed or in an `.aab` |
| `ready` | boolean | 16 KB ready |

### iOS build

| Field | Type | Meaning |
| --- | --- | --- |
| `type` | string | `ipa` or `app` |
| `platform` | string | `ios` |
| `path` | string | the file or folder |
| `id` | string | bundle id |
| `version` | string | `CFBundleShortVersionString` |
| `build` | string | `CFBundleVersion` |
| `min_os` | string | minimum iOS version |
| `families` | list of strings | `iphone`, `ipad` |
| `target` | string | `device` or `simulator` |
| `executable` | string | the app's executable name |
| `profile` | object or null | embedded provisioning profile, below; null when there is none |

### Provisioning profile

| Field | Type | Meaning |
| --- | --- | --- |
| `name` | string | profile name |
| `kind` | string | `development`, `ad-hoc`, `enterprise` or `app-store` |
| `team` | string | team id |
| `expires` | string | expiry, ISO 8601 |
| `expired` | boolean | whether it has expired |
| `all_devices` | boolean | whether it allows every device |
| `devices` | list of strings | the UDIDs it allows |

## From a script or a coding agent

Three calls check a build, deliver it and confirm it runs. Each exits 0
only when every selected device is `ok`, and each prints one JSON
document:

```
devdrop doctor                                     # exit 0: tools and devices are ready
devdrop install app.apk --dry-run --json           # which devices would take the build, and why not
devdrop install app.apk --launch --logs logs --screenshot shots --json
```

After the last call, for each record: `result` and `reason` say what
happened, `running` whether the app survived its first seconds, `log`
and `screenshot` where the evidence is. An agent that changed the app
can read the log and look at the PNG to confirm its change on every
device at once. To go on from there and drive the app's interface, use
a UI automation tool; devdrop stops at install, launch and evidence.
