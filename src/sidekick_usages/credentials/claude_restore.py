"""Targeted restoration of one imported Claude setup token."""

from dataclasses import dataclass, field, replace
from typing import Protocol

from sidekick_usages.core.models import (
    Account,
    ClaudeLoginCredentials,
    ClaudeSetupTokenCredentials,
    UsageReport,
)
from sidekick_usages.core.types import AccountLabel, ProviderId
from sidekick_usages.http import HttpClient
from sidekick_usages.persistence.account_store import AccountStore
from sidekick_usages.persistence.artifacts import FileSnapshot
from sidekick_usages.persistence.credential_transactions import (
    CredentialSourceGuard,
)
from sidekick_usages.persistence.errors import DurabilityUncertainError
from sidekick_usages.persistence.filesystem import PersistenceFilesystem
from sidekick_usages.persistence.schemas import decode_prototype
from sidekick_usages.providers.base import (
    ProviderBoundaryError,
    ProviderFailure,
    ProviderFailureKind,
)
from sidekick_usages.providers.claude.credential_schemas import (
    validate_setup_token,
)


@dataclass(frozen=True, slots=True)
class ClaudeSetupTokenRestorePreview:
    """Validated secret-safe authorization for one exact restore source."""

    label: AccountLabel
    previous_authentication: str
    source: FileSnapshot = field(repr=False)
    credentials: ClaudeSetupTokenCredentials = field(repr=False)


@dataclass(frozen=True, slots=True)
class ClaudeSetupTokenRestoreSuccess:
    """One exact Claude credential was restored and reopened."""

    label: AccountLabel


type ClaudeSetupTokenRestoreResult = (
    ClaudeSetupTokenRestoreSuccess | ProviderFailure
)
type ClaudeSetupTokenRestorePreviewResult = (
    ClaudeSetupTokenRestorePreview | ProviderFailure
)


class _ClaudeUsageProvider(Protocol):
    """Expose only the existing provider usage-verification boundary."""

    def fetch_usage(
        self,
        account: Account,
        http: HttpClient,
    ) -> UsageReport:
        """Fetch usage for one candidate Claude account."""


def _failure(
    kind: ProviderFailureKind,
    message: str,
) -> ProviderFailure:
    """Return one secret-safe targeted-restore failure."""
    return ProviderFailure(
        provider_id=ProviderId.CLAUDE,
        kind=kind,
        message=message,
    )


class ClaudeSetupTokenRestoreService:
    """Restore one exact setup token through guarded account persistence."""

    def __init__(
        self,
        store: AccountStore,
        http: HttpClient,
        usage_provider: _ClaudeUsageProvider | None,
    ) -> None:
        self._store = store
        self._http = http
        self._usage_provider = usage_provider
        self._prototype = PersistenceFilesystem(
            store.locations.prototype_cc_usage
        )

    def preview(
        self,
        label: AccountLabel,
    ) -> ClaudeSetupTokenRestorePreviewResult:
        """Validate one exact source and target before user confirmation."""
        source = self._prototype.read_external_private_source()
        if source is None:
            return _failure(
                ProviderFailureKind.MISSING,
                "The import-only Claude prototype is not available.",
            )
        prototype = decode_prototype(source.data)
        record = next(
            (
                account
                for account in prototype.accounts
                if account.label == label
            ),
            None,
        )
        if record is None:
            return _failure(
                ProviderFailureKind.MISSING,
                f"The prototype has no Claude setup token named '{label}'.",
            )
        try:
            token = validate_setup_token(record.token)
        except ProviderBoundaryError:
            return _failure(
                ProviderFailureKind.MALFORMED,
                f"The prototype record named '{label}' is not a valid "
                "Claude setup token.",
            )
        target = self._store.read_fresh(label)
        if target is None:
            return _failure(
                ProviderFailureKind.MISSING,
                f"No current account is named '{label}'.",
            )
        if target.provider_id is not ProviderId.CLAUDE:
            return _failure(
                ProviderFailureKind.IDENTITY_MISMATCH,
                f"The current account named '{label}' is not a Claude "
                "account.",
            )
        previous = (
            "subscription login"
            if isinstance(target.credentials, ClaudeLoginCredentials)
            else "setup token"
        )
        return ClaudeSetupTokenRestorePreview(
            label=label,
            previous_authentication=previous,
            source=source,
            credentials=ClaudeSetupTokenCredentials(access_token=token),
        )

    def restore(
        self,
        preview: ClaudeSetupTokenRestorePreview,
    ) -> ClaudeSetupTokenRestoreResult:
        """Guard, replace, commit, reopen, and prove one restore target."""
        current = self._store.read_fresh(preview.label)
        if current is None:
            return _failure(
                ProviderFailureKind.MISSING,
                f"No current account is named '{preview.label}'.",
            )
        if current.provider_id is not ProviderId.CLAUDE:
            return _failure(
                ProviderFailureKind.IDENTITY_MISMATCH,
                f"The current account named '{preview.label}' is not a "
                "Claude account.",
            )
        candidate = replace(
            current,
            credentials=preview.credentials,
            last_refresh_at=None,
            last_refresh_status=None,
            last_refresh_error=None,
        )
        if self._usage_provider is None:
            return _failure(
                ProviderFailureKind.UNSUPPORTED,
                "Claude provider verification is unavailable.",
            )
        self._usage_provider.fetch_usage(candidate, self._http)
        source_guard = CredentialSourceGuard(
            self._prototype.authority_path,
            preview.source.fingerprint,
            self._prototype.read_external_private_source,
        )
        self._store.persist_credentials(
            candidate,
            source_guard=source_guard,
        )
        reopened = self._store.read_fresh(preview.label)
        source = self._prototype.read_external_private_source()
        if (
            reopened is None
            or reopened.credentials != preview.credentials
            or source is None
            or source.fingerprint != preview.source.fingerprint
            or source.data != preview.source.data
        ):
            raise DurabilityUncertainError(self._store.path.name)
        return ClaudeSetupTokenRestoreSuccess(preview.label)


__all__ = [
    "ClaudeSetupTokenRestorePreview",
    "ClaudeSetupTokenRestorePreviewResult",
    "ClaudeSetupTokenRestoreResult",
    "ClaudeSetupTokenRestoreService",
    "ClaudeSetupTokenRestoreSuccess",
]
