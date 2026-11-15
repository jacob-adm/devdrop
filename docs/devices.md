# Devices

Choosing devices, naming them, Wi-Fi, emulators and simulators, and what to expect from a physical iPhone.

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

## Physical iPhones

- The iPhone needs Developer Mode on (Settings > Privacy & Security) and
  must trust the computer.
- The build must be signed with a development or ad-hoc profile that
  lists the phone's UDID. `devdrop inspect app.ipa` shows the profile's
  type, team, devices and expiry; devdrop skips a phone the profile does
  not list.
- The first launch of an app from a new developer is refused until you
  tap Settings > General > VPN & Device Management > the developer >
  Trust. devdrop's reason says so.
- iOS takes that trust back when the developer's last app is removed from
  the phone. Keep one app from the same team installed if you uninstall
  and reinstall often.
- Builds signed with a free Apple account stop working after 7 days.
- A locked iPhone that already trusts the computer still installs and
  launches (seen on iOS 26).
