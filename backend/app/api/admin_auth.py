"""Authentication for administrative API routes."""

import hmac
import os

from fastapi import Header, HTTPException, Request

MIN_ADMIN_API_KEY_LENGTH = 32


def configured_admin_api_key() -> str | None:
    value = os.getenv("ADMIN_API_KEY")
    return value if value else None


def validate_admin_configuration() -> None:
    key = configured_admin_api_key()
    if key is not None:
        if len(key) < MIN_ADMIN_API_KEY_LENGTH:
            raise RuntimeError(
                f"ADMIN_API_KEY must contain at least {MIN_ADMIN_API_KEY_LENGTH} characters."
            )
        if any(character.isspace() for character in key):
            raise RuntimeError("ADMIN_API_KEY must not contain whitespace.")


def require_admin(
    request: Request,
    authorization: str | None = Header(default=None),
) -> None:
    expected = configured_admin_api_key()
    if expected is None:
        raise HTTPException(
            status_code=503,
            detail="Administrative rule management is not configured.",
        )
    if len(expected) < MIN_ADMIN_API_KEY_LENGTH:
        raise HTTPException(
            status_code=503,
            detail="Administrative rule management is not configured securely.",
        )

    if os.getenv("APP_ENV", "development").casefold() in {"prod", "production"}:
        if request.url.scheme != "https":
            raise HTTPException(
                status_code=400,
                detail="Administrative requests require HTTPS in production.",
            )

    scheme, separator, supplied = (authorization or "").partition(" ")
    if (
        not separator
        or scheme.casefold() != "bearer"
        or not hmac.compare_digest(supplied.encode("utf-8"), expected.encode("utf-8"))
    ):
        raise HTTPException(
            status_code=401,
            detail="A valid administrative bearer token is required.",
            headers={"WWW-Authenticate": "Bearer"},
        )
