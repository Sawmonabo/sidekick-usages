"""Authoritative activity state for one structured Claude stream."""

from sidekick_usages.providers.claude.structured.models import (
    ClaudeStructuredActivityKind,
    ClaudeStructuredActivityState,
    ClaudeStructuredConversationId,
    ClaudeStructuredStreamEvent,
)

_IGNORED_TASK_TYPES = frozenset({"monitor_mcp", "monitor_ws"})


class ClaudeStructuredActivityObserver:
    """Combine exact session and task state into safe idle proof."""

    def __init__(self) -> None:
        self._tasks: dict[str, ClaudeStructuredActivityKind] = {}
        self._ignored_tasks: set[str] = set()
        self._session_idle = False
        self._background_observed = False
        self._degraded = False

    def session_state(self, state: str) -> bool:
        """Replace the provider session state and return current idle proof."""
        self._session_idle = state == "idle"
        return self._idle_proof()

    def replace_background_tasks(
        self,
        values: tuple[tuple[str, str], ...],
        conversation_id: ClaudeStructuredConversationId | None,
    ) -> tuple[tuple[ClaudeStructuredStreamEvent, ...], bool]:
        """Replace the complete live background set and return idle proof."""
        replacement: dict[str, ClaudeStructuredActivityKind] = {}
        ignored: set[str] = set()
        for task_id, task_type in values:
            if task_id in replacement or task_id in ignored:
                raise ValueError("Duplicate Claude background task ID.")
            if task_type in _IGNORED_TASK_TYPES:
                ignored.add(task_id)
            else:
                replacement[task_id] = _task_kind(task_type)
        events = tuple(
            _activity(
                conversation_id,
                kind,
                task_id,
                ClaudeStructuredActivityState.FINISHED,
            )
            for task_id, kind in self._tasks.items()
            if replacement.get(task_id) != kind
        ) + tuple(
            _activity(
                conversation_id,
                kind,
                task_id,
                ClaudeStructuredActivityState.STARTED,
            )
            for task_id, kind in replacement.items()
            if self._tasks.get(task_id) != kind
        )
        self._tasks = replacement
        self._ignored_tasks = ignored
        self._background_observed = True
        if replacement:
            self._session_idle = False
        return events, self._idle_proof()

    def start_task(
        self,
        task_id: str,
        task_type: str | None,
        conversation_id: ClaudeStructuredConversationId | None,
    ) -> tuple[ClaudeStructuredStreamEvent, ...]:
        """Apply one non-authoritative task-start edge."""
        if task_type in _IGNORED_TASK_TYPES:
            if task_id not in self._tasks:
                self._ignored_tasks.add(task_id)
                return ()
            self._degraded = True
            self._invalidate_task_proof()
            return ()
        self._invalidate_task_proof()
        if task_id in self._tasks or task_id in self._ignored_tasks:
            self._degraded = True
            return ()
        kind = _task_kind(task_type)
        self._tasks[task_id] = kind
        return (
            _activity(
                conversation_id,
                kind,
                task_id,
                ClaudeStructuredActivityState.STARTED,
            ),
        )

    def update_task(
        self,
        task_id: str,
        terminal: bool,
        conversation_id: ClaudeStructuredConversationId | None,
    ) -> tuple[ClaudeStructuredStreamEvent, ...]:
        """Apply one non-authoritative task update or completion edge."""
        if task_id in self._ignored_tasks:
            if terminal:
                self._ignored_tasks.remove(task_id)
            return ()
        self._invalidate_task_proof()
        kind = self._tasks.get(task_id)
        if kind is None:
            self._degraded = True
            return ()
        if not terminal:
            return ()
        del self._tasks[task_id]
        return (
            _activity(
                conversation_id,
                kind,
                task_id,
                ClaudeStructuredActivityState.FINISHED,
            ),
        )

    def _invalidate_task_proof(self) -> None:
        self._session_idle = False
        self._background_observed = False

    def _idle_proof(self) -> bool:
        safe = (
            self._session_idle
            and self._background_observed
            and not self._tasks
        )
        if safe:
            self._degraded = False
        return safe and not self._degraded


def _activity(
    conversation_id: ClaudeStructuredConversationId | None,
    kind: ClaudeStructuredActivityKind,
    activity_id: str,
    state: ClaudeStructuredActivityState,
) -> ClaudeStructuredStreamEvent:
    return ClaudeStructuredStreamEvent(
        conversation_id=conversation_id,
        activity_kind=kind,
        activity_id=activity_id,
        activity_state=state,
    )


def _task_kind(task_type: str | None) -> ClaudeStructuredActivityKind:
    if task_type == "local_agent":
        return ClaudeStructuredActivityKind.BACKGROUND_AGENT
    if task_type == "local_bash":
        return ClaudeStructuredActivityKind.TERMINAL
    return ClaudeStructuredActivityKind.BACKGROUND_TASK
