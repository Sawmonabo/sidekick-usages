"""Provider-owned credential refresh staging tests."""

import os
import stat
import sys
from pathlib import Path

import pytest

from sidekick_usages.core.types import AccountLabel, ProviderId
from sidekick_usages.credentials.refresh import CredentialRefreshReason
from sidekick_usages.persistence.credentials.refresh.service import (
    CredentialRefreshTransactions,
)
from sidekick_usages.persistence.errors import UnsafeManagedFileError
from sidekick_usages.persistence.platform.models import NativeFile
from sidekick_usages.persistence.platform.posix import files
from sidekick_usages.providers.base import ProviderFailure, ProviderFailureKind
from tests.fakes.credentials.refresh import (
    BroadStageFailureProvider,
    ManagedStageRefreshProvider,
    login_account,
    refresh_coordinator,
)
from tests.support.persistence import make_account_store
from tests.support.time import REFERENCE_TIME

_PRIVATE_DIRECTORY_MODE = 0o700
_PRIVATE_FILE_MODE = 0o600


def test_staged_provider_uses_only_transactions_owned_private_home(
    tmp_path: Path,
) -> None:
    """CLI-capable providers cannot fall through to an unmanaged refresh."""
    label = AccountLabel("claude-team")
    store = make_account_store(tmp_path, (login_account(),))
    provider = ManagedStageRefreshProvider()
    root = tmp_path / "credential-refresh"
    coordinator = refresh_coordinator(store, provider, root)

    result = coordinator.refresh(
        provider_id=ProviderId.CLAUDE,
        label=label,
        reason=CredentialRefreshReason.OPERATOR_FORCED,
    )

    assert not isinstance(result, ProviderFailure)
    assert provider.stage_home is not None
    assert provider.stage_home.is_relative_to(root)
    assert not provider.stage_home.exists()


@pytest.mark.skipif(
    not sys.platform.startswith("linux"),
    reason="Linux provider-stage mode normalization",
)
def test_provider_created_nonwritable_directory_is_cleaned_after_failure(
    tmp_path: Path,
) -> None:
    """A conventional provider directory cannot strand failed refresh state."""
    label = AccountLabel("claude-team")
    store = make_account_store(tmp_path, (login_account(),))
    provider = BroadStageFailureProvider()
    root = tmp_path / "credential-refresh"
    coordinator = refresh_coordinator(store, provider, root)

    result = coordinator.refresh(
        provider_id=ProviderId.CLAUDE,
        label=label,
        reason=CredentialRefreshReason.OPERATOR_FORCED,
    )

    assert isinstance(result, ProviderFailure)
    assert result.kind is ProviderFailureKind.REJECTED
    assert provider.stage_home is not None
    assert not provider.stage_home.exists()
    assert not any(path.is_dir() for path in root.iterdir())


def test_claude_stage_reader_normalizes_then_reads_provider_output(
    tmp_path: Path,
) -> None:
    """The injected reader hardens only provider output before parsing it."""
    label = AccountLabel("claude-team")
    store = make_account_store(tmp_path, (login_account(),))
    transactions = CredentialRefreshTransactions(
        store,
        tmp_path / "credential-refresh",
    )
    with transactions.hold_stable(
        provider_id=ProviderId.CLAUDE,
        label=label,
        reason=CredentialRefreshReason.OPERATOR_FORCED.value,
        started_at=REFERENCE_TIME,
    ) as lease:
        stage_home = transactions.prepare_provider_stage(lease)
        credentials = stage_home / ".claude" / ".credentials.json"
        credentials.write_bytes(b"test-only-qualified-credentials")
        credentials.chmod(0o644 if sys.platform.startswith("linux") else 0o600)
        backups = stage_home / ".claude" / "backups"
        backups.mkdir(
            mode=0o755 if sys.platform.startswith("linux") else 0o700
        )
        if sys.platform.startswith("linux"):
            backups.chmod(0o755)

        assert transactions.read_provider_stage(lease) == (
            b"test-only-qualified-credentials"
        )
        if sys.platform.startswith("linux"):
            assert stat.S_IMODE(credentials.stat().st_mode) == (
                _PRIVATE_FILE_MODE
            )
            assert stat.S_IMODE(backups.stat().st_mode) == (
                _PRIVATE_DIRECTORY_MODE
            )


@pytest.mark.skipif(os.name == "nt", reason="POSIX private-file fixtures")
@pytest.mark.parametrize("unsafe_kind", ["symlink", "hardlink", "mode"])
def test_claude_stage_reader_rejects_unsafe_file_identity(
    tmp_path: Path,
    unsafe_kind: str,
) -> None:
    """Links and exposed modes cannot become refreshed authority."""
    label = AccountLabel("claude-team")
    store = make_account_store(tmp_path, (login_account(),))
    transactions = CredentialRefreshTransactions(
        store,
        tmp_path / "credential-refresh",
    )
    with transactions.hold_stable(
        provider_id=ProviderId.CLAUDE,
        label=label,
        reason=CredentialRefreshReason.OPERATOR_FORCED.value,
        started_at=REFERENCE_TIME,
    ) as lease:
        stage_home = transactions.prepare_provider_stage(lease)
        credentials = stage_home / ".claude" / ".credentials.json"
        outside = tmp_path / "outside-credentials.json"
        outside.write_bytes(b"test-only-outside-credentials")
        outside.chmod(0o600)
        if unsafe_kind == "symlink":
            credentials.symlink_to(outside)
        elif unsafe_kind == "hardlink":
            credentials.hardlink_to(outside)
        else:
            credentials.write_bytes(b"test-only-exposed-credentials")
            credentials.chmod(0o660)

        with pytest.raises(UnsafeManagedFileError):
            transactions.read_provider_stage(lease)


@pytest.mark.skipif(os.name == "nt", reason="POSIX same-entry fixture")
def test_claude_stage_reader_rejects_path_replacement_during_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The staged path must still name the descriptor that was read."""
    label = AccountLabel("claude-team")
    store = make_account_store(tmp_path, (login_account(),))
    transactions = CredentialRefreshTransactions(
        store,
        tmp_path / "credential-refresh",
    )
    with transactions.hold_stable(
        provider_id=ProviderId.CLAUDE,
        label=label,
        reason=CredentialRefreshReason.OPERATOR_FORCED.value,
        started_at=REFERENCE_TIME,
    ) as lease:
        stage_home = transactions.prepare_provider_stage(lease)
        credentials = stage_home / ".claude" / ".credentials.json"
        credentials.write_bytes(b"test-only-original-credentials")
        credentials.chmod(0o600)
        replacement = tmp_path / "replacement-credentials.json"
        replacement.write_bytes(b"test-only-replacement-credentials")
        replacement.chmod(0o600)
        original_read = files.read_descriptor
        swapped = False

        def swap_during_read(
            descriptor: int,
            root_device: int,
            limit: int,
        ) -> NativeFile:
            nonlocal swapped
            if not swapped:
                swapped = True
                os.replace(replacement, credentials)
            return original_read(descriptor, root_device, limit)

        monkeypatch.setattr(
            files,
            "read_descriptor",
            swap_during_read,
        )

        with pytest.raises(UnsafeManagedFileError):
            transactions.read_provider_stage(lease)
