from __future__ import annotations

import unittest

import bridge_config
from bridge_config import ConfigError, load_config
from tests.support import BASE_ENV, configured, render_env


class ConfigTests(unittest.TestCase):
    def test_quoted_names_and_levels_are_read(self) -> None:
        with configured(LOCAL_OUTPUT_NAME='"Office Display"'):
            config = load_config()
        self.assertEqual(config.local_output_name, "Office Display")
        self.assertEqual(config.local_volume, 71)
        self.assertEqual(config.wiim_volume, 39)

    def test_invalid_volume_is_rejected(self) -> None:
        with (
            configured(LOCAL_VOLUME="101"),
            self.assertRaisesRegex(
                ConfigError, "LOCAL_VOLUME must be between 0 and 100"
            ),
        ):
            load_config()

    def test_offset_bounds_are_enforced(self) -> None:
        with (
            configured(WIIM_OFFSET_MS="-2001"),
            self.assertRaisesRegex(
                ConfigError, "WIIM_OFFSET_MS must be between -2000 and 2000"
            ),
        ):
            load_config()

    def test_duplicate_key_is_rejected(self) -> None:
        text = render_env(BASE_ENV) + "LOCAL_VOLUME=91\n"
        with (
            configured(text),
            self.assertRaisesRegex(ConfigError, "duplicate.*LOCAL_VOLUME"),
        ):
            load_config()

    def test_unknown_key_is_rejected_with_a_suggestion(self) -> None:
        text = render_env(BASE_ENV) + "LOCAL_VOLME=90\n"
        with (
            configured(text),
            self.assertRaisesRegex(
                ConfigError, "unknown.*LOCAL_VOLME.*did you mean LOCAL_VOLUME"
            ),
        ):
            load_config()

    def test_compose_only_keys_are_ignored(self) -> None:
        # Docker Compose reads these from the same file; rejecting them would
        # break every bridge tool for a setting that is not even ours.
        text = render_env(BASE_ENV) + "COMPOSE_PROJECT_NAME=myhouse\n"
        with configured(text):
            self.assertEqual(load_config().local_volume, 71)

    def test_inline_comments_are_stripped_like_compose_does(self) -> None:
        text = render_env(BASE_ENV).replace(
            "START_BUFFER_MS=1750\n", "START_BUFFER_MS=1750  # tested value\n"
        )
        with configured(text):
            self.assertEqual(load_config().start_buffer_ms, 1750)

    def test_hash_without_leading_space_stays_in_the_value(self) -> None:
        with configured(LOCAL_OUTPUT_NAME="Desk#2"):
            self.assertEqual(load_config().local_output_name, "Desk#2")

    def test_dollar_is_rejected_because_compose_would_expand_it(self) -> None:
        with (
            configured(SOLOIST_KEY_FILE="/home/tester/$USER/key"),
            self.assertRaisesRegex(ConfigError, r"must not contain '\$'"),
        ):
            load_config()

    def test_runtime_paths_must_be_absolute(self) -> None:
        with (
            configured(SOLOIST_KEY_FILE="relative/key"),
            self.assertRaisesRegex(ConfigError, "absolute path"),
        ):
            load_config()

    def test_key_file_inside_the_repository_is_rejected(self) -> None:
        inside = str(bridge_config.PROJECT / "soloist_api_key")
        with (
            configured(SOLOIST_KEY_FILE=inside),
            self.assertRaisesRegex(ConfigError, "must live outside the repository"),
        ):
            load_config()

    def test_key_file_nested_inside_the_repository_is_rejected(self) -> None:
        inside = str(bridge_config.PROJECT / ".secrets" / "soloist_api_key")
        with (
            configured(SOLOIST_KEY_FILE=inside),
            self.assertRaisesRegex(ConfigError, "must live outside the repository"),
        ):
            load_config()


class TrustedNetworkTests(unittest.TestCase):
    def test_devices_must_be_inside_the_trusted_network(self) -> None:
        with (
            configured(LIVING_ROOM_IP="198.51.100.11"),
            self.assertRaisesRegex(ConfigError, "must be inside TRUSTED_NETWORK"),
        ):
            load_config()

    def test_host_bits_are_rejected_rather_than_silently_masked(self) -> None:
        # strict=False would rewrite this to 0.0.0.0/0 and make the containment
        # check accept every address on the internet.
        with (
            configured(TRUSTED_NETWORK="192.0.2.10/0"),
            self.assertRaisesRegex(ConfigError, "no host bits set"),
        ):
            load_config()

    def test_a_zero_prefix_cannot_be_used(self) -> None:
        with (
            configured(
                TRUSTED_NETWORK="0.0.0.0/0",
                KITCHEN_IP="8.8.8.8",
                LIVING_ROOM_IP="1.1.1.1",
            ),
            self.assertRaisesRegex(ConfigError, "/8, /16 or /24"),
        ):
            load_config()

    def test_public_networks_are_rejected(self) -> None:
        with (
            configured(
                TRUSTED_NETWORK="8.8.8.0/24",
                KITCHEN_IP="8.8.8.10",
                LIVING_ROOM_IP="8.8.8.11",
            ),
            self.assertRaisesRegex(ConfigError, "must be a private network"),
        ):
            load_config()

    def test_non_octet_prefixes_are_rejected(self) -> None:
        # OwnTone compares address text, so a /25 cannot be expressed exactly.
        with (
            configured(TRUSTED_NETWORK="192.0.2.0/25"),
            self.assertRaisesRegex(ConfigError, "/8, /16 or /24"),
        ):
            load_config()

    def test_prefix_rendered_for_owntone_is_octet_text_not_cidr(self) -> None:
        for network, expected in (
            ("192.0.2.0/24", "192.0.2"),
            ("192.168.0.0/16", "192.168"),
            ("10.0.0.0/8", "10"),
        ):
            with (
                self.subTest(network=network),
                configured(
                    TRUSTED_NETWORK=network,
                    KITCHEN_IP=str(
                        {
                            "192.0.2.0/24": "192.0.2.10",
                            "192.168.0.0/16": "192.168.4.10",
                            "10.0.0.0/8": "10.4.4.10",
                        }[network]
                    ),
                    LIVING_ROOM_IP=str(
                        {
                            "192.0.2.0/24": "192.0.2.11",
                            "192.168.0.0/16": "192.168.4.11",
                            "10.0.0.0/8": "10.4.4.11",
                        }[network]
                    ),
                ),
            ):
                self.assertEqual(load_config().trusted_network_prefix, expected)


class FollowerTests(unittest.TestCase):
    def test_a_single_follower_comes_from_the_living_room_keys(self) -> None:
        with configured():
            self.assertEqual(load_config().followers, (("192.0.2.11", "Living Room"),))

    def test_several_followers_can_be_configured(self) -> None:
        with configured(WIIM_FOLLOWERS="192.0.2.11=Living Room,192.0.2.12=Patio"):
            self.assertEqual(
                load_config().followers,
                (("192.0.2.11", "Living Room"), ("192.0.2.12", "Patio")),
            )

    def test_a_follower_outside_the_trusted_network_is_rejected(self) -> None:
        with (
            configured(WIIM_FOLLOWERS="192.0.2.11=Living Room,203.0.113.9=Patio"),
            self.assertRaisesRegex(ConfigError, "must be inside TRUSTED_NETWORK"),
        ):
            load_config()

    def test_a_follower_that_repeats_the_leader_is_rejected(self) -> None:
        with (
            configured(WIIM_FOLLOWERS="192.0.2.10=Kitchen"),
            self.assertRaisesRegex(ConfigError, "duplicate WiiM address"),
        ):
            load_config()

    def test_malformed_follower_entries_are_rejected(self) -> None:
        with (
            configured(WIIM_FOLLOWERS="192.0.2.11"),
            self.assertRaisesRegex(ConfigError, "ADDRESS=Name"),
        ):
            load_config()


class LazyLoadingTests(unittest.TestCase):
    def test_importing_the_module_does_not_read_any_env_file(self) -> None:
        # doctor.py exists to diagnose a broken install, and the test suite has
        # to import cleanly on a fresh clone that has no .env at all.
        self.assertIsInstance(bridge_config.CONFIG, bridge_config._LazyConfig)


if __name__ == "__main__":
    unittest.main()
