"""Complete CLI ownership of one coordinated stock Codex TUI."""

from pathlib import Path
from typing import Protocol

from sidekick_usages.cli.session.launcher import ProviderSessionLauncher


class CodexSessionRuntime(Protocol):
    """Own one qualified relay and registered participant lifetime."""

    @property
    def socket_path(self) -> Path:
        """Return the stable owner-only stock-TUI relay endpoint."""

    def open(self) -> None:
        """Qualify, start, register, and subscribe one participant."""

    def close(self) -> None:
        """Close only resources owned by this participant session."""


class CodexCliSession:
    """Launch one stock Codex TUI through one retained participant relay."""

    def __init__(
        self,
        launcher: ProviderSessionLauncher,
        runtime: CodexSessionRuntime,
        *,
        codex_home: Path,
    ) -> None:
        if not codex_home.is_absolute():
            raise ValueError("The neutral Codex home must be absolute.")
        self._launcher = launcher
        self._runtime = runtime
        self._codex_home = codex_home

    def run(self, arguments: tuple[str, ...]) -> int:
        """Run exactly one stock TUI and return its natural exit status."""
        spec = self._launcher.plan_codex_remote(
            arguments,
            socket_path=self._runtime.socket_path,
            codex_home=self._codex_home,
        )
        child = self._launcher.prepare_child(spec)
        try:
            self._runtime.open()
        except BaseException:
            child.cancel()
            raise
        result = 0
        failure: BaseException | None = None
        try:
            result = child.run()
        except BaseException as error:
            failure = error
        try:
            self._runtime.close()
        except BaseException as cleanup_error:
            if failure is None:
                raise
            raise BaseExceptionGroup(
                "Codex session and cleanup both failed.",
                [failure, cleanup_error],
            ) from None
        if failure is not None:
            raise failure
        return result
