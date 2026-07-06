"""Optional API-key authentication.

When ``API_AUTH_TOKEN`` is configured, every request must present the token via
an ``Authorization: Bearer <token>`` or ``X-API-Key: <token>`` header. When it
is unset (the default), authentication is a no-op — preserving the trusted-LAN
behavior the project was designed around. Enable it before exposing the API to
anything beyond a fully trusted network.
"""
from __future__ import annotations

import secrets
from typing import Optional

from fastapi import Header, HTTPException

from backend.config.settings import get_settings


def _extract_presented_token(
    authorization: Optional[str], x_api_key: Optional[str]
) -> Optional[str]:
    if x_api_key:
        return x_api_key.strip()
    if authorization:
        value = authorization.strip()
        if value.lower().startswith("bearer "):
            return value[7:].strip()
        return value
    return None


def require_api_auth(
    authorization: Optional[str] = Header(default=None),
    x_api_key: Optional[str] = Header(default=None, alias="X-API-Key"),
) -> None:
    """FastAPI dependency enforcing the optional API token.

    No-op when no token is configured. Uses a constant-time comparison to avoid
    leaking the token via timing.
    """
    expected = get_settings().api_auth_token
    if not expected:
        return

    presented = _extract_presented_token(authorization, x_api_key)
    if not presented or not secrets.compare_digest(presented, expected):
        raise HTTPException(
            status_code=401,
            detail="Invalid or missing API token",
            headers={"WWW-Authenticate": "Bearer"},
        )
