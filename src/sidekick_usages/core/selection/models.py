"""Validated models for provider selection and durable operations."""

from dataclasses import dataclass
from datetime import datetime

from sidekick_usages.core.accounts.types import (
    AuthorityGeneration,
    OperationId,
    ProviderIdentity,
    SidekickAccountId,
)
from sidekick_usages.core.selection.types import (
    ActivationOutcome,
    ActivationPhase,
    OperationKind,
    OperationPriority,
    OperationState,
    ProviderAuthState,
    ProviderRuntimeState,
)
from sidekick_usages.core.time import as_utc
from sidekick_usages.core.types import ProviderId

_MAX_ATTEMPTS = 1_000_000
_MAX_SAFE_CODE_BYTES = 128


def safe_outcome_code(value: str | None) -> str | None:
    """Validate one bounded non-secret machine-readable outcome code."""
    if value is None:
        return None
    if not isinstance(value, str):
        raise TypeError("Safe outcome code must be text.")
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError:
        raise ValueError("Safe outcome code must be valid UTF-8.") from None
    if (
        not encoded
        or len(encoded) > _MAX_SAFE_CODE_BYTES
        or not all(
            character.isascii()
            and (
                character.islower() or character.isdigit() or character == "_"
            )
            for character in value
        )
    ):
        raise ValueError(
            "Safe outcome code must use bounded lowercase ASCII identifiers."
        )
    return value


@dataclass(frozen=True, slots=True, kw_only=True)
class ProviderAuthObservation:
    """One strict secret-free observation of native provider authentication."""

    provider_id: ProviderId
    state: ProviderAuthState
    provider_identity: ProviderIdentity | None
    generation: AuthorityGeneration | None
    observed_at: datetime

    def __post_init__(self) -> None:
        """Require complete identity only for active authentication."""
        object.__setattr__(self, "observed_at", as_utc(self.observed_at))
        if self.state is ProviderAuthState.ACTIVE:
            if self.provider_identity is None or self.generation is None:
                raise ValueError(
                    "Active provider authentication requires identity."
                )
            return
        if self.provider_identity is not None or self.generation is not None:
            raise ValueError(
                "Inactive provider authentication cannot claim identity."
            )


@dataclass(frozen=True, slots=True, kw_only=True)
class SelectedAccountState:
    """Last provider-verified runtime authentication state."""

    provider_id: ProviderId
    runtime_state: ProviderRuntimeState
    account_id: SidekickAccountId | None
    provider_identity: ProviderIdentity | None
    runtime_generation: AuthorityGeneration | None
    verified_at: datetime
    outcome: ActivationOutcome

    def __post_init__(self) -> None:
        """Require a complete state-specific provider observation."""
        object.__setattr__(self, "verified_at", as_utc(self.verified_at))
        if self.runtime_state is ProviderRuntimeState.SAVED_ACTIVE:
            if (
                self.account_id is None
                or self.provider_identity is None
                or self.runtime_generation is None
                or self.outcome
                not in {
                    ActivationOutcome.VERIFIED,
                    ActivationOutcome.ROLLED_BACK,
                    ActivationOutcome.EXTERNAL_RECONCILED,
                }
            ):
                raise ValueError(
                    "Saved-active state requires complete verified identity."
                )
            return
        if self.runtime_state is ProviderRuntimeState.EXTERNAL_ACTIVE:
            if (
                self.account_id is not None
                or self.provider_identity is None
                or self.runtime_generation is None
                or self.outcome is not ActivationOutcome.EXTERNAL_RECONCILED
            ):
                raise ValueError(
                    "External-active state requires unowned provider identity."
                )
            return
        if (
            self.account_id is not None
            or self.provider_identity is not None
            or self.runtime_generation is not None
        ):
            raise ValueError(
                "Inactive provider state cannot claim account identity."
            )
        expected = {
            ProviderRuntimeState.LOGGED_OUT: ActivationOutcome.LOGGED_OUT,
            ProviderRuntimeState.UNREADABLE: (
                ActivationOutcome.RECONCILIATION_REQUIRED
            ),
            ProviderRuntimeState.UNSUPPORTED: ActivationOutcome.UNSUPPORTED,
        }[self.runtime_state]
        if self.outcome is not expected:
            raise ValueError("Provider runtime state and outcome disagree.")


def activation_account_ids(
    selected_baseline: SelectedAccountState | None,
    target_account_id: SidekickAccountId,
) -> frozenset[SidekickAccountId]:
    """Return the exact saved-account authority set for one activation."""
    source = (
        None if selected_baseline is None else selected_baseline.account_id
    )
    return frozenset(
        {target_account_id} if source is None else {source, target_account_id}
    )


@dataclass(frozen=True, slots=True, kw_only=True)
class ActivationRecord:
    """One secret-free provider activation journal record."""

    provider_id: ProviderId
    operation_id: OperationId
    selected_baseline: SelectedAccountState | None
    native_auth_baseline: ProviderAuthObservation
    target_account_id: SidekickAccountId
    expected_target_identity: ProviderIdentity
    expected_target_generation: AuthorityGeneration
    phase: ActivationPhase
    started_at: datetime
    updated_at: datetime
    outcome: ActivationOutcome | None = None
    failure_code: str | None = None

    def __post_init__(self) -> None:
        """Normalize timestamps and enforce terminal result invariants."""
        started_at = as_utc(self.started_at)
        updated_at = as_utc(self.updated_at)
        if updated_at < started_at:
            raise ValueError("Activation update cannot predate its start.")
        if (
            self.selected_baseline is not None
            and self.selected_baseline.provider_id is not self.provider_id
        ):
            raise ValueError(
                "Selected baseline must match the activation provider."
            )
        if self.native_auth_baseline.provider_id is not self.provider_id:
            raise ValueError(
                "Native authentication must match the activation provider."
            )
        if (
            self.selected_baseline is not None
            and self.selected_baseline.account_id == self.target_account_id
        ):
            raise ValueError("Activation source and target must differ.")
        _validate_activation_outcome(self.phase, self.outcome)
        object.__setattr__(self, "started_at", started_at)
        object.__setattr__(self, "updated_at", updated_at)
        object.__setattr__(
            self,
            "failure_code",
            safe_outcome_code(self.failure_code),
        )

    @property
    def account_ids(self) -> frozenset[SidekickAccountId]:
        """Return the complete account authority set for this activation."""
        return activation_account_ids(
            self.selected_baseline,
            self.target_account_id,
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class DueOperation:
    """One durable operation slot keyed by account and operation kind."""

    operation_id: OperationId
    provider_id: ProviderId
    account_id: SidekickAccountId
    kind: OperationKind
    priority: OperationPriority
    state: OperationState
    due_at: datetime
    updated_at: datetime
    attempts: int = 0
    failure_code: str | None = None

    def __post_init__(self) -> None:
        """Normalize wall time and validate retry state."""
        if (
            type(self.attempts) is not int
            or self.attempts < 0
            or self.attempts > _MAX_ATTEMPTS
        ):
            raise ValueError("Operation attempts are outside the bound.")
        due_at = as_utc(self.due_at)
        updated_at = as_utc(self.updated_at)
        failure_code = safe_outcome_code(self.failure_code)
        if (
            self.state
            in {
                OperationState.RETRY_WAIT,
                OperationState.ACTION_REQUIRED,
            }
            and failure_code is None
        ):
            raise ValueError("Failed operation state requires a safe code.")
        if (
            self.state in {OperationState.SCHEDULED, OperationState.RUNNING}
            and failure_code is not None
        ):
            raise ValueError("Healthy operation state cannot carry failure.")
        callback_kind = self.kind is OperationKind.CODEX_CALLBACK
        callback_priority = self.priority is OperationPriority.CODEX_CALLBACK
        if callback_kind != callback_priority or (
            callback_kind and self.provider_id is not ProviderId.CODEX
        ):
            raise ValueError(
                "Codex callback kind and priority must be used together."
            )
        object.__setattr__(self, "due_at", due_at)
        object.__setattr__(self, "updated_at", updated_at)
        object.__setattr__(self, "failure_code", failure_code)


def _validate_activation_outcome(
    phase: ActivationPhase,
    outcome: ActivationOutcome | None,
) -> None:
    """Require each phase to carry only its truthful safe outcome."""
    allowed: frozenset[ActivationOutcome | None]
    if phase is ActivationPhase.COMMITTED:
        allowed = frozenset({ActivationOutcome.VERIFIED})
    elif phase is ActivationPhase.ROLLED_BACK:
        allowed = frozenset(
            {
                ActivationOutcome.ROLLED_BACK,
                ActivationOutcome.EXTERNAL_RECONCILED,
                ActivationOutcome.LOGGED_OUT,
            }
        )
    elif phase is ActivationPhase.RECONCILIATION_REQUIRED:
        allowed = frozenset({ActivationOutcome.RECONCILIATION_REQUIRED})
    else:
        allowed = frozenset({None})
    if outcome not in allowed:
        raise ValueError("Activation phase and outcome disagree.")
