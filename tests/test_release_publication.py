import json
import subprocess
import unittest
from unittest.mock import MagicMock, patch

from admin import release_dataset as release


class ReleasePublicationTest(unittest.TestCase):
    def run_mode(self, *args):
        with (
            patch("sys.argv", ["release_dataset.py", *args]),
            patch.object(release, "next_release_name", return_value="v20"),
            patch.object(release, "ensure_publishable", return_value="abc") as clean,
            patch.object(
                release,
                "build",
                return_value=(
                    release.ROOT / "dist/data.zip",
                    release.ROOT / "dist/notes.md",
                ),
            ) as build,
            patch.object(release, "chart_module") as charts,
            patch.object(release, "verify_uploaded_release") as verify,
            patch.object(release, "publish_verified_draft") as publish,
            patch.object(release.subprocess, "run") as run,
        ):
            release.main()
        return clean, build, charts, verify, publish, run

    def test_default_verifies_then_publishes(self):
        clean, build, charts, verify, publish, run = self.run_mode()
        clean.assert_called_once()
        build.assert_called_once()
        charts.assert_called_once()
        self.assertIn("--draft", run.call_args.args[0])
        publish.assert_called_once_with("v20", "abc")
        verify.assert_not_called()

    def test_draft_stays_private_without_charts(self):
        _, _, charts, verify, publish, _ = self.run_mode("--draft")
        verify.assert_called_once_with("v20", "abc")
        publish.assert_not_called()
        charts.assert_not_called()

    def test_build_only_does_not_upload_or_require_clean_checkout(self):
        clean, build, charts, verify, publish, run = self.run_mode("--build-only")
        build.assert_called_once()
        for mock in (clean, charts, verify, publish, run):
            mock.assert_not_called()

    def test_publish_draft_does_not_build(self):
        with patch.object(
            release, "latest_draft", return_value=("v19", "abc")
        ) as latest:
            _, build, _, _, publish, run = self.run_mode("--publish-draft")
        latest.assert_called_once_with(None)
        publish.assert_called_once_with("v19", "abc")
        build.assert_not_called()
        run.assert_not_called()

    def test_charts_only_does_not_publish(self):
        _, build, charts, verify, publish, run = self.run_mode("--charts-only")
        charts.return_value.regenerate.assert_called_once()
        for mock in (build, verify, publish, run):
            mock.assert_not_called()

    def test_verification_blocks_publication(self):
        with (
            patch.object(
                release,
                "verify_uploaded_release",
                side_effect=RuntimeError("digest mismatch"),
            ),
            patch.object(release.subprocess, "run") as run,
            patch.object(release, "chart_module") as charts,
        ):
            with self.assertRaisesRegex(RuntimeError, "digest mismatch"):
                release.publish_verified_draft("v20", "abc")
        run.assert_not_called()
        charts.assert_not_called()

    def test_publish_then_regenerate_and_explain_chart_failure(self):
        order = MagicMock()
        with (
            patch.object(
                release,
                "verify_uploaded_release",
                side_effect=lambda *a: order.verify(),
            ),
            patch.object(
                release.subprocess, "run", side_effect=lambda *a, **k: order.publish()
            ),
            patch.object(
                release.subprocess,
                "check_output",
                return_value='{"isDraft":false,"isPrerelease":false}',
            ),
            patch.object(release, "chart_module") as charts,
        ):
            charts.return_value.regenerate.side_effect = lambda: order.charts()
            release.publish_verified_draft("v20", "abc")
            self.assertEqual(
                [c[0] for c in order.mock_calls], ["verify", "publish", "charts"]
            )
            charts.return_value.regenerate.side_effect = subprocess.CalledProcessError(
                1, "gh"
            )
            with self.assertRaisesRegex(
                RuntimeError, "v20 is published.*--charts-only"
            ):
                release.publish_verified_draft("v20", "abc")

    def test_publication_confirmation_failure_does_not_generate_charts(self):
        with (
            patch.object(release, "verify_uploaded_release"),
            patch.object(release.subprocess, "run"),
            patch.object(
                release.subprocess,
                "check_output",
                side_effect=subprocess.CalledProcessError(1, "gh"),
            ),
            patch.object(release, "chart_module") as charts,
        ):
            with self.assertRaisesRegex(RuntimeError, "may already be public"):
                release.publish_verified_draft("v20", "abc")
        charts.assert_not_called()

    def test_missing_chart_dependency_blocks_publication(self):
        with (
            patch("sys.argv", ["release_dataset.py", "--publish-draft"]),
            patch.object(
                release,
                "chart_module",
                side_effect=RuntimeError("missing plotting dependency"),
            ),
            patch.object(release, "latest_draft") as latest,
            patch.object(release, "publish_verified_draft") as publish,
            patch("sys.stderr"),
        ):
            with self.assertRaises(SystemExit):
                release.main()
        latest.assert_not_called()
        publish.assert_not_called()

    def test_latest_draft_uses_creation_date_and_rejects_published_release(self):
        releases = [
            {"tagName": tag, "isDraft": draft, "isPrerelease": pre, "createdAt": date}
            for tag, draft, pre, date in [
                ("v18", True, False, "1"),
                ("v19", True, False, "2"),
                ("v20", False, False, "3"),
                ("v21", True, True, "4"),
            ]
        ]
        info = {"isDraft": True, "isPrerelease": False, "targetCommitish": "a" * 40}
        with patch.object(
            release.subprocess,
            "check_output",
            side_effect=[json.dumps(releases), json.dumps(info)],
        ):
            self.assertEqual(release.latest_draft(), ("v19", "a" * 40))
        info["isDraft"] = False
        with patch.object(
            release.subprocess, "check_output", return_value=json.dumps(info)
        ):
            with self.assertRaisesRegex(RuntimeError, "not a stable draft"):
                release.latest_draft("v19")


if __name__ == "__main__":
    unittest.main()
