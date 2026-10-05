"""Owner-only, resource-bound OAuth for the Chat translation connection.

The OAuth metadata adapter delegates PKCE to Cognito. Its issuer is distinct
from the JWT issuer, which remains our actual Cognito pool. The existing SPA
verifier and its accepted clients remain untouched.
"""
from urllib.parse import urlsplit

import httpx
from fastapi import HTTPException, Request
from jose import JWTError, jwt

from app.core import auth
from app.core.config import settings


def connection_config() -> tuple[str, str]:
    resource = settings.CHAT_MCP_RESOURCE_URL.rstrip("/")
    domain = settings.CHAT_MCP_COGNITO_DOMAIN.rstrip("/")
    for url in (resource, domain):
        parsed = urlsplit(url)
        if parsed.scheme != "https" or not parsed.hostname or parsed.query or parsed.fragment or parsed.username:
            raise HTTPException(503, "Chat connection not configured")
    if urlsplit(resource).path != "/mcp" or urlsplit(domain).path:
        raise HTTPException(503, "Chat connection not configured")
    return resource, domain


def metadata_urls() -> tuple[str, str]:
    resource, _ = connection_config()
    origin = resource.removesuffix("/mcp")
    return origin + "/.well-known/oauth-protected-resource/mcp", origin + "/mcp-auth"


def require_chat_owner(request: Request) -> dict:
    resource, _ = connection_config()
    metadata, _ = metadata_urls()
    challenge = {"WWW-Authenticate": f'Bearer resource_metadata="{metadata}", scope="{settings.CHAT_MCP_SCOPE}"'}
    if not settings.CHAT_MCP_CLIENT_ID or not settings.COGNITO_USER_POOL_ID or not settings.OWNER_SUB:
        raise HTTPException(503, "Chat connection not configured")
    header = request.headers.get("authorization", "")
    if not header.startswith("Bearer "):
        raise HTTPException(401, "Connect your MyBlog account", headers=challenge)
    try:
        token = header[7:]
        kid = jwt.get_unverified_header(token).get("kid")
        keys = auth._get_jwks()
        key = next((key for key in keys["keys"] if key.get("kid") == kid), None)
        if key is None:
            auth._refresh_jwks_if_due()
            raise JWTError("Unknown key")
        issuer = f"https://cognito-idp.{settings.COGNITO_REGION}.amazonaws.com/{settings.COGNITO_USER_POOL_ID}"
        claims = jwt.decode(token, key, algorithms=["RS256"], issuer=issuer, audience=resource,
                            options={"require_exp": True, "require_sub": True, "require_aud": True})
        if claims.get("token_use") != "access" or claims.get("client_id") != settings.CHAT_MCP_CLIENT_ID:
            raise JWTError("Invalid token type or client")
        scope = claims.get("scope")
        if not isinstance(scope, str) or settings.CHAT_MCP_SCOPE not in scope.split():
            raise JWTError("Missing scope")
    except (JWTError, ValueError, KeyError, TypeError):
        raise HTTPException(401, "Invalid Chat authorization", headers=challenge) from None
    except httpx.HTTPError:
        raise HTTPException(503, "Auth provider unavailable") from None
    if claims["sub"] != settings.OWNER_SUB:
        raise HTTPException(403, "Owner only")
    return claims
