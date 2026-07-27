"""Compose exact-profile status with protected Claude credentials."""

from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from datetime import datetime

from sidekick_usages.core.accounts.types import ProviderIdentity
from sidekick_usages.providers.claude.auth.login.service import (
    claude_status_association_key,
    read_official_claude_auth_status,
)
from sidekick_usages.providers.claude.auth.storage.errors import (
    ClaudeProtectedStorageError,
)
from sidekick_usages.providers.claude.auth.storage.models import (
    ClaudeAuthoritySnapshot,
    ClaudeProtectedLogin,
)
from sidekick_usages.providers.claude.auth.storage.service import (
    protected_claude_credential,
    read_protected_claude_credential,
)
from sidekick_usages.providers.claude.auth.storage.types import (
    ClaudeCredentialFileSource,
    ClaudeProtectedStorageFailure,
)
from sidekick_usages.providers.claude.environment import (
    claude_profile_environment,
)
from sidekick_usages.providers.claude.errors import ClaudeProcessError
from sidekick_usages.providers.claude.managed.errors import ClaudeManagedError
from sidekick_usages.providers.claude.managed.models import ClaudeCapabilities
from sidekick_usages.providers.claude.process import (
    run_bounded_claude_command,
)
from sidekick_usages.providers.claude.types import ClaudeCommandRunner


def read_proven_claude_authority(
    capabilities: ClaudeCapabilities,
    files: ClaudeCredentialFileSource,
    reference_time: datetime,
    *,
    expected_identity: ProviderIdentity | None = None,
    environment: Mapping[str, str] | None = None,
    runner: ClaudeCommandRunner = run_bounded_claude_command,
) -> ClaudeAuthoritySnapshot:
    """Return one stable exact-profile Claude authority."""
    with proven_claude_login(
        capabilities,
        files,
        reference_time,
        expected_identity=expected_identity,
        environment=environment,
        runner=runner,
    ) as proven:
        return proven.snapshot


@contextmanager
def proven_claude_login(
    capabilities: ClaudeCapabilities,
    files: ClaudeCredentialFileSource,
    reference_time: datetime,
    *,
    expected_identity: ProviderIdentity | None = None,
    environment: Mapping[str, str] | None = None,
    runner: ClaudeCommandRunner = run_bounded_claude_command,
) -> Iterator[ClaudeProtectedLogin]:
    """Yield a stable association with active protected credentials."""
    before = read_protected_claude_credential(
        capabilities,
        files,
        reference_time,
        environment=environment,
        runner=runner,
    )
    association_key = _association_key(
        capabilities,
        environment,
        runner,
    )
    if expected_identity is not None and association_key != expected_identity:
        raise ClaudeProtectedStorageError(
            ClaudeProtectedStorageFailure.IDENTITY_MISMATCH
        )
    with protected_claude_credential(
        capabilities,
        files,
        reference_time,
        environment=environment,
        runner=runner,
    ) as protected:
        first = before.associated_with(association_key)
        proven = protected.snapshot.associated_with(association_key)
        if not same_claude_authority_proof(first, proven):
            raise ClaudeProtectedStorageError(
                ClaudeProtectedStorageFailure.PROOF_CHANGED
            )
        yield ClaudeProtectedLogin(proven, protected)


def same_claude_authority_proof(
    first: ClaudeAuthoritySnapshot,
    second: ClaudeAuthoritySnapshot,
) -> bool:
    """Compare association and protected semantics, not provenance."""
    return (
        first.provider_identity == second.provider_identity
        and first.generation == second.generation
        and first.plan == second.plan
        and first.scopes == second.scopes
        and first.access_expires_at == second.access_expires_at
        and first.refresh_expires_at == second.refresh_expires_at
        and first.health is second.health
        and first.action is second.action
    )


def _association_key(
    capabilities: ClaudeCapabilities,
    source_environment: Mapping[str, str] | None,
    runner: ClaudeCommandRunner,
) -> ProviderIdentity:
    environment: dict[str, str] = {}
    try:
        environment.update(
            claude_profile_environment(
                source_environment,
                capabilities.profile,
            )
        )
        status = read_official_claude_auth_status(
            capabilities.executable,
            environment,
            capabilities.profile.config_directory,
            runner=runner,
        )
    except ClaudeManagedError, ClaudeProcessError:
        raise ClaudeProtectedStorageError(
            ClaudeProtectedStorageFailure.UNREADABLE
        ) from None
    finally:
        environment.clear()
    association_key = claude_status_association_key(status)
    if association_key is None:
        raise ClaudeProtectedStorageError(
            ClaudeProtectedStorageFailure.IDENTITY_MISMATCH
        )
    return association_key
