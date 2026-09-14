from __future__ import annotations

import importlib.util
import subprocess  # nosec B404
import sys
import tempfile
import unittest
from pathlib import Path

from bridge_config import PROJECT

SCANNER = PROJECT / "scripts/check-secrets.py"

# Assembled at runtime so this test file does not itself look like a leak to the
# scanner it exercises.
FAKE_KEY = "spak_" + "AbCdEf0123456789012345678901"


def load_scanner():
    spec = importlib.util.spec_from_file_location("check_secrets", SCANNER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


check_secrets = load_scanner()


class PatternTests(unittest.TestCase):
    def test_every_private_key_header_variant_matches(self) -> None:
        for header in (
            "PRIVATE KEY",
            "RSA PRIVATE KEY",
            "EC PRIVATE KEY",
            "OPENSSH PRIVATE KEY",
            "ENCRYPTED PRIVATE KEY",
            "DSA PRIVATE KEY",
            "PGP PRIVATE KEY BLOCK",
        ):
            with self.subTest(header=header):
                body = f"-----BEGIN {header}-----".encode()
                self.assertTrue(check_secrets.PATTERNS["private key"].search(body))

    def test_a_literal_key_is_found_regardless_of_format(self) -> None:
        # The documented key prefix is an assumption; matching the installation's
        # actual key value does not depend on it.
        secret = b"totally-not-a-spak-prefixed-key-1234"
        labels = check_secrets.scan(b"config: " + secret, [secret])
        self.assertIn("this installation's live API key", labels)

    def test_ordinary_content_is_not_flagged(self) -> None:
        self.assertEqual(check_secrets.scan(b"just some config\n", []), [])

    def test_the_scanner_does_not_match_its_own_source(self) -> None:
        self.assertEqual(check_secrets.scan(SCANNER.read_bytes(), []), [])


class HistoryScanTests(unittest.TestCase):
    """The scan must cover history: publishing a repo publishes every commit."""

    def repository(self, directory: str) -> Path:
        root = Path(directory) / "repo"
        root.mkdir()
        scripts = root / "scripts"
        scripts.mkdir()
        (scripts / "check-secrets.py").write_bytes(SCANNER.read_bytes())
        (root / "README.md").write_text("hello\n", encoding="utf-8")
        self.git(root, "init", "-q")
        self.git(root, "add", "-A")
        self.commit(root, "initial")
        return root

    def git(self, root: Path, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(  # nosec B603
            ["git", *args], cwd=root, check=True, capture_output=True
        )

    def commit(self, root: Path, message: str) -> None:
        self.git(
            root,
            "-c",
            "user.email=test@example.invalid",
            "-c",
            "user.name=Test",
            "commit",
            "-qm",
            message,
        )

    def scan(self, root: Path) -> subprocess.CompletedProcess:
        return subprocess.run(  # nosec B603
            [sys.executable, str(root / "scripts/check-secrets.py")],
            cwd=root,
            check=False,
            capture_output=True,
            text=True,
        )

    def test_a_clean_repository_passes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = self.repository(directory)
            self.assertEqual(self.scan(root).returncode, 0)

    def test_a_key_deleted_from_the_tree_is_still_found_in_history(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = self.repository(directory)
            leaked = root / "leaked.txt"
            leaked.write_text(f"{FAKE_KEY}\n", encoding="utf-8")
            self.git(root, "add", "-A")
            self.commit(root, "add key")
            self.git(root, "rm", "-q", "leaked.txt")
            self.commit(root, "remove key")

            self.assertEqual(self.git(root, "status", "--porcelain").stdout, b"")
            result = self.scan(root)
            self.assertEqual(result.returncode, 1, result.stdout)
            self.assertIn("Git history", result.stderr)

    def test_a_key_in_the_working_tree_is_found(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = self.repository(directory)
            (root / "leaked.txt").write_text(f"{FAKE_KEY}\n", encoding="utf-8")
            result = self.scan(root)
            self.assertEqual(result.returncode, 1)
            self.assertIn("working tree", result.stderr)


if __name__ == "__main__":
    unittest.main()
