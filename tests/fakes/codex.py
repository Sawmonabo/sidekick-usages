"""Managed-home and executable fakes for Codex boundary tests."""

import base64
import json
import os
import sys
import textwrap
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path

from sidekick_usages.core.accounts.models import (
    CodexAccountAuthority,
    CodexManagedAuthority,
    SavedAccount,
)
from sidekick_usages.core.accounts.types import (
    AuthorityGeneration,
    AuthorityId,
    CredentialHealth,
    ProviderIdentity,
    SidekickAccountId,
)
from sidekick_usages.core.types import AccountLabel, ProviderId
from sidekick_usages.credentials.codex.authorities import (
    CodexManagedAuthorityCoordinator,
)
from sidekick_usages.paths import ApplicationPaths, managed_codex_home
from sidekick_usages.persistence.accounts.store import AccountStore
from sidekick_usages.persistence.filesystem.service import (
    PersistenceFilesystem,
)
from sidekick_usages.persistence.locking import PersistenceLock
from sidekick_usages.persistence.models.account import VersionThreeDocument
from sidekick_usages.persistence.private.credentials import (
    PrivateCredentialTree,
)
from sidekick_usages.persistence.schema.account import encode_version_three
from sidekick_usages.persistence.types.artifact import AuthorityExpectation
from sidekick_usages.providers.codex.app_server.capabilities import (
    probe_codex_capabilities,
)
from sidekick_usages.providers.codex.app_server.executable import (
    discover_codex_executable,
)
from sidekick_usages.providers.codex.auth import (
    CODEX_AUTH_FILE,
    CODEX_CONFIG_FILE,
    CODEX_FILE_AUTH_CONFIG,
)
from sidekick_usages.serialization.json import JsonObject, JsonValue
from tests.test_support import (
    REFERENCE_TIME,
    FixedClock,
    make_application_paths,
)

RAW_PROVIDER_SECRET = "raw-provider-secret"
NEXT_AUTH_FILE = "next-auth.json"
LOGIN_CONFIG_FILE = "login-config.json"
MANAGED_FILE_CONFIG = f"{CODEX_FILE_AUTH_CONFIG}\n".encode()
_LOGIN_OUTCOMES = frozenset({"cancelled", "success"})


@dataclass(frozen=True, slots=True)
class FakeCodexLogin:
    """One official fake-login result for a private Codex home."""

    provider_identity: str
    login_generation: str
    refresh_generation: str
    outcome: str = "success"

    def __post_init__(self) -> None:
        """Reject unsupported fake outcomes."""
        if self.outcome not in _LOGIN_OUTCOMES:
            raise ValueError("Fake Codex login outcome is invalid.")


def codex_jwt(account_id: str, generation: str) -> str:
    """Build one deterministic JWT-shaped access credential."""

    def encode(value: Mapping[str, object]) -> str:
        payload = json.dumps(value, separators=(",", ":")).encode()
        return base64.urlsafe_b64encode(payload).decode().rstrip("=")

    claims = {
        "https://api.openai.com/auth": {
            "chatgpt_account_id": account_id,
            "chatgpt_plan_type": "pro",
            "generation": generation,
        }
    }
    return f"{encode({'alg': 'none'})}.{encode(claims)}.sig"


def managed_auth(provider_identity: str, generation: str) -> bytes:
    """Encode one synthetic official managed-home authority."""
    return json.dumps(
        {
            "auth_mode": "chatgpt",
            "last_refresh": generation,
            "tokens": {
                "access_token": codex_jwt(provider_identity, generation),
                "refresh_token": (
                    f"managed-refresh-{provider_identity}-{generation}"
                ),
                "id_token": f"managed-id-{provider_identity}-{generation}",
                "account_id": provider_identity,
            },
        }
    ).encode()


def managed_saved_account(
    account_id: SidekickAccountId,
    authority_id: AuthorityId,
    label: str,
    provider_identity: str,
    generation: str,
) -> SavedAccount:
    """Build one secret-free managed Codex account."""
    return SavedAccount(
        account_id=account_id,
        label=AccountLabel(label),
        provider_id=ProviderId.CODEX,
        plan="pro",
        authority=CodexAccountAuthority(
            subscription=CodexManagedAuthority(
                authority_id=authority_id,
                provider_identity=ProviderIdentity(provider_identity),
                generation=AuthorityGeneration(generation),
                verified_at=REFERENCE_TIME - timedelta(minutes=5),
                executable_version="0.145.0",
                health=CredentialHealth.HEALTHY,
            )
        ),
        credential_health=CredentialHealth.HEALTHY,
    )


def managed_subscription(account: SavedAccount) -> CodexManagedAuthority:
    """Return the managed subscription from a synthetic Codex account."""
    authority = account.authority
    assert isinstance(authority, CodexAccountAuthority)
    subscription = authority.subscription
    assert isinstance(subscription, CodexManagedAuthority)
    return subscription


def seed_managed_accounts(
    root: Path,
    accounts: tuple[SavedAccount, ...],
    next_authorities: Mapping[SidekickAccountId, bytes],
) -> tuple[ApplicationPaths, AccountStore, PrivateCredentialTree]:
    """Persist managed metadata and independent synthetic Codex homes."""
    paths = make_application_paths(root)
    filesystem = PersistenceFilesystem(paths.accounts)
    filesystem.repair_parent_permissions()
    with PersistenceLock(filesystem).hold() as transaction:
        transaction.commit_authority(
            encode_version_three(VersionThreeDocument(accounts)),
            AuthorityExpectation.ABSENT,
        )
    managed_tree = PrivateCredentialTree(
        paths.private_codex_profiles,
        account_path=paths.accounts,
    )
    for account in accounts:
        authority = managed_subscription(account)
        managed_tree.write_bundle(
            managed_codex_home(paths, account.account_id),
            {
                CODEX_AUTH_FILE: managed_auth(
                    str(authority.provider_identity),
                    str(authority.generation),
                ),
                CODEX_CONFIG_FILE: MANAGED_FILE_CONFIG,
                NEXT_AUTH_FILE: next_authorities[account.account_id],
            },
            expected_bundle_present=False,
            expected_files={},
        )
    credential_tree = PrivateCredentialTree(
        paths.private_credentials,
        account_path=paths.accounts,
    )
    return (
        paths,
        AccountStore(paths.accounts, credential_tree).load(),
        managed_tree,
    )


def managed_coordinator(
    root: Path,
    paths: ApplicationPaths,
    store: AccountStore,
    private: PrivateCredentialTree,
) -> CodexManagedAuthorityCoordinator:
    """Compose one managed coordinator around a release-matched fake."""
    schema_root = root / "schema"
    write_codex_schema(schema_root, external_auth=True)
    write_fake_codex(root, schema_root)
    environment = {
        "HOME": str(root),
        "PATH": os.pathsep.join((str(root), os.environ["PATH"])),
    }
    executable = discover_codex_executable(environment)
    capabilities = probe_codex_capabilities(executable, environment)
    return CodexManagedAuthorityCoordinator(
        paths,
        store,
        private,
        capabilities,
        FixedClock(),
        environment=environment,
    )


def managed_generation(
    private: PrivateCredentialTree,
    account_id: SidekickAccountId,
) -> str:
    """Read the current synthetic provider-owned generation."""
    snapshot = private.read_relative_bundle_file(
        str(account_id),
        CODEX_AUTH_FILE,
    )
    assert snapshot is not None
    generation = json.loads(snapshot.data)["last_refresh"]
    assert isinstance(generation, str)
    return generation


def configure_codex_logins(
    root: Path,
    logins: Mapping[Path, FakeCodexLogin],
) -> None:
    """Configure synthetic official login results by exact final home."""
    payload = {
        str(home): {
            "login_auth": managed_auth(
                login.provider_identity,
                login.login_generation,
            ).decode(),
            "outcome": login.outcome,
            "refresh_auth": managed_auth(
                login.provider_identity,
                login.refresh_generation,
            ).decode(),
        }
        for home, login in logins.items()
    }
    (root / LOGIN_CONFIG_FILE).write_text(
        json.dumps(payload),
        encoding="utf-8",
    )
    (root / "mode").write_text("normal", encoding="utf-8")


def write_codex_schema(root: Path, *, external_auth: bool) -> None:
    """Write the minimal release-shaped schema required by the probe."""
    login_variants: list[JsonValue] = [
        _variant("chatgpt"),
        _variant("chatgptDeviceCode"),
    ]
    login_response_variants: list[JsonValue] = [
        _variant("chatgpt", "authUrl", "loginId"),
        _variant(
            "chatgptDeviceCode",
            "loginId",
            "userCode",
            "verificationUrl",
        ),
    ]
    if external_auth:
        login_variants.append(
            _variant(
                "chatgptAuthTokens",
                "accessToken",
                "chatgptAccountId",
            )
        )
        login_response_variants.append(_variant("chatgptAuthTokens"))
    account_variants: list[JsonValue] = [
        _variant("chatgpt", "email", "planType")
    ]
    schemas_by_path: dict[str, JsonObject] = {
        "v1/InitializeParams.json": _object_schema(
            {"clientInfo": {"type": "object"}},
            required=("clientInfo",),
            definitions={
                "InitializeCapabilities": _object_schema(
                    {"experimentalApi": {"type": "boolean"}}
                )
            },
        ),
        "v1/InitializeResponse.json": _object_schema(
            {
                "codexHome": {"type": "string"},
                "platformFamily": {"type": "string"},
                "platformOs": {"type": "string"},
                "userAgent": {"type": "string"},
            },
            required=(
                "codexHome",
                "platformFamily",
                "platformOs",
                "userAgent",
            ),
        ),
        "v2/GetAccountParams.json": _object_schema(
            {"refreshToken": {"type": "boolean"}}
        ),
        "v2/GetAccountResponse.json": _object_schema(
            {
                "account": {"type": _json_values("object", "null")},
                "requiresOpenaiAuth": {"type": "boolean"},
            },
            required=("requiresOpenaiAuth",),
            definitions={"Account": {"oneOf": account_variants}},
        ),
        "v2/LoginAccountParams.json": {"oneOf": login_variants},
        "v2/LoginAccountResponse.json": {"oneOf": login_response_variants},
        "v2/AccountLoginCompletedNotification.json": _object_schema(
            {
                "error": {"type": _json_values("string", "null")},
                "loginId": {"type": _json_values("string", "null")},
                "success": {"type": "boolean"},
            },
            required=("success",),
        ),
        "v2/AccountUpdatedNotification.json": _object_schema(
            {
                "authMode": {
                    "type": _json_values("string", "null"),
                    "enum": _json_values(
                        "chatgpt",
                        "chatgptAuthTokens",
                        None,
                    ),
                },
                "planType": {"type": _json_values("string", "null")},
            }
        ),
        "ChatgptAuthTokensRefreshParams.json": _object_schema(
            {
                "previousAccountId": {"type": _json_values("string", "null")},
                "reason": {
                    "type": "string",
                    "enum": _json_values("unauthorized"),
                },
            },
            required=("reason",),
        ),
        "ChatgptAuthTokensRefreshResponse.json": _object_schema(
            {
                "accessToken": {"type": "string"},
                "chatgptAccountId": {"type": "string"},
                "chatgptPlanType": {"type": _json_values("string", "null")},
            },
            required=("accessToken", "chatgptAccountId"),
        ),
        "ClientRequest.json": _method_schema(
            "initialize",
            "account/login/start",
            "account/read",
        ),
        "ClientNotification.json": _method_schema("initialized"),
        "ServerRequest.json": _method_schema(
            "account/chatgptAuthTokens/refresh"
        ),
        "ServerNotification.json": _method_schema(
            "account/login/completed",
            "account/updated",
        ),
    }
    for relative, payload in schemas_by_path.items():
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(payload), encoding="utf-8")


def write_fake_codex(tmp_path: Path, schema_root: Path) -> Path:
    """Write one fake that supports version, schema, and stdio modes."""
    executable = tmp_path / "codex"
    mode_path = tmp_path / "mode"
    events_path = tmp_path / "events.jsonl"
    pid_path = tmp_path / "app-server.pid"
    executable.write_text(
        textwrap.dedent(
            f"""\
            #!{sys.executable}
            import json
            import os
            import shutil
            import signal
            import sys
            import time
            from pathlib import Path

            SCHEMA_ROOT = Path({json.dumps(str(schema_root))})
            MODE_PATH = Path({json.dumps(str(mode_path))})
            EVENTS_PATH = Path({json.dumps(str(events_path))})
            PID_PATH = Path({json.dumps(str(pid_path))})
            RAW_SECRET = {json.dumps(RAW_PROVIDER_SECRET)}
            NEXT_AUTH_FILE = {json.dumps(NEXT_AUTH_FILE)}
            LOGIN_CONFIG_FILE = Path(
                {json.dumps(str(tmp_path / LOGIN_CONFIG_FILE))}
            )

            def login_config():
                if not LOGIN_CONFIG_FILE.exists():
                    return None
                configured = json.loads(
                    LOGIN_CONFIG_FILE.read_text(encoding="utf-8")
                )
                return configured.get(os.environ["CODEX_HOME"])

            def write_auth(home, payload):
                auth_path = home / "auth.json"
                auth_path.write_text(payload, encoding="utf-8")
                os.chmod(auth_path, 0o600)

            event = {{
                "argv": sys.argv[1:],
                "codex_home": os.environ.get("CODEX_HOME"),
                "cwd": os.getcwd(),
                "openai_api_key": os.environ.get("OPENAI_API_KEY"),
            }}
            with EVENTS_PATH.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(event) + "\\n")

            if sys.argv[1:] == ["--version"]:
                print("codex-cli 0.145.0")
                raise SystemExit
            if sys.argv[1:4] == [
                "app-server",
                "generate-json-schema",
                "--experimental",
            ]:
                output = Path(sys.argv[5])
                shutil.copytree(SCHEMA_ROOT, output, dirs_exist_ok=True)
                raise SystemExit
            if sys.argv[1:] != ["app-server"]:
                raise SystemExit(2)

            PID_PATH.write_text(str(os.getpid()), encoding="utf-8")
            mode = MODE_PATH.read_text(encoding="utf-8").strip()
            for line in sys.stdin:
                request = json.loads(line)
                if request["method"] == "initialized":
                    continue
                request_id = request["id"]
                if mode == "malformed":
                    print(
                        '{{"id":1,"result":{{"secret":"'
                        + RAW_SECRET
                        + '"}},"result":{{}}}}',
                        flush=True,
                    )
                    continue
                if request["method"] == "initialize":
                    result = {{
                        "codexHome": os.environ["CODEX_HOME"],
                        "platformFamily": "unix",
                        "platformOs": "linux",
                        "userAgent": "fake-codex",
                    }}
                    print(
                        json.dumps({{"id": request_id, "result": result}}),
                        flush=True,
                    )
                elif request["method"] == "account/login/start":
                    with EVENTS_PATH.open("a", encoding="utf-8") as stream:
                        stream.write(
                            json.dumps(
                                {{
                                    "codex_home": os.environ["CODEX_HOME"],
                                    "cwd": os.getcwd(),
                                    "method": request["method"],
                                    "params": request["params"],
                                }}
                            )
                            + "\\n"
                        )
                    configured = login_config()
                    if configured is None:
                        print(
                            json.dumps(
                                {{
                                    "id": request_id,
                                    "error": {{
                                        "code": -32000,
                                        "message": RAW_SECRET,
                                    }},
                                }}
                            ),
                            flush=True,
                        )
                        continue
                    print(
                        json.dumps(
                            {{
                                "method": "configWarning",
                                "params": {{"message": "synthetic warning"}},
                            }}
                        ),
                        flush=True,
                    )
                    home = Path(os.environ["CODEX_HOME"])
                    login_id = "login-" + home.name
                    login_type = request["params"]["type"]
                    if login_type == "chatgptDeviceCode":
                        result = {{
                            "loginId": login_id,
                            "type": login_type,
                            "userCode": "SAFE-CODE",
                            "verificationUrl": (
                                "https://auth.openai.com/codex/device"
                            ),
                        }}
                    else:
                        result = {{
                            "authUrl": (
                                "https://auth.openai.com/oauth/authorize"
                                "?state=synthetic"
                            ),
                            "loginId": login_id,
                            "type": login_type,
                        }}
                    print(
                        json.dumps({{"id": request_id, "result": result}}),
                        flush=True,
                    )
                    succeeded = configured["outcome"] == "success"
                    if succeeded:
                        write_auth(home, configured["login_auth"])
                    print(
                        json.dumps(
                            {{
                                "method": "account/login/completed",
                                "params": {{
                                    "error": (
                                        None if succeeded else RAW_SECRET
                                    ),
                                    "loginId": login_id,
                                    "success": succeeded,
                                }},
                            }}
                        ),
                        flush=True,
                    )
                    if succeeded:
                        print(
                            json.dumps(
                                {{
                                    "method": "account/updated",
                                    "params": {{
                                        "authMode": "chatgpt",
                                        "planType": "pro",
                                    }},
                                }}
                            ),
                            flush=True,
                        )
                elif request["method"] == "account/read":
                    with EVENTS_PATH.open("a", encoding="utf-8") as stream:
                        stream.write(
                            json.dumps(
                                {{
                                    "codex_home": os.environ["CODEX_HOME"],
                                    "cwd": os.getcwd(),
                                    "method": request["method"],
                                    "params": request["params"],
                                }}
                            )
                            + "\\n"
                        )
                    if request["params"]["refreshToken"] is True:
                        home = Path(os.environ["CODEX_HOME"])
                        next_auth = home / NEXT_AUTH_FILE
                        if next_auth.exists():
                            os.replace(next_auth, home / "auth.json")
                        else:
                            configured = login_config()
                            if configured is not None:
                                write_auth(home, configured["refresh_auth"])
                    notification = {{
                        "method": "account/updated",
                        "params": {{
                            "authMode": "chatgpt",
                            "planType": "pro",
                        }},
                    }}
                    result = {{
                        "account": {{
                            "email": None,
                            "planType": "pro",
                            "type": "chatgpt",
                        }},
                        "requiresOpenaiAuth": True,
                    }}
                    print(json.dumps(notification), flush=True)
                    print(
                        json.dumps({{"id": request_id, "result": result}}),
                        flush=True,
                    )
            signal.signal(signal.SIGTERM, signal.SIG_IGN)
            while mode == "stubborn":
                time.sleep(1)
            """
        ),
        encoding="utf-8",
    )
    executable.chmod(0o700)
    mode_path.write_text("stubborn", encoding="utf-8")
    return executable


def _object_schema(
    properties: JsonObject,
    *,
    required: tuple[str, ...] = (),
    definitions: JsonObject | None = None,
) -> JsonObject:
    schema: JsonObject = {"type": "object", "properties": properties}
    if required:
        schema["required"] = _json_values(*required)
    if definitions is not None:
        schema["definitions"] = definitions
    return schema


def _type_property(value: str) -> JsonObject:
    return {"type": "string", "enum": _json_values(value)}


def _variant(value: str, *required: str) -> JsonObject:
    properties: JsonObject = {"type": _type_property(value)}
    for name in required:
        properties[name] = {"type": "string"}
    return _object_schema(
        properties,
        required=("type", *required),
    )


def _method_schema(*methods: str) -> JsonObject:
    variants: list[JsonValue] = [
        _object_schema(
            {"method": _type_property(method)},
            required=("method",),
        )
        for method in methods
    ]
    return {"oneOf": variants}


def _json_values(*values: JsonValue) -> list[JsonValue]:
    return list(values)
