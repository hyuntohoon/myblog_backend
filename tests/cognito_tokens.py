"""Real RS256 token vectors for the Cognito guard.

Every auth test in this repo before 2026-08-26 did one of three things: passed
``credentials=None``, passed a literal non-JWT string like ``"x.y.z"``, or
monkeypatched ``verify_token`` / ``_get_jwks`` away entirely. None of them ever
constructed a signed JWT, so signature verification, the issuer check, the
``token_use`` check, expiry, ``nbf``, the RS256 allowlist and the app-client
binding were all unverified by automation — the whole cryptographic surface.

This module mints real tokens against a throwaway 2048-bit RSA keypair generated
once per process, and serves a matching JWKS. It is deliberately a plain module
rather than fixtures in ``conftest.py`` so that ``myblog_music`` can carry a
byte-identical copy; the two auth guards are duplicated on purpose (CLAUDE.md,
"fix both copies in the same change") and their test vectors must be too, or the
copies drift in exactly the place the duplication exists to protect.

Nothing here touches the network or the real pool.
"""

from __future__ import annotations

import json
import time
from base64 import urlsafe_b64encode
from functools import lru_cache
from typing import Any, Iterable

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from jose import jwt

KID = "test-kid-1"
REGION = "ap-northeast-2"
POOL_ID = "ap-northeast-2_testpool"
ISSUER = f"https://cognito-idp.{REGION}.amazonaws.com/{POOL_ID}"

# Stands in for the SPA app client. The guard's allowlist is set to this in tests.
CLIENT_ID = "test-spa-client"
OTHER_CLIENT_ID = "test-other-client"

_SENTINEL = object()


@lru_cache(maxsize=2)
def _key(tag: str = "primary") -> rsa.RSAPrivateKey:
    """A throwaway keypair. ``tag="attacker"`` gives a second, unrelated one."""
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def _pem(tag: str = "primary") -> str:
    return (
        _key(tag)
        .private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
        .decode()
    )


def _b64u(raw: bytes) -> str:
    return urlsafe_b64encode(raw).rstrip(b"=").decode()


def _b64u_uint(value: int) -> str:
    return _b64u(value.to_bytes((value.bit_length() + 7) // 8, "big"))


def jwks(*, kid: str = KID, tag: str = "primary") -> dict[str, Any]:
    """A JWKS document containing exactly the public half of ``tag``'s keypair."""
    pub = _key(tag).public_key().public_numbers()
    return {
        "keys": [
            {
                "kty": "RSA",
                "kid": kid,
                "use": "sig",
                "alg": "RS256",
                "n": _b64u_uint(pub.n),
                "e": _b64u_uint(pub.e),
            }
        ]
    }


def mint(
    *,
    token_use: Any = "access",
    client_id: Any = _SENTINEL,
    sub: str = "user-sub-1",
    issuer: Any = _SENTINEL,
    expires_in: int | None = 3600,
    not_before: int | None = None,
    issued_at: int | None = 0,
    kid: str | None = KID,
    algorithm: str = "RS256",
    signing_tag: str = "primary",
    extra: dict[str, Any] | None = None,
    omit: Iterable[str] = (),
) -> str:
    """Mint a signed token.

    Offsets (``expires_in``, ``not_before``, ``issued_at``) are seconds relative
    to now; pass ``None`` to leave the claim out entirely. ``omit`` drops claims
    after assembly, which is how a token with no ``iss`` or no ``token_use`` is
    built. ``signing_tag="attacker"`` signs with a key the JWKS does not contain.
    """
    now = int(time.time())
    claims: dict[str, Any] = {"sub": sub}

    if token_use is not None:
        claims["token_use"] = token_use
    claims["client_id"] = CLIENT_ID if client_id is _SENTINEL else client_id
    claims["iss"] = ISSUER if issuer is _SENTINEL else issuer
    if expires_in is not None:
        claims["exp"] = now + expires_in
    if issued_at is not None:
        claims["iat"] = now + issued_at
    if not_before is not None:
        claims["nbf"] = now + not_before
    if extra:
        claims.update(extra)
    for key in omit:
        claims.pop(key, None)

    headers = {"kid": kid} if kid is not None else {}
    return jwt.encode(claims, _pem(signing_tag), algorithm=algorithm, headers=headers)


def mint_unsigned(*, kid: str = KID, **claim_overrides: Any) -> str:
    """An ``alg: none`` token — a valid-looking JWT with an empty signature.

    Built by hand: python-jose refuses to *sign* with ``none``, but a verifier
    that does not pin its algorithm list will happily accept one.
    """
    now = int(time.time())
    claims: dict[str, Any] = {
        "sub": "user-sub-1",
        "token_use": "access",
        "client_id": CLIENT_ID,
        "iss": ISSUER,
        "exp": now + 3600,
        "iat": now,
    }
    claims.update(claim_overrides)
    header = _b64u(json.dumps({"alg": "none", "typ": "JWT", "kid": kid}).encode())
    payload = _b64u(json.dumps(claims).encode())
    return f"{header}.{payload}."


def mint_hs256_with_modulus(*, kid: str = KID) -> str:
    """The classic algorithm-confusion token.

    Signed with HMAC-SHA256 using the RSA public modulus as the shared secret. A
    verifier that reads ``alg`` from the token instead of pinning it treats the
    public key as an HMAC key and accepts this.
    """
    now = int(time.time())
    pub = _key().public_key().public_numbers()
    secret = _b64u_uint(pub.n)
    claims = {
        "sub": "user-sub-1",
        "token_use": "access",
        "client_id": CLIENT_ID,
        "iss": ISSUER,
        "exp": now + 3600,
        "iat": now,
    }
    return jwt.encode(claims, secret, algorithm="HS256", headers={"kid": kid})
