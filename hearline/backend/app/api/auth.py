"""Login: verify the Clerk session token sent by the frontend (Authorization: Bearer <jwt>).

Clerk signs short-lived RS256 tokens; the public keys come from the instance's JWKS endpoint
({CLERK_ISSUER}/.well-known/jwks.json) and are cached. A valid token's `sub` is the Clerk
user id, stored as User.external_id. No Clerk SDK or secret key is needed for this.

AUTH_MODE=dev (default) skips this and uses the X-User-Id header: local development only.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Any

import jwt

from app.config import get_settings


class AuthError(Exception):
    pass


@lru_cache
def _jwks_client(issuer: str) -> jwt.PyJWKClient:
    return jwt.PyJWKClient(f"{issuer.rstrip('/')}/.well-known/jwks.json", cache_keys=True)


def signing_key(token: str, issuer: str) -> Any:
    return _jwks_client(issuer).get_signing_key_from_jwt(token).key


def verify_token(token: str) -> str:
    """The Clerk user id (`sub`) of a valid session token; raises AuthError otherwise."""
    return str(verify_claims(token)["sub"])


def verify_claims(token: str) -> dict[str, Any]:
    """The claims of a valid Clerk session token; raises AuthError otherwise. Besides `sub`,
    Clerk adds `name` / `email` when the instance's session token is customized with
    {"name": "{{user.full_name}}", "email": "{{user.primary_email_address}}"}."""
    s = get_settings()
    if not s.clerk_issuer:
        raise AuthError("AUTH_MODE=clerk needs CLERK_ISSUER")
    try:
        claims = jwt.decode(token, signing_key(token, s.clerk_issuer), algorithms=["RS256"],
                            issuer=s.clerk_issuer.rstrip("/"), leeway=10,
                            options={"require": ["exp", "iat", "sub", "iss"]})
    except jwt.PyJWTError as exc:
        raise AuthError(str(exc)) from exc
    # azp = the site that asked for the token; only our own frontend origins are accepted
    if s.clerk_authorized_parties and claims.get("azp") not in s.clerk_authorized_parties:
        raise AuthError("token issued for another site")
    return claims
