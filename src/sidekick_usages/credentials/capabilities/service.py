"""Cached read-only provider capability evidence."""

import os
from collections.abc import Callable, Mapping
from threading import Event, Lock
from typing import assert_never

from sidekick_usages.core.types import ProviderId
from sidekick_usages.credentials.capabilities.models import (
    ProviderCapabilityReport,
    ProviderCapabilityResult,
)
from sidekick_usages.credentials.claude.managed.profile import (
    ClaudeProfileCapabilityFactory,
)
from sidekick_usages.paths import ApplicationPaths
from sidekick_usages.persistence.errors import PersistenceError
from sidekick_usages.persistence.private.credentials import (
    PrivateCredentialTree,
)
from sidekick_usages.providers.claude.managed.errors import ClaudeManagedError
from sidekick_usages.providers.claude.managed.types import (
    ClaudeManagedFailure,
)
from sidekick_usages.providers.codex.app_server.capabilities import (
    probe_codex_capabilities,
)
from sidekick_usages.providers.codex.app_server.errors import (
    CodexAppServerError,
)
from sidekick_usages.providers.codex.app_server.executable import (
    discover_codex_executable,
)
from sidekick_usages.providers.codex.app_server.models import CodexExecutable
from sidekick_usages.providers.codex.app_server.types import (
    CodexAppServerFailure,
)


class ProviderCapabilityService:
    """Probe each exact provider gate once without provider-auth mutation."""

    def __init__(
        self,
        claude: ClaudeProfileCapabilityFactory | ProviderCapabilityResult,
        environment: Mapping[str, str],
    ) -> None:
        if (
            isinstance(claude, ProviderCapabilityResult)
            and claude.provider_id is not ProviderId.CLAUDE
        ):
            raise ValueError(
                "Claude capability result has the wrong provider."
            )
        self._claude = claude
        self._environment = dict(environment)
        self._cancelled = Event()
        self._lock = Lock()
        self._results: dict[ProviderId, ProviderCapabilityResult] = {}

    def cancel(self) -> None:
        """Interrupt cancellable probes and reject late readiness."""
        self._cancelled.set()
        claude = self._claude
        if isinstance(claude, ClaudeProfileCapabilityFactory):
            claude.cancel()

    def ready(self, provider_id: ProviderId) -> bool:
        """Return whether one authoritative provider capability gate passed."""
        if self._cancelled.is_set():
            return False
        result = self.probe(provider_id)
        return result.ready and not self._cancelled.is_set()

    def probe(self, provider_id: ProviderId) -> ProviderCapabilityResult:
        """Return one cached secret-free capability result."""
        with self._lock:
            cached = self._results.get(provider_id)
            if cached is not None:
                return cached
            match provider_id:
                case ProviderId.CLAUDE:
                    result = self._probe_claude()
                case ProviderId.CODEX:
                    result = _probe_codex_result(
                        self._environment,
                        self._cancelled.is_set,
                    )
                case _:
                    assert_never(provider_id)
            self._results[provider_id] = result
            return result

    def report(self) -> ProviderCapabilityReport:
        """Return deterministic evidence for every supported provider."""
        return ProviderCapabilityReport(
            tuple(self.probe(provider_id) for provider_id in ProviderId)
        )

    def _probe_claude(self) -> ProviderCapabilityResult:
        claude = self._claude
        return (
            claude
            if isinstance(claude, ProviderCapabilityResult)
            else claude.result()
        )


def build_provider_capability_service(
    paths: ApplicationPaths,
    environment: Mapping[str, str] | None = None,
) -> ProviderCapabilityService:
    """Compose isolated provider probes around stable Sidekick paths."""
    source = os.environ if environment is None else environment
    try:
        profiles = PrivateCredentialTree(
            paths.private_claude_profiles,
            account_path=paths.accounts,
        )
        claude: ClaudeProfileCapabilityFactory | ProviderCapabilityResult = (
            ClaudeProfileCapabilityFactory(
                paths,
                profiles,
                environment=source,
            )
        )
    except ClaudeManagedError as error:
        claude = ProviderCapabilityResult.failed(
            ProviderId.CLAUDE,
            error.code,
        )
    except PersistenceError, ValueError:
        claude = ProviderCapabilityResult.failed(
            ProviderId.CLAUDE,
            ClaudeManagedFailure.PROFILE_UNSAFE,
        )
    return ProviderCapabilityService(claude, source)


def _probe_codex_result(
    environment: Mapping[str, str],
    cancelled: Callable[[], bool],
) -> ProviderCapabilityResult:
    executable: CodexExecutable | None = None
    try:
        executable = discover_codex_executable(
            environment,
            cancelled=cancelled,
        )
        capabilities = probe_codex_capabilities(
            executable,
            environment,
            cancelled=cancelled,
        )
    except CodexAppServerError as error:
        failure = error.code
    except OSError, ValueError:
        failure = CodexAppServerFailure.PROCESS_FAILED
    else:
        return ProviderCapabilityResult.succeeded(
            ProviderId.CODEX,
            executable,
            capabilities,
        )
    return ProviderCapabilityResult.failed(
        ProviderId.CODEX,
        failure,
        executable=executable,
    )
