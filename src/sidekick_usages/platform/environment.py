"""Credential-free subprocess environment allowlists."""

from collections.abc import Mapping

from sidekick_usages.platform.types import WorkerEnvironment

MAX_WORKER_ENVIRONMENT_VALUE_BYTES = 16 * 1024

SAFE_NETWORK_ENVIRONMENT_KEYS = frozenset(
    {
        "ALL_PROXY",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "NO_PROXY",
        "SSL_CERT_DIR",
        "SSL_CERT_FILE",
        "all_proxy",
        "http_proxy",
        "https_proxy",
        "no_proxy",
    }
)
SAFE_PROCESS_ENVIRONMENT_KEYS = frozenset(
    {
        "HOME",
        "LANG",
        "LC_ALL",
        "LC_CTYPE",
        "LOGNAME",
        "PATH",
        "TMPDIR",
        "USER",
    }
)
SAFE_PROVIDER_ENVIRONMENT_KEYS = (
    SAFE_NETWORK_ENVIRONMENT_KEYS | SAFE_PROCESS_ENVIRONMENT_KEYS
)
SAFE_WORKER_ENVIRONMENT_KEYS = SAFE_PROVIDER_ENVIRONMENT_KEYS | frozenset(
    {
        "XDG_CONFIG_HOME",
        "XDG_DATA_HOME",
        "XDG_RUNTIME_DIR",
    }
)


def minimal_worker_environment(
    source: Mapping[str, str],
) -> WorkerEnvironment:
    """Return one sorted credential-free isolated-worker environment."""
    environment = tuple(
        sorted(
            (key, value)
            for key, value in source.items()
            if key in SAFE_WORKER_ENVIRONMENT_KEYS
        )
    )
    require_worker_environment(environment)
    return environment


def require_worker_environment(
    environment: WorkerEnvironment,
) -> None:
    """Validate one exact credential-free isolated-worker environment."""
    keys = tuple(key for key, _value in environment)
    if (
        len(keys) != len(set(keys))
        or tuple(sorted(keys)) != keys
        or not set(keys) <= SAFE_WORKER_ENVIRONMENT_KEYS
    ):
        raise ValueError("Worker environment is not a minimal allowlist.")
    for _key, value in environment:
        try:
            encoded = value.encode("utf-8")
        except AttributeError, UnicodeEncodeError:
            raise ValueError(
                "Worker environment must be valid UTF-8."
            ) from None
        if (
            len(encoded) > MAX_WORKER_ENVIRONMENT_VALUE_BYTES
            or "\x00" in value
        ):
            raise ValueError("Worker environment value is unsafe.")
