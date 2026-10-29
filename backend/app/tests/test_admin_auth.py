import pytest
from fastapi import HTTPException
from starlette.requests import Request

from app.api.admin_auth import require_admin, validate_admin_configuration


def _request(scheme="http"):
    return Request(
        {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": "POST",
            "scheme": scheme,
            "path": "/api/admin/business-rules/drafts",
            "raw_path": b"/api/admin/business-rules/drafts",
            "query_string": b"",
            "headers": [],
            "server": ("testserver", 443 if scheme == "https" else 80),
            "client": ("127.0.0.1", 1234),
            "root_path": "",
        }
    )


def test_admin_api_key_must_be_long_enough(monkeypatch):
    monkeypatch.setenv("ADMIN_API_KEY", "short")

    with pytest.raises(RuntimeError, match="at least 32 characters"):
        validate_admin_configuration()


def test_admin_auth_fails_closed_when_key_is_not_configured(monkeypatch):
    monkeypatch.delenv("ADMIN_API_KEY", raising=False)

    with pytest.raises(HTTPException) as error:
        require_admin(_request(), None)

    assert error.value.status_code == 503


def test_admin_auth_requires_matching_bearer_key(monkeypatch):
    monkeypatch.setenv("ADMIN_API_KEY", "a" * 40)

    with pytest.raises(HTTPException) as error:
        require_admin(_request(), "Bearer wrong")

    assert error.value.status_code == 401
    require_admin(_request(), f"Bearer {'a' * 40}")


def test_admin_auth_requires_https_in_production(monkeypatch):
    monkeypatch.setenv("ADMIN_API_KEY", "a" * 40)
    monkeypatch.setenv("APP_ENV", "production")

    with pytest.raises(HTTPException) as error:
        require_admin(_request("http"), f"Bearer {'a' * 40}")

    assert error.value.status_code == 400
    require_admin(_request("https"), f"Bearer {'a' * 40}")
