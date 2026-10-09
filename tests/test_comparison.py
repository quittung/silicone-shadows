import threading
import unittest
from unittest.mock import Mock, patch

from server.comparison import ComparisonSnapshot


class ComparisonSnapshotTest(unittest.TestCase):
    def test_each_change_resets_the_quiet_period(self):
        snapshot = ComparisonSnapshot(lambda: "initial")
        with patch("server.comparison.time.monotonic", side_effect=[100, 110]):
            snapshot.schedule()
            self.assertEqual(snapshot.deadline, 115)
            snapshot.schedule()
            self.assertEqual(snapshot.deadline, 125)
        self.assertEqual(snapshot.get(), "initial")
        snapshot.close()

    def test_background_rebuild_never_blocks_readers_and_batches_changes(self):
        started, release = threading.Event(), threading.Event()

        def build():
            started.set()
            if not release.wait(2):
                raise RuntimeError("test builder timed out")
            return "updated"

        builder = Mock(return_value="initial")
        snapshot = ComparisonSnapshot(builder, delay=0.01)
        builder.side_effect = build
        snapshot.schedule()
        snapshot.schedule()
        snapshot.start()
        try:
            self.assertTrue(started.wait(2))
            self.assertEqual(snapshot.get(), "initial")
            release.set()
            with snapshot.condition:
                self.assertTrue(snapshot.condition.wait_for(
                    lambda: snapshot.deadline is None, timeout=2
                ))
            self.assertEqual(snapshot.get(), "updated")
            self.assertEqual(builder.call_count, 2)  # Startup and one batched rebuild.
        finally:
            release.set()
            snapshot.close()

    def test_change_during_rebuild_discards_the_obsolete_result(self):
        builder = Mock(return_value="initial")
        snapshot = ComparisonSnapshot(builder)

        def build():
            snapshot.schedule()
            return "obsolete"

        builder.side_effect = build
        snapshot.schedule()
        snapshot.refresh()
        self.assertEqual(snapshot.get(), "initial")
        self.assertIsNotNone(snapshot.deadline)
        builder.side_effect = None
        builder.return_value = "latest"
        snapshot.refresh()
        self.assertEqual(snapshot.get(), "latest")
        self.assertIsNone(snapshot.deadline)
        snapshot.close()

    def test_failed_background_build_keeps_snapshot_and_retries(self):
        builder = Mock(side_effect=["initial", ValueError("invalid"), "updated"])
        snapshot = ComparisonSnapshot(builder, delay=0.01)
        snapshot.schedule()
        with patch("server.comparison.logger.exception") as logged:
            snapshot.start()
            try:
                with snapshot.condition:
                    self.assertTrue(snapshot.condition.wait_for(
                        lambda: snapshot.deadline is None, timeout=2
                    ))
                self.assertEqual(snapshot.get(), "updated")
                logged.assert_called_once()
            finally:
                snapshot.close()

    def test_shutdown_cancels_pending_refresh(self):
        builder = Mock(return_value="initial")
        snapshot = ComparisonSnapshot(builder)
        snapshot.schedule()
        snapshot.start()
        snapshot.close()
        snapshot.schedule()
        self.assertFalse(snapshot.worker.is_alive())
        self.assertEqual(builder.call_count, 1)
