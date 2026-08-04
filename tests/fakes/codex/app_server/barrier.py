"""One-shot synchronization for synthetic app-server boundaries."""

from threading import Event, Lock


class FakeCodexBarrier:
    """Pause one selected fake-provider boundary until released."""

    def __init__(self, label: str, timeout_seconds: float) -> None:
        self._label = label
        self._timeout_seconds = timeout_seconds
        self._lock = Lock()
        self._armed = False
        self._paused = Event()
        self._resume = Event()
        self._resumed = Event()

    def arm(self) -> None:
        """Arm exactly one future boundary."""
        with self._lock:
            if self._armed:
                raise AssertionError(f"Fake Codex {self._label} is armed.")
            self._armed = True
            self._paused.clear()
            self._resume.clear()
            self._resumed.clear()

    def arrive(self) -> None:
        """Pause at the boundary only when armed."""
        with self._lock:
            if not self._armed:
                return
            self._armed = False
        self._paused.set()
        if not self._resume.wait(self._timeout_seconds):
            raise AssertionError(f"Fake Codex {self._label} did not resume.")
        self._resumed.set()

    def wait(self) -> None:
        """Wait until the armed boundary pauses."""
        if not self._paused.wait(self._timeout_seconds):
            raise AssertionError(f"Fake Codex {self._label} did not pause.")

    def release(self) -> None:
        """Release and observe the paused boundary."""
        self._resume.set()
        if not self._resumed.wait(self._timeout_seconds):
            raise AssertionError(f"Fake Codex {self._label} did not resume.")

    def cancel(self) -> None:
        """Release a boundary during fake shutdown without waiting."""
        self._resume.set()
