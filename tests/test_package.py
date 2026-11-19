# SPDX-License-Identifier: Apache-2.0
"""Package metadata: the license in the form current build backends support."""

import os
import tomllib
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class License(unittest.TestCase):
    def setUp(self):
        with open(os.path.join(ROOT, "pyproject.toml"), "rb") as f:
            self.pyproject = tomllib.load(f)

    def test_license_is_an_spdx_expression(self):
        # The table form ({text = ...}) is deprecated by setuptools and stops building in 2027.
        self.assertEqual(self.pyproject["project"]["license"], "Apache-2.0")
        self.assertEqual(self.pyproject["project"]["license-files"], ["LICENSE"])

    def test_backend_supports_spdx_license(self):
        # setuptools reads license-files and SPDX strings from 77 on.
        self.assertIn("setuptools>=77", self.pyproject["build-system"]["requires"])


if __name__ == "__main__":
    unittest.main()
