"""Refresh comparison snapshots after a quiet period, without delaying readers."""

import logging
import threading
import time

logger = logging.getLogger(__name__)
COMPARISON_REFRESH_DELAY = 15


class ComparisonSnapshot:
    def __init__(self, build, delay=COMPARISON_REFRESH_DELAY):
        self.build = build
        self.delay = delay
        self.value = build()
        self.condition = threading.Condition()
        self.build_lock = threading.Lock()
        self.generation = 0
        self.deadline = None
        self.stopped = False
        self.worker = None

    def schedule(self):
        with self.condition:
            if not self.stopped:
                self.generation += 1
                self.deadline = time.monotonic() + self.delay
                self.condition.notify_all()

    def get(self):
        with self.condition:
            return self.value

    def refresh(self):
        # Readers never take this lock or wait for the builder.
        with self.build_lock:
            with self.condition:
                generation = self.generation
            value = self.build()
            with self.condition:
                # Another approval during the build gets its own quiet period.
                if generation == self.generation and not self.stopped:
                    self.value = value
                    self.deadline = None
                    self.condition.notify_all()

    def start(self):
        self.worker = threading.Thread(
            target=self.run, name="comparison-refresh", daemon=True
        )
        self.worker.start()

    def close(self):
        with self.condition:
            self.stopped = True
            self.condition.notify_all()
        if self.worker:
            self.worker.join(timeout=2)

    def run(self):
        while True:
            with self.condition:
                while not self.stopped:
                    if self.deadline is None:
                        self.condition.wait()
                    else:
                        remaining = self.deadline - time.monotonic()
                        if remaining <= 0:
                            break
                        self.condition.wait(remaining)
                if self.stopped:
                    return
            try:
                self.refresh()
            except Exception:
                logger.exception("Comparison refresh failed; keeping the previous snapshot")
                with self.condition:
                    # Retry without spinning, retaining any newer approval's deadline.
                    if self.deadline is not None and self.deadline <= time.monotonic():
                        self.deadline = time.monotonic() + self.delay
