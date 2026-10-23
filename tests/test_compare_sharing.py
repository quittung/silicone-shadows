"""Run comparison link codec checks through the normal test command."""
import shutil
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class CompareSharingTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which("node"), "Node.js is required for browser codec checks")
    def test_link_codec(self):
        result = subprocess.run(
            [shutil.which("node"), "tests/test_compare_sharing.js"],
            cwd=ROOT, capture_output=True, text=True, timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
