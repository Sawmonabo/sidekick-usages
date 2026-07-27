"""Confined application paths for one synthetic cached-dashboard trace."""

from pathlib import Path

from sidekick_usages.paths import ApplicationPaths


def benchmark_application_paths(root: Path) -> ApplicationPaths:
    """Return a complete Sidekick path set confined below ``root``."""
    if not root.is_absolute():
        raise ValueError("Dashboard benchmark root must be absolute.")
    data = root / "data"
    credentials = data / "credentials"
    runtime = root / "runtime"
    return ApplicationPaths(
        accounts=data / "accounts.json",
        private_credentials=credentials,
        private_codex_profiles=credentials / "codex",
        activity_snapshots=data / "token-activity.json",
        usage_snapshots=data / "usage-metrics.json",
        metrics_refresh_status=data / "metrics-refresh.json",
        credential_refresh=data / "credential-refresh",
        private_claude_profiles=credentials / "claude",
        selected_state=data / "selected-accounts.json",
        activation_journals=data / "activation-journals",
        durable_operations=data / "operations",
        service_state=data / "service-state.json",
        service_setup_acknowledgement=(
            data / "service-setup-acknowledgement.json"
        ),
        service_logs=root / "logs",
        runtime_directory=runtime,
        supervisor_socket=runtime / "supervisor.sock",
        systemd_user_service=root / "service" / "sidekick-usages.service",
        launch_agent=root / "service" / "sidekick-usages.plist",
    )
