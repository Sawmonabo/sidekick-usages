"""Secret-free authoritative provider capability evidence."""

from dataclasses import dataclass
from typing import assert_never

from sidekick_usages.core.types import ProviderId
from sidekick_usages.credentials.capabilities.types import (
    ProviderCapabilityEvidence,
    ProviderCapabilityFailure,
    ProviderExecutable,
)
from sidekick_usages.platform.models import ExecutableProvenance
from sidekick_usages.providers.claude.managed.models import (
    ClaudeRuntimeCapabilities,
)
from sidekick_usages.providers.claude.managed.types import (
    ClaudeManagedFailure,
)
from sidekick_usages.providers.claude.models import ClaudeExecutable
from sidekick_usages.providers.codex.app_server.models import (
    CodexAppServerCapabilities,
    CodexExecutable,
)
from sidekick_usages.providers.codex.app_server.types import (
    CodexAppServerFailure,
)


@dataclass(frozen=True, slots=True, kw_only=True)
class ProviderCapabilityResult:
    """One exact provider executable and capability outcome."""

    provider_id: ProviderId
    executable: ProviderExecutable | None
    capabilities: ProviderCapabilityEvidence | None
    failure: ProviderCapabilityFailure | None

    def __post_init__(self) -> None:
        """Require one provider-matched success or sanitized failure."""
        if (self.capabilities is None) == (self.failure is None):
            raise ValueError(
                "Capability evidence requires exactly one terminal outcome."
            )
        match self.provider_id:
            case ProviderId.CLAUDE:
                valid_executable = self.executable is None or isinstance(
                    self.executable,
                    ClaudeExecutable,
                )
                valid_capabilities = self.capabilities is None or isinstance(
                    self.capabilities,
                    ClaudeRuntimeCapabilities,
                )
                valid_failure = self.failure is None or isinstance(
                    self.failure,
                    ClaudeManagedFailure,
                )
            case ProviderId.CODEX:
                valid_executable = self.executable is None or isinstance(
                    self.executable,
                    CodexExecutable,
                )
                valid_capabilities = self.capabilities is None or isinstance(
                    self.capabilities,
                    CodexAppServerCapabilities,
                )
                valid_failure = self.failure is None or isinstance(
                    self.failure,
                    CodexAppServerFailure,
                )
            case _:
                assert_never(self.provider_id)
        if not valid_executable or not valid_capabilities or not valid_failure:
            raise ValueError(
                "Capability evidence does not match its provider."
            )
        if self.capabilities is not None:
            if self.executable is None:
                raise ValueError(
                    "Successful capability evidence requires an executable."
                )
            if self.capabilities.executable != self.executable:
                raise ValueError(
                    "Capability evidence executable identity changed."
                )

    @classmethod
    def succeeded(
        cls,
        provider_id: ProviderId,
        executable: ProviderExecutable,
        capabilities: ProviderCapabilityEvidence,
    ) -> ProviderCapabilityResult:
        """Build one provider-matched successful result."""
        return cls(
            provider_id=provider_id,
            executable=executable,
            capabilities=capabilities,
            failure=None,
        )

    @classmethod
    def failed(
        cls,
        provider_id: ProviderId,
        failure: ProviderCapabilityFailure,
        *,
        executable: ProviderExecutable | None = None,
    ) -> ProviderCapabilityResult:
        """Build one sanitized failure retaining qualified provenance."""
        return cls(
            provider_id=provider_id,
            executable=executable,
            capabilities=None,
            failure=failure,
        )

    @property
    def ready(self) -> bool:
        """Return whether the exact provider capability gate passed."""
        return self.capabilities is not None

    @property
    def provenance(self) -> ExecutableProvenance | None:
        """Return qualified executable identity when discovery succeeded."""
        executable = self.executable
        return None if executable is None else executable.provenance

    @property
    def version(self) -> str | None:
        """Return the qualified executable version when known."""
        executable = self.executable
        return None if executable is None else str(executable.version)

    @property
    def failure_code(self) -> str | None:
        """Return the exact provider-owned sanitized failure code."""
        failure = self.failure
        return None if failure is None else failure.value


@dataclass(frozen=True, slots=True)
class ProviderCapabilityReport:
    """Deterministic complete capability evidence for every provider."""

    results: tuple[ProviderCapabilityResult, ...]

    def __post_init__(self) -> None:
        """Require exactly one result in canonical provider order."""
        provider_ids = tuple(result.provider_id for result in self.results)
        if provider_ids != tuple(ProviderId):
            raise ValueError(
                "Capability report must follow canonical provider order."
            )

    @property
    def ready_provider_ids(self) -> tuple[ProviderId, ...]:
        """Return capable providers in canonical order."""
        return tuple(
            result.provider_id for result in self.results if result.ready
        )

    def result(self, provider_id: ProviderId) -> ProviderCapabilityResult:
        """Return the authoritative result for one provider."""
        return next(
            result
            for result in self.results
            if result.provider_id is provider_id
        )
