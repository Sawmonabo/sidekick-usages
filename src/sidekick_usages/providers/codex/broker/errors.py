"""Secret-safe shared Codex daemon failures."""

from sidekick_usages.errors import UsageError
from sidekick_usages.providers.codex.app_server.errors import (
    CodexAppServerError,
)
from sidekick_usages.providers.codex.app_server.types import (
    CodexAppServerFailure,
)
from sidekick_usages.providers.codex.broker.types import CodexBrokerFailure
from sidekick_usages.providers.codex.session.errors import (
    CodexSessionConfigurationError,
)
from sidekick_usages.providers.codex.session.models import (
    CodexSessionPreparationReport,
)

_FAILURE_MESSAGES = {
    CodexBrokerFailure.PLATFORM_UNSUPPORTED: (
        "Shared Codex sessions require Linux, WSL, or macOS."
    ),
    CodexBrokerFailure.INSTALLATION_UNSUPPORTED: (
        "The installed Codex CLI cannot manage the shared daemon."
    ),
    CodexBrokerFailure.VERSION_UNSUPPORTED: (
        "The shared Codex daemon version is not supported."
    ),
    CodexBrokerFailure.PROTOCOL_UNSUPPORTED: (
        "The shared Codex daemon lacks the required authentication protocol."
    ),
    CodexBrokerFailure.SESSION_CONFIGURATION_REQUIRED: (
        "The neutral Codex session requires operator preparation."
    ),
    CodexBrokerFailure.LIFECYCLE_FAILED: (
        "The official Codex daemon lifecycle command failed."
    ),
    CodexBrokerFailure.LIFECYCLE_MALFORMED: (
        "The official Codex daemon returned invalid lifecycle state."
    ),
    CodexBrokerFailure.DAEMON_UNMANAGED: (
        "The Codex control socket is not owned by the official daemon."
    ),
    CodexBrokerFailure.RUNTIME_UNSAFE: (
        "The local Codex daemon endpoint is not safe to use."
    ),
    CodexBrokerFailure.RUNTIME_CHANGED: (
        "The local Codex daemon changed during authentication."
    ),
    CodexBrokerFailure.CONNECTION_FAILED: (
        "The local Codex daemon connection failed."
    ),
    CodexBrokerFailure.PROTOCOL_FAILED: (
        "The shared Codex daemon returned an invalid protocol message."
    ),
    CodexBrokerFailure.PROJECTION_REJECTED: (
        "The shared Codex daemon rejected the selected account."
    ),
    CodexBrokerFailure.IDENTITY_MISMATCH: (
        "The selected Codex authority has conflicting account identities."
    ),
}
_APP_SERVER_FAILURES = {
    CodexAppServerFailure.EXECUTABLE_MISSING: (
        CodexBrokerFailure.INSTALLATION_UNSUPPORTED
    ),
    CodexAppServerFailure.EXECUTABLE_UNSAFE: (
        CodexBrokerFailure.INSTALLATION_UNSUPPORTED
    ),
    CodexAppServerFailure.VERSION_UNSUPPORTED: (
        CodexBrokerFailure.VERSION_UNSUPPORTED
    ),
    CodexAppServerFailure.CAPABILITY_UNSUPPORTED: (
        CodexBrokerFailure.PROTOCOL_UNSUPPORTED
    ),
    CodexAppServerFailure.PROCESS_FAILED: CodexBrokerFailure.LIFECYCLE_FAILED,
    CodexAppServerFailure.PROCESS_TIMEOUT: CodexBrokerFailure.LIFECYCLE_FAILED,
    CodexAppServerFailure.PROTOCOL_MALFORMED: (
        CodexBrokerFailure.PROTOCOL_FAILED
    ),
    CodexAppServerFailure.REQUEST_REJECTED: (
        CodexBrokerFailure.PROJECTION_REJECTED
    ),
    CodexAppServerFailure.PROTOCOL_TIMEOUT: (
        CodexBrokerFailure.PROTOCOL_FAILED
    ),
    CodexAppServerFailure.PROTOCOL_CLOSED: (
        CodexBrokerFailure.CONNECTION_FAILED
    ),
}


class CodexBrokerError(UsageError):
    """One shared-runtime failure containing no provider material."""

    def __init__(
        self,
        code: CodexBrokerFailure,
        preparation_report: CodexSessionPreparationReport | None = None,
    ) -> None:
        if (preparation_report is None) is (
            code is CodexBrokerFailure.SESSION_CONFIGURATION_REQUIRED
        ):
            raise ValueError("Codex preparation report and code disagree.")
        self.code = code
        self.preparation_report = preparation_report
        super().__init__(_FAILURE_MESSAGES[code])


def codex_broker_error(error: CodexAppServerError) -> CodexBrokerError:
    """Translate an app-server failure at the broker boundary."""
    return CodexBrokerError(_APP_SERVER_FAILURES[error.code])


def codex_session_configuration_error(
    error: CodexSessionConfigurationError,
) -> CodexBrokerError:
    """Translate one token-free session preparation refusal."""
    return CodexBrokerError(
        CodexBrokerFailure.SESSION_CONFIGURATION_REQUIRED,
        error.report,
    )
