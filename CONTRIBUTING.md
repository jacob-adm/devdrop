# Contributing

## Tests

```sh
python3 -m unittest discover -s tests                     # unit, CLI and fuzz tests (no devices needed)
ci/linux.sh                                               # the same suite on Linux in Docker
DEVDROP_E2E=1 python3 -m unittest tests.e2e.test_e2e -v   # end to end on emulators and simulators
```

The unit suite runs devdrop against stand-in `adb`, `xcrun` and `emulator` programs, so it needs no
SDK and no devices. The end-to-end suites boot real emulators and simulators, or use an attached
iPhone; see [Testing](docs/testing.md) for what each needs.

## Contributing

Changes reach `main` only through pull requests.

1. Fork the repository and create a branch.
2. Make the change with a test that fails without it, then run `python3 -m unittest discover -s tests`.
3. Open a pull request against `main`. CI runs the tests on Linux, macOS and Windows with Python
   3.11 and 3.13, and builds the package; the required checks must pass before merging. Pull requests
   are merged with a merge commit.

Keep devdrop standard-library-only, and keep each platform working without the other's tools.
