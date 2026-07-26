"""One-shot usage and token-activity recovery copy."""

import shlex

from sidekick_usages.core.types import ProviderId
from sidekick_usages.usage.models import (
    AuthenticationFailure,
    CompleteTokenActivity,
    CredentialRecoveryKind,
    FailedTokenActivity,
    FetchFailure,
    ForbiddenFailure,
    InvalidExpiryFailure,
    PartialTokenActivity,
    PersistenceFailure,
    ProviderTokenActivity,
    RateLimitFailure,
    RefreshRejectedFailure,
    TokenActivityFailureKind,
    TokenActivityIssue,
)


def _authentication_failure_copy(
    failure: AuthenticationFailure | RefreshRejectedFailure,
    message_lines: tuple[str, ...],
) -> tuple[str, tuple[str, ...]]:
    if failure.credential_kind is CredentialRecoveryKind.CLAUDE_SETUP_TOKEN:
        command = shlex.join(
            [
                "sidekick-usages",
                "claude",
                "setup-token",
                "--label",
                failure.label,
                "--force",
            ]
        )
        return "authentication failed", (*message_lines, f"Run: {command}")
    if (
        failure.credential_kind
        is CredentialRecoveryKind.CLAUDE_SUBSCRIPTION_LOGIN
    ):
        command = shlex.join(["sidekick-usages", "refresh", failure.label])
        return (
            "authentication failed",
            (
                *message_lines,
                "Sign in to that Claude account, then run:",
                command,
            ),
        )
    command = shlex.join(["sidekick-usages", "codex", "login", failure.label])
    return (
        "login required",
        (
            *message_lines,
            "Run official managed Codex login:",
            command,
        ),
    )


def failure_copy(failure: FetchFailure) -> tuple[str, tuple[str, ...]]:
    """Map one typed application failure to human recovery copy."""
    message_lines = tuple(failure.message.splitlines())
    if isinstance(
        failure,
        AuthenticationFailure | RefreshRejectedFailure,
    ):
        return _authentication_failure_copy(failure, message_lines)
    if isinstance(failure, InvalidExpiryFailure):
        command = shlex.join(["sidekick-usages", "refresh", failure.label])
        return "invalid expiry", (*message_lines, command)
    if isinstance(failure, ForbiddenFailure):
        detail = list(message_lines)
        if failure.required_scope is not None:
            detail.append(f"Required scope: {failure.required_scope}.")
        return "forbidden", tuple(detail)
    if isinstance(failure, RateLimitFailure):
        detail = list(message_lines)
        if failure.retry_after_seconds is not None:
            detail.append(
                f"Retry after {failure.retry_after_seconds} seconds."
            )
        return "rate limited", tuple(detail)
    if isinstance(failure, PersistenceFailure):
        return (
            "state not saved",
            (
                "Usage was withheld because account changes were not durable.",
                *message_lines,
            ),
        )
    return "error", message_lines


def activity_issue_copy(
    provider_id: ProviderId,
    issue: TokenActivityIssue,
) -> tuple[str, ...]:
    """Return safe recovery detail for one account activity issue."""
    if (
        issue.kind is not TokenActivityFailureKind.AUTHENTICATION
        or issue.label is None
    ):
        return ()
    if provider_id is ProviderId.CODEX:
        action = "Run official managed Codex login:"
        command = shlex.join(
            ["sidekick-usages", "codex", "login", issue.label]
        )
    else:
        action = "Run official managed Claude login:"
        command = shlex.join(["sidekick-usages", "refresh", issue.label])
    return (
        action,
        command,
    )


def account_activity_issues(
    activity: ProviderTokenActivity | None,
) -> tuple[TokenActivityIssue, ...]:
    """Return only account-scoped issues suitable for warning rows."""
    if not isinstance(
        activity,
        CompleteTokenActivity | PartialTokenActivity | FailedTokenActivity,
    ):
        return ()
    return tuple(issue for issue in activity.issues if issue.label is not None)
