# SPDX-License-Identifier: Apache-2.0
"""`python -m devdrop`: run the CLI and exit with its status code."""

import sys

from devdrop.cli import main

sys.exit(main())
