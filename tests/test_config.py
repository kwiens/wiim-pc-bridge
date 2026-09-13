from __future__ import annotations

import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import bridge_config


class ConfigTests(unittest.TestCase):
    def test_env_file_supports_quoted_names_and_desired_levels(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / ".env"
            path.write_text(
                'LOCAL_OUTPUT_NAME="Office Display"\n'
                "LOCAL_VOLUME=91\n"
                "WIIM_VOLUME=43\n",
                encoding="utf-8",
            )
            with mock.patch.dict(
                os.environ, {"WIIM_BRIDGE_ENV": str(path)}, clear=False
            ):
                config = bridge_config.load_config()
        self.assertEqual(config.local_output_name, "Office Display")
        self.assertEqual(config.local_volume, 91)
        self.assertEqual(config.wiim_volume, 43)

    def test_invalid_volume_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / ".env"
            path.write_text("LOCAL_VOLUME=101\n", encoding="utf-8")
            with mock.patch.dict(
                os.environ, {"WIIM_BRIDGE_ENV": str(path)}, clear=False
            ):
                with self.assertRaisesRegex(ValueError, "LOCAL_VOLUME"):
                    bridge_config.load_config()


if __name__ == "__main__":
    unittest.main()
