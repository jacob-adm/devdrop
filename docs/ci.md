# Reports for CI

`--json` prints every device's result. `--junit FILE` (install, launch,
uninstall, boot, shutdown) also writes JUnit XML with one testcase per
device: skipped and failed devices carry devdrop's reason, and the
device's details, warnings and log path are in its output section, so
any CI dashboard shows which devices failed and why.

devdrop exits 0 only when every selected device is `ok`; see
[Exit codes](commands.md#exit-codes) for the rest.
