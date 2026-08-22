import subprocess
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class AdminScriptsTest(unittest.TestCase):
    def test_deploy_script_is_valid_and_documents_dry_run(self) -> None:
        script = ROOT / "admin" / "deploy.sh"
        subprocess.run(["bash", "-n", script], check=True)
        output = subprocess.check_output([script, "--help"], text=True)
        self.assertIn("Without --apply", output)
        self.assertIn("requires --allow-dirty", output)
        release_help = subprocess.check_output(
            [
                ROOT / ".venv" / "bin" / "python",
                ROOT / "admin" / "release_dataset.py",
                "--help",
            ],
            text=True,
        )
        self.assertIn("--sync-hosted", release_help)


if __name__ == "__main__":
    unittest.main()
