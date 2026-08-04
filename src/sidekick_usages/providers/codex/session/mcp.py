"""Strict MCP inventory and lifecycle proof for resident Codex threads."""

from collections.abc import Callable
from dataclasses import dataclass
from threading import Condition
from time import monotonic

from sidekick_usages.core.selection.types import SelectionCode
from sidekick_usages.providers.codex.app_server.methods import (
    MCP_SERVER_STATUS_LIST_METHOD,
)
from sidekick_usages.providers.codex.session.config import CodexSessionReader
from sidekick_usages.providers.codex.session.errors import CodexRelayError
from sidekick_usages.providers.codex.session.models import (
    CodexLoadedThreadSnapshot,
)

MAX_CODEX_RELAY_MCP_SERVERS = 256
_MCP_PROOF_TIMEOUT_SECONDS = 40.0
_MCP_TERMINAL_STATES = frozenset({"cancelled", "failed", "ready"})
type CodexMcpNames = dict[str, frozenset[str]]


@dataclass(slots=True)
class CodexMcpRefreshProof:
    """Retain exact precommit MCP state until postcommit readback."""

    refresh_required: bool
    armed_revision: int
    names: CodexMcpNames
    baseline_revisions: dict[tuple[str, str], int] | None = None
    confirmed_names: CodexMcpNames | None = None


class CodexMcpLifecycle:
    """Own notification revision watermarks for one participant relay."""

    def __init__(self, condition: Condition) -> None:
        self._condition = condition
        self._revision = 0
        self._statuses: dict[tuple[str, str], tuple[str, int]] = {}

    @property
    def revision(self) -> int:
        """Return the latest observed lifecycle notification revision."""
        return self._revision

    def observe(self, thread_id: str, name: str, status: str) -> None:
        """Record one exact thread-scoped lifecycle notification."""
        self._revision += 1
        key = thread_id, name
        self._statuses[key] = status, self._revision
        self._condition.notify_all()

    def retain_terminal(self, proof: CodexMcpRefreshProof) -> bool:
        """Retain authoritative terminal revisions already seen live."""
        if not self._terminal(proof.names):
            return False
        proof.baseline_revisions = {
            (thread_id, name): self._statuses[(thread_id, name)][1]
            for thread_id, thread_names in proof.names.items()
            for name in thread_names
        }
        return True

    def await_baseline(
        self,
        proof: CodexMcpRefreshProof,
        unusable: Callable[[], None],
    ) -> bool:
        """Await newer terminal lifecycle for every armed pair."""
        deadline = monotonic() + _MCP_PROOF_TIMEOUT_SECONDS
        with self._condition:
            while not self._refresh_terminal_after(
                proof.names,
                proof.armed_revision,
            ):
                unusable()
                remaining = deadline - monotonic()
                if remaining <= 0:
                    return False
                self._condition.wait(remaining)
            return True

    def confirm_baseline(self, proof: CodexMcpRefreshProof) -> bool:
        """Retain exact per-pair revisions after terminal confirmation."""
        if not self._refresh_terminal_after(
            proof.names,
            proof.armed_revision,
        ):
            return False
        proof.baseline_revisions = {
            (thread_id, name): self._statuses[(thread_id, name)][1]
            for thread_id, thread_names in proof.names.items()
            for name in thread_names
        }
        return True

    def await_ready(
        self,
        proof: CodexMcpRefreshProof,
        unusable: Callable[[], None],
    ) -> bool:
        """Await still-newer ready lifecycle for every armed pair."""
        deadline = monotonic() + _MCP_PROOF_TIMEOUT_SECONDS
        with self._condition:
            while not self.ready(proof):
                unusable()
                remaining = deadline - monotonic()
                if remaining <= 0:
                    return False
                self._condition.wait(remaining)
            return True

    def ready(self, proof: CodexMcpRefreshProof) -> bool:
        """Return whether every pair is ready after its baseline."""
        baselines = proof.baseline_revisions
        if baselines is None:
            return False
        for thread_id, thread_names in proof.names.items():
            for name in thread_names:
                status, revision = self._statuses.get(
                    (thread_id, name),
                    ("", 0),
                )
                baseline = baselines.get((thread_id, name))
                if (
                    baseline is None
                    or status != "ready"
                    or revision <= baseline
                ):
                    return False
        return True

    def proven(
        self,
        proof: CodexMcpRefreshProof,
        names: CodexMcpNames,
    ) -> bool:
        """Return exact retained proof for readiness publication."""
        if names != proof.names:
            return False
        if proof.refresh_required:
            return self.ready(proof)
        return self._retained_terminal(proof)

    def _terminal(self, names: CodexMcpNames) -> bool:
        return all(
            self._statuses.get((thread_id, name), ("", 0))[0]
            in _MCP_TERMINAL_STATES
            for thread_id, thread_names in names.items()
            for name in thread_names
        )

    def _refresh_terminal_after(
        self,
        names: CodexMcpNames,
        revision_watermark: int,
    ) -> bool:
        return all(
            status in _MCP_TERMINAL_STATES and revision > revision_watermark
            for thread_id, thread_names in names.items()
            for name in thread_names
            for key in ((thread_id, name),)
            for status, revision in (self._statuses.get(key, ("", 0)),)
        )

    def _retained_terminal(self, proof: CodexMcpRefreshProof) -> bool:
        revisions = proof.baseline_revisions
        if revisions is None:
            return False
        return all(
            status in _MCP_TERMINAL_STATES and revisions.get(key) == revision
            for thread_id, thread_names in proof.names.items()
            for name in thread_names
            for key in ((thread_id, name),)
            for status, revision in (self._statuses.get(key, ("", 0)),)
        )


def read_codex_mcp_names(
    reader: CodexSessionReader,
    snapshot: CodexLoadedThreadSnapshot,
) -> CodexMcpNames:
    """Read exact configured MCP names for every loaded thread."""
    names_by_thread: CodexMcpNames = {}
    for thread_id in snapshot.thread_ids:
        result = reader.request(
            MCP_SERVER_STATUS_LIST_METHOD,
            {"threadId": thread_id},
        )
        statuses = result.get("data")
        next_cursor = result.get("nextCursor")
        if (
            set(result) not in ({"data"}, {"data", "nextCursor"})
            or not isinstance(statuses, list)
            or next_cursor is not None
            or len(statuses) > MAX_CODEX_RELAY_MCP_SERVERS
        ):
            raise CodexRelayError(SelectionCode.AUTHORITY_PROOF_FAILED)
        names: set[str] = set()
        for status in statuses:
            name = status.get("name") if isinstance(status, dict) else None
            if not isinstance(name, str) or not name or name in names:
                raise CodexRelayError(SelectionCode.AUTHORITY_PROOF_FAILED)
            names.add(name)
        names_by_thread[thread_id] = frozenset(names)
    return names_by_thread
