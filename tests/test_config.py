from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import bridge_config


class ConfigTests(unittest.TestCase):
    def test_env_file_supports_quoted_names_and_desired_levels(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / ".env"
            path.write_text(
                'LOCAL_OUTPUT_NAME="Office Display"\n'
                "DISPLAYPORT_SINK=alsa_output.example\n"
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
            with (
                mock.patch.dict(
                    os.environ, {"WIIM_BRIDGE_ENV": str(path)}, clear=False
                ),
                self.assertRaisesRegex(ValueError, "LOCAL_VOLUME"),
            ):
                bridge_config.load_config()

    def test_duplicate_key_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / ".env"
            path.write_text("LOCAL_VOLUME=90\nLOCAL_VOLUME=91\n", encoding="utf-8")
            with (
                mock.patch.dict(
                    os.environ, {"WIIM_BRIDGE_ENV": str(path)}, clear=False
                ),
                self.assertRaisesRegex(ValueError, "duplicate.*LOCAL_VOLUME"),
            ):
                bridge_config.load_config()

    def test_unknown_key_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / ".env"
            path.write_text("LOCAL_VOLME=90\n", encoding="utf-8")
            with (
                mock.patch.dict(
                    os.environ, {"WIIM_BRIDGE_ENV": str(path)}, clear=False
                ),
                self.assertRaisesRegex(ValueError, "unknown.*LOCAL_VOLME"),
            ):
                bridge_config.load_config()

    def test_runtime_paths_must_be_absolute(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / ".env"
            path.write_text("SOLOIST_KEY_FILE=relative/key\n", encoding="utf-8")
            with (
                mock.patch.dict(
                    os.environ, {"WIIM_BRIDGE_ENV": str(path)}, clear=False
                ),
                self.assertRaisesRegex(ValueError, "absolute path"),
            ):
                bridge_config.load_config()

    def test_wiim_addresses_must_be_in_trusted_network(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / ".env"
            path.write_text(
                "DISPLAYPORT_SINK=alsa_output.example\n"
                "KITCHEN_IP=192.0.2.10\n"
                "LIVING_ROOM_IP=198.51.100.11\n"
                "TRUSTED_NETWORK=192.0.2.0/24\n",
                encoding="utf-8",
            )
            with (
                mock.patch.dict(
                    os.environ, {"WIIM_BRIDGE_ENV": str(path)}, clear=False
                ),
                self.assertRaisesRegex(ValueError, "inside TRUSTED_NETWORK"),
            ):
                bridge_config.load_config()


if __name__ == "__main__":
    unittest.main()
