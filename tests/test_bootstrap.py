from __future__ import annotations

import unittest

import context_hub


class BootstrapTest(unittest.TestCase):
    def test_package_version(self) -> None:
        self.assertEqual(context_hub.__version__, "0.1.0")


if __name__ == "__main__":
    unittest.main()

