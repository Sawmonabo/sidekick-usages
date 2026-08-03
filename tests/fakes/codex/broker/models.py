"""Models for synthetic Codex broker behavior."""

from dataclasses import dataclass, field
from pathlib import Path

from sidekick_usages.paths import ApplicationPaths
from sidekick_usages.persistence.private.credentials import (
    PrivateCredentialTree,
)
from sidekick_usages.providers.codex.app_server.models import CodexExecutable


@dataclass(frozen=True, slots=True)
class FakeWorkerRoute:
    """One worker router and its hung-operation start marker."""

    executable: Path
    started: Path


@dataclass(frozen=True, slots=True)
class FakeCodexBrokerFixture:
    """Shared filesystem and executable state for resident broker tests."""

    paths: ApplicationPaths
    environment: dict[str, str] = field(repr=False)
    executable: CodexExecutable
    private: PrivateCredentialTree = field(repr=False)
    provider_root: Path
    native_home: Path
    session_home: Path
    native_auth: Path
