"""Synthetic provider capability evidence for daemon and Doctor tests."""

import sys
from pathlib import Path

from sidekick_usages.core.types import ProviderId
from sidekick_usages.credentials.capabilities.models import (
    ProviderCapabilityReport,
    ProviderCapabilityResult,
)
from sidekick_usages.platform.models import ExecutableProvenance
from sidekick_usages.providers.claude.managed.models import (
    ClaudeRuntimeCapabilities,
)
from sidekick_usages.providers.claude.managed.types import (
    ClaudeManagedPlatform,
)
from sidekick_usages.providers.claude.models import (
    ClaudeExecutable,
    ClaudeVersion,
)
from sidekick_usages.providers.codex.app_server.models import (
    CodexAppServerCapabilities,
    CodexExecutable,
    CodexVersion,
)
from sidekick_usages.providers.codex.app_server.types import (
    CodexAppServerFailure,
)

_SYNTHETIC_EXECUTABLE_ROOT = Path(sys.executable).resolve().parent


def make_provider_capability_report(
    *,
    codex_ready: bool = True,
) -> ProviderCapabilityReport:
    """Return deterministic secret-free provider capability evidence."""
    claude_executable = ClaudeExecutable(
        ExecutableProvenance(
            _SYNTHETIC_EXECUTABLE_ROOT / "claude",
            10,
            11,
            12,
            13,
        ),
        ClaudeVersion(2, 1, 220),
    )
    codex_executable = CodexExecutable(
        ExecutableProvenance(
            _SYNTHETIC_EXECUTABLE_ROOT / "codex",
            20,
            21,
            22,
            23,
        ),
        CodexVersion(0, 145, 0),
    )
    codex_result = (
        ProviderCapabilityResult.succeeded(
            ProviderId.CODEX,
            codex_executable,
            CodexAppServerCapabilities(
                codex_executable,
                "a" * 64,
            ),
        )
        if codex_ready
        else ProviderCapabilityResult.failed(
            ProviderId.CODEX,
            CodexAppServerFailure.CAPABILITY_UNSUPPORTED,
            executable=codex_executable,
        )
    )
    return ProviderCapabilityReport(
        (
            ProviderCapabilityResult.succeeded(
                ProviderId.CLAUDE,
                claude_executable,
                ClaudeRuntimeCapabilities(
                    claude_executable,
                    ClaudeManagedPlatform.LINUX_FILE,
                ),
            ),
            codex_result,
        )
    )


class StaticProviderCapabilityService:
    """Expose one synthetic immutable report through production ports."""

    def __init__(self, report: ProviderCapabilityReport) -> None:
        self._report = report
        self.requested_provider_ids: list[ProviderId] = []

    def cancel(self) -> None:
        """Leave immutable evidence unchanged."""

    def ready(self, provider_id: ProviderId) -> bool:
        """Return one provider result's readiness."""
        self.requested_provider_ids.append(provider_id)
        return self._report.result(provider_id).ready

    def report(
        self,
        provider_id: ProviderId | None = None,
    ) -> ProviderCapabilityReport:
        """Return deterministic complete or provider-scoped evidence."""
        if provider_id is None:
            return self._report
        self.requested_provider_ids.append(provider_id)
        return ProviderCapabilityReport((self._report.result(provider_id),))
