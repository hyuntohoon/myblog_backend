"""Signed-token boundary and narrow Chat transport regression tests."""
import time
from types import SimpleNamespace
from unittest.mock import MagicMock
from uuid import uuid4

import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import HTTPException
from fastapi.testclient import TestClient
from jose import jwt
from starlette.requests import Request

from app.core import auth, mcp_auth
from app.api.routes import chat_mcp
from app.db.session import get_db
from app.services.chat_translation_service import validate_segments


@pytest.fixture
def configured(monkeypatch):
    cfg = SimpleNamespace(CHAT_MCP_RESOURCE_URL="https://example.com/mcp", CHAT_MCP_CLIENT_ID="chat-client",
        CHAT_MCP_COGNITO_DOMAIN="https://login.example.com", CHAT_MCP_SCOPE="myblog-chat/translate",
        COGNITO_REGION="ap-northeast-2", COGNITO_USER_POOL_ID="pool", OWNER_SUB="owner")
    monkeypatch.setattr(mcp_auth, "settings", cfg)
    monkeypatch.setattr(chat_mcp, "settings", cfg)
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public = key.public_key().public_numbers()
    import base64
    def encoded(n):
        return base64.urlsafe_b64encode(n.to_bytes((n.bit_length() + 7)//8, "big")).rstrip(b"=").decode()
    monkeypatch.setattr(auth, "_get_jwks", lambda: {"keys": [{"kid": "test", "kty": "RSA", "n": encoded(public.n), "e": encoded(public.e)}]})
    def token(**overrides):
        claims = {"iss": "https://cognito-idp.ap-northeast-2.amazonaws.com/pool", "sub": "owner",
            "aud": "https://example.com/mcp", "client_id": "chat-client", "token_use": "access",
            "scope": "myblog-chat/translate", "exp": int(time.time()) + 300}
        claims.update(overrides)
        return jwt.encode(claims, key, algorithm="RS256", headers={"kid": "test"})
    return cfg, token


def request(token=None):
    headers = [(b"authorization", f"Bearer {token}".encode())] if token else []
    return Request({"type": "http", "headers": headers})


def test_signed_owner_access_token(configured):
    _, token = configured
    assert mcp_auth.require_chat_owner(request(token()))["sub"] == "owner"


@pytest.mark.parametrize("overrides", [{"aud": "other"}, {"client_id": "spa"}, {"token_use": "id"},
    {"scope": "openid"}, {"scope": None}, {"scope": ["myblog-chat/translate"]},
    {"exp": 1}, {"iss": "https://attacker.example"}, {"exp": None}, {"aud": None}])
def test_wrong_token_context_rejected(configured, overrides):
    _, token = configured
    with pytest.raises(HTTPException) as error:
        mcp_auth.require_chat_owner(request(token(**overrides)))
    assert error.value.status_code == 401
    assert "resource_metadata" in error.value.headers["WWW-Authenticate"]


def test_member_and_missing_config_never_bypass(configured):
    cfg, token = configured
    with pytest.raises(HTTPException) as error:
        mcp_auth.require_chat_owner(request(token(sub="member")))
    assert error.value.status_code == 403
    cfg.CHAT_MCP_CLIENT_ID = ""
    with pytest.raises(HTTPException) as error:
        mcp_auth.require_chat_owner(request())
    assert error.value.status_code == 503


def test_missing_token_challenges_before_tools(configured):
    with pytest.raises(HTTPException) as error:
        mcp_auth.require_chat_owner(request())
    assert error.value.status_code == 401


@pytest.fixture
def rpc_client(app, configured):
    db = MagicMock()
    app.dependency_overrides[mcp_auth.require_chat_owner] = lambda: {"sub": "owner"}
    app.dependency_overrides[get_db] = lambda: db
    with TestClient(app) as client:
        yield client, db


def rpc(client, method, params=None, **kwargs):
    return client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}}, **kwargs)


def test_discovery_and_tool_surface(rpc_client):
    client, db = rpc_client
    init = rpc(client, "initialize", {"protocolVersion": "2025-06-18"}).json()["result"]
    assert init["protocolVersion"] == "2025-06-18"
    listed = rpc(client, "tools/list").json()["result"]["tools"]
    assert {tool["name"] for tool in listed} == {"prepare_translation", "submit_translation", "stop_translation"}
    assert all(tool["inputSchema"]["additionalProperties"] is False for tool in listed)
    assert "queue" not in {tool["name"] for tool in listed}
    assert client.get("/mcp").status_code == 405
    assert client.get("/.well-known/oauth-protected-resource/mcp").json()["resource"] == "https://example.com/mcp"
    metadata = client.get("/.well-known/oauth-authorization-server/mcp-auth").json()
    assert metadata["issuer"] == "https://example.com/mcp-auth"
    assert metadata["authorization_endpoint"] == "https://login.example.com/oauth2/authorize"
    assert metadata["code_challenge_methods_supported"] == ["S256"]
    db.execute.assert_not_called()


@pytest.mark.parametrize("arguments", [{"spotify_track_id": "x", "queue": "all"},
    {"spotify_track_id": "a"*22, "kind": "genius"},
    {"spotify_track_id": "a"*22, "annotation_id": 2}])
def test_invalid_or_broad_requests_never_touch_db(rpc_client, arguments):
    client, db = rpc_client
    result = rpc(client, "tools/call", {"name": "prepare_translation", "arguments": arguments}).json()
    assert result["error"]["code"] == -32602
    db.execute.assert_not_called()


def test_transport_limits_and_untrusted_origin(rpc_client):
    client, _ = rpc_client
    assert rpc(client, "ping", headers={"Origin": "https://attacker.example"}).status_code == 403
    assert client.post("/mcp", content="x", headers={"Content-Type": "text/plain"}).status_code == 415
    assert client.post("/mcp", content=" "*262145, headers={"Content-Type": "application/json"}).status_code == 413
    assert rpc(client, "ping", headers={"MCP-Protocol-Version": "unknown"}).status_code == 400


def test_tool_failures_rollback_without_automatic_retry(rpc_client, monkeypatch):
    client, db = rpc_client
    def fail(*args, **kwargs):
        raise HTTPException(409, "Source changed")
    monkeypatch.setattr(chat_mcp.ChatTranslationService, "submit", fail)
    output = rpc(client, "tools/call", {"name": "submit_translation", "arguments": {
        "work_id": str(uuid4()), "claim_token": str(uuid4()), "segments": [{"i": 0, "text_ko": "번역"}]}}).json()["result"]
    assert output["isError"] is True
    assert '"automatic_retry": false' in output["content"][0]["text"]
    db.rollback.assert_called_once()
    db.commit.assert_not_called()


@pytest.mark.parametrize("segments", [[{"i": 0, "text_ko": "번역"}],
    [{"i": 1, "text_ko": "번역"}, {"i": 0, "text_ko": ""}],
    [{"i": 0, "text_ko": " "}, {"i": 1, "text_ko": ""}],
    [{"i": 0, "text_ko": "번역"}, {"i": 1, "text_ko": "간격"}]])
def test_missing_reordered_empty_or_filled_gap_rejected(segments):
    with pytest.raises(HTTPException) as error:
        validate_segments([{"i": 0, "text": "line"}, {"i": 1, "text": ""}], segments)
    assert error.value.status_code == 422
