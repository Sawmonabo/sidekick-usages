"""Credential-free subprocess environment allowlists."""

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
        "LOGNAME",
        "PATH",
        "TMPDIR",
        "USER",
    }
)
SAFE_CODEX_ENVIRONMENT_KEYS = (
    SAFE_NETWORK_ENVIRONMENT_KEYS | SAFE_PROCESS_ENVIRONMENT_KEYS
)
SAFE_WORKER_ENVIRONMENT_KEYS = SAFE_CODEX_ENVIRONMENT_KEYS | frozenset(
    {
        "XDG_CONFIG_HOME",
        "XDG_DATA_HOME",
        "XDG_RUNTIME_DIR",
    }
)
