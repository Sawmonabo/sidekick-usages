"""Codex-specific heartbeat protocol and command tests."""

from pathlib import Path

from sidekick_usages.core.types import HeartbeatStatus, ProviderId
from sidekick_usages.providers.codex.heartbeat import SPARK_HEARTBEAT_MODEL
from tests.fakes.heartbeat import (
    CODEX_USAGE_FETCHES_FOR_WARM,
    SPARK_RESET,
    STANDARD_RESET,
    FakeCodexHttp,
    codex_heartbeat,
    heartbeat_account,
    install_heartbeat_context,
)
from tests.support.accounts import authenticated_account


def test_heartbeat_enable_accepts_codex_with_saved_account_id(
    tmp_path: Path,
) -> None:
    """Codex accounts with saved account ids can opt into heartbeat."""
    harness, store, stdout, _ = install_heartbeat_context(
        tmp_path,
        [
            heartbeat_account(
                provider_id=ProviderId.CODEX,
                provider_account_id="acct-codex",
            )
        ],
        {ProviderId.CODEX: codex_heartbeat()},
    )

    result = harness.invoke(["heartbeat", "enable", "team"])

    assert result.exit_code == 0
    saved = store.get("team")
    assert saved is not None
    assert saved.heartbeat_enabled is True
    assert "team: enabled" in stdout.getvalue()


def test_codex_heartbeat_warms_standard_window_with_mini() -> None:
    """Codex standard heartbeat uses the cheapest standard-window model."""
    account = heartbeat_account(
        provider_id=ProviderId.CODEX,
        provider_account_id="acct-codex",
    )
    http = FakeCodexHttp(
        [
            {"rate_limit": {"primary_window": {"used_percent": 0}}},
            {
                "rate_limit": {
                    "primary_window": {
                        "used_percent": 1,
                        "resets_at": "2026-06-12T18:00:00Z",
                    }
                }
            },
        ]
    )

    result = codex_heartbeat().run(authenticated_account(account), http)

    assert result.status is HeartbeatStatus.WARMED
    assert result.reset_at == STANDARD_RESET
    assert len(http.get_calls) == CODEX_USAGE_FETCHES_FOR_WARM
    assert len(http.post_calls) == 1
    url, body, headers = http.post_calls[0]
    assert url == "https://chatgpt.com/backend-api/codex/responses"
    assert body["model"] == "gpt-5.4-mini"
    assert body["model"] != SPARK_HEARTBEAT_MODEL
    assert body["instructions"] == "Reply with exactly: ok"
    assert body["stream"] is True
    assert body["store"] is False
    assert body["reasoning"] == {"effort": "low"}
    assert body["input"] == [
        {
            "type": "message",
            "role": "user",
            "content": [{"type": "input_text", "text": "ok"}],
        }
    ]
    assert headers["Authorization"] == "Bearer old-token"
    assert headers["ChatGPT-Account-Id"] == "acct-codex"
    assert headers["Accept"] == "text/event-stream"


def test_codex_heartbeat_warms_spark_window_with_spark_model() -> None:
    """Codex Spark heartbeat targets the separate Spark rate limit."""
    account = heartbeat_account(
        provider_id=ProviderId.CODEX,
        provider_account_id="acct-codex",
    )
    http = FakeCodexHttp(
        [
            {
                "rate_limit": {
                    "primary_window": {
                        "used_percent": 1,
                        "resets_at": "2026-06-12T18:00:00Z",
                    }
                },
                "additional_rate_limits": [
                    {
                        "limit_name": "GPT-5.3-Codex-Spark",
                        "rate_limit": {
                            "primary_window": {"used_percent": 0},
                        },
                    }
                ],
            },
            {
                "rate_limit": {
                    "primary_window": {
                        "used_percent": 1,
                        "resets_at": "2026-06-12T18:00:00Z",
                    }
                },
                "additional_rate_limits": [
                    {
                        "limit_name": "GPT-5.3-Codex-Spark",
                        "rate_limit": {
                            "primary_window": {
                                "used_percent": 1,
                                "resets_at": "2026-06-12T19:00:00Z",
                            },
                        },
                    }
                ],
            },
        ]
    )

    result = codex_heartbeat().run(
        authenticated_account(account),
        http,
        target_id="spark",
    )

    assert result.status is HeartbeatStatus.WARMED
    assert result.reset_at == SPARK_RESET
    assert result.target_id == "spark"
    assert len(http.post_calls) == 1
    _, body, _ = http.post_calls[0]
    assert body["model"] == SPARK_HEARTBEAT_MODEL


def test_codex_heartbeat_fails_when_target_window_stays_inactive() -> None:
    """A successful POST is not reported as warmed unless usage confirms it."""
    account = heartbeat_account(
        provider_id=ProviderId.CODEX,
        provider_account_id="acct-codex",
    )
    http = FakeCodexHttp(
        [
            {"rate_limit": {"primary_window": {"used_percent": 0}}},
            {"rate_limit": {"primary_window": {"used_percent": 1}}},
        ]
    )

    result = codex_heartbeat().run(authenticated_account(account), http)

    assert result.status is HeartbeatStatus.FAILED
    assert result.warmed is False
    assert "did not become active" in result.message


def test_codex_heartbeat_can_enable_all_targets(tmp_path: Path) -> None:
    """Codex opt-in can include standard and Spark windows."""
    harness, store, stdout, _ = install_heartbeat_context(
        tmp_path,
        [
            heartbeat_account(
                provider_id=ProviderId.CODEX,
                provider_account_id="acct-codex",
            )
        ],
        {ProviderId.CODEX: codex_heartbeat()},
    )

    result = harness.invoke(
        ["heartbeat", "enable", "team", "--target", "all"],
    )

    assert result.exit_code == 0
    saved = store.get("team")
    assert saved is not None
    assert saved.heartbeat_enabled is True
    assert saved.heartbeat_targets == ("standard", "spark")
    assert "team: enabled" in stdout.getvalue()


def test_codex_heartbeat_skips_when_usage_window_is_active() -> None:
    """Codex usage state is inspected before sending a model request."""
    account = heartbeat_account(
        provider_id=ProviderId.CODEX,
        provider_account_id="acct-codex",
    )
    http = FakeCodexHttp(
        [
            {
                "rate_limit": {
                    "primary_window": {
                        "used_percent": 1,
                        "resets_at": "2026-06-12T18:00:00Z",
                    }
                }
            }
        ]
    )

    result = codex_heartbeat().run(authenticated_account(account), http)

    assert result.status is HeartbeatStatus.ACTIVE
    assert result.reset_at == STANDARD_RESET
    assert http.post_calls == []
