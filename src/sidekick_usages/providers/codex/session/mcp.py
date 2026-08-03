"""Strict MCP server-name readback for resident Codex threads."""

from dataclasses import dataclass

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
type CodexMcpNames = dict[str, frozenset[str]]


@dataclass(slots=True)
class CodexMcpRefreshProof:
    """Retain exact precommit MCP state until postcommit readback."""

    refresh_required: bool
    armed_revision: int
    baseline_revisions: dict[tuple[str, str], int]
    names: CodexMcpNames
    confirmed_names: CodexMcpNames | None = None


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
