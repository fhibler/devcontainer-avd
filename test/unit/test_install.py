"""Exercise the installer's actual package retry helper without root or a network."""

import subprocess
import unittest
from pathlib import Path

INSTALLER = Path(__file__).resolve().parents[2] / "src/avd/install.sh"


def package_helper():
    # Load just the shipped helper; the rest of install.sh mutates the host.
    source = INSTALLER.read_text()
    start = source.index("install_rhel_packages() {\n")
    end = source.index("\n}\n", start) + 3
    return "set -euo pipefail\n" + source[start:end]


class PackageDownloads(unittest.TestCase):
    def install(self, manager, failures):
        harness = (
            package_helper()
            + r"""
attempts=0
manager=$1
failures=$2
mock_manager() {
    printf 'manager: %s\n' "$*"
    if [ "$1" = clean ]; then return 1; fi
    attempts=$((attempts + 1))
    if [ "$attempts" -le "$failures" ]; then return 42; fi
}
dnf() { mock_manager "$@"; }
microdnf() { mock_manager "$@"; }
sleep() { printf 'sleep: %s\n' "$1"; }
install_rhel_packages "$manager" ca-certificates 'package with spaces'
"""
        )
        return subprocess.run(["bash", "-c", harness, "test", manager, str(failures)], text=True, capture_output=True)

    def test_successful_downloads_are_not_retried(self):
        for manager in ("dnf", "microdnf"):
            with self.subTest(manager=manager):
                result = self.install(manager, 0)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout.count(" install -y "), 1)
                self.assertNotIn("manager: clean", result.stdout)
                self.assertNotIn("sleep:", result.stdout)
                scope = "*." if manager == "microdnf" else ""
                self.assertIn(f"--setopt={scope}retries=1 --setopt={scope}timeout=30", result.stdout)

    def test_failed_downloads_refresh_metadata_and_recover(self):
        for manager in ("dnf", "microdnf"):
            with self.subTest(manager=manager):
                result = self.install(manager, 2)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout.count(" install -y ca-certificates package with spaces"), 3)
                cleanup = "clean all" if manager == "microdnf" else "clean metadata"
                self.assertEqual(result.stdout.count(cleanup), 2)
                self.assertIn("sleep: 2\n", result.stdout)
                self.assertIn("sleep: 4\n", result.stdout)
                self.assertIn(f"{manager} package install failed", result.stderr)

    def test_empty_appstream_metadata_is_discarded_before_retry(self):
        harness = (
            package_helper()
            + r"""
metadata=empty
dnf() {
    if [ "$1" = clean ]; then
        printf 'clean %s\n' "$2"
        if [ "$2" = metadata ]; then metadata=valid; fi
        return 0
    fi
    printf 'install\n'
    if [ "$metadata" = empty ]; then
        printf '%s\n' "Error: Failed to download metadata for repo 'appstream': repomd.xml parser error: Parse error at line: 1 (Document is empty)" >&2
        return 1
    fi
}
sleep() { :; }
install_rhel_packages dnf ca-certificates
"""
        )
        result = subprocess.run(["bash", "-c", harness], text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.splitlines(), ["install", "clean metadata", "install"])
        self.assertIn("repomd.xml parser error", result.stderr)
        self.assertIn("Document is empty", result.stderr)
        self.assertIn("attempt 1/3", result.stderr)

    def test_persistent_failures_preserve_the_exit_status(self):
        for manager in ("dnf", "microdnf"):
            with self.subTest(manager=manager):
                result = self.install(manager, 3)
                self.assertEqual(result.returncode, 42, result.stderr)
                self.assertEqual(result.stdout.count(" install -y "), 3)
                self.assertEqual(result.stdout.count("sleep:"), 2)


if __name__ == "__main__":
    unittest.main()
