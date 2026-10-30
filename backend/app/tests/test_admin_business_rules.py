import json
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.agent.nodes.business_rules import BusinessRulesStore
from app.api import admin_business_rules
from app.services.business_rule_markdown import render_business_rules_markdown


RULEBOOK_PATH = (
    Path(__file__).resolve().parents[1] / "rules" / "business_rules.json"
)


def _app():
    application = FastAPI()
    application.include_router(admin_business_rules.router)
    return application


def test_upload_creates_validated_draft_preview(monkeypatch):
    rulebook = json.loads(RULEBOOK_PATH.read_text(encoding="utf-8"))
    rulebook["_meta"]["version"] = "draft-test"
    markdown = render_business_rules_markdown(rulebook)
    store = BusinessRulesStore(str(RULEBOOK_PATH))
    created = {}

    async def no_refresh():
        return None

    async def active_info():
        return None, None

    async def create_draft(**kwargs):
        created.update(kwargs)
        return "d0eb08e2-b181-4b13-b712-029ee7b6dcf8"

    monkeypatch.setenv("ADMIN_API_KEY", "a" * 40)
    monkeypatch.setattr(
        admin_business_rules, "refresh_active_business_rules", no_refresh
    )
    monkeypatch.setattr(
        admin_business_rules, "get_active_revision_info", active_info
    )
    monkeypatch.setattr(
        admin_business_rules,
        "create_rule_draft",
        create_draft,
    )
    monkeypatch.setattr(
        admin_business_rules,
        "get_business_rules_store",
        lambda: store,
    )
    monkeypatch.setattr(
        admin_business_rules, "get_cached_schema", lambda: {}
    )
    monkeypatch.setattr(
        admin_business_rules, "get_cached_relationships", lambda: []
    )
    monkeypatch.setattr(
        admin_business_rules,
        "validate_business_rule_schema_references",
        lambda *_args: None,
    )

    response = TestClient(_app()).post(
        "/api/admin/business-rules/drafts",
        content=markdown,
        headers={
            "Authorization": f"Bearer {'a' * 40}",
            "Content-Type": "text/markdown; charset=utf-8",
        },
    )

    assert response.status_code == 201
    payload = response.json()
    assert payload["status"] == "draft"
    assert payload["preview"]["rulebook_version"] == "draft-test"
    assert payload["preview"]["term_count"] == len(rulebook["terms"])
    assert created["base_revision_id"] is None
    assert created["rulebook"]["_meta"]["version"] == "draft-test"


def test_upload_increments_active_rulebook_version_automatically(monkeypatch):
    rulebook = json.loads(RULEBOOK_PATH.read_text(encoding="utf-8"))
    markdown = render_business_rules_markdown(rulebook)
    store = BusinessRulesStore(str(RULEBOOK_PATH))
    created = {}

    async def no_refresh():
        return None

    async def active_info():
        return None, None

    async def create_draft(**kwargs):
        created.update(kwargs)
        return "d0eb08e2-b181-4b13-b712-029ee7b6dcf8"

    monkeypatch.setenv("ADMIN_API_KEY", "a" * 40)
    monkeypatch.setattr(
        admin_business_rules, "refresh_active_business_rules", no_refresh
    )
    monkeypatch.setattr(
        admin_business_rules, "get_active_revision_info", active_info
    )
    monkeypatch.setattr(admin_business_rules, "create_rule_draft", create_draft)
    monkeypatch.setattr(
        admin_business_rules, "get_business_rules_store", lambda: store
    )
    monkeypatch.setattr(admin_business_rules, "get_cached_schema", lambda: {})
    monkeypatch.setattr(admin_business_rules, "get_cached_relationships", lambda: [])
    monkeypatch.setattr(
        admin_business_rules,
        "validate_business_rule_schema_references",
        lambda *_args: None,
    )

    response = TestClient(_app()).post(
        "/api/admin/business-rules/drafts",
        content=markdown,
        headers={
            "Authorization": "Bearer " + ("a" * 40),
            "Content-Type": "text/markdown; charset=utf-8",
        },
    )

    assert response.status_code == 201
    payload = response.json()
    assert payload["preview"]["rulebook_version"] == "1.10.1"
    assert created["rulebook"]["_meta"]["version"] == "1.10.1"
    assert '"version": "1.10.1"' in created["markdown"]


def test_upload_rejects_invalid_markdown_and_invalid_auth(monkeypatch):
    monkeypatch.setenv("ADMIN_API_KEY", "a" * 40)
    client = TestClient(_app())

    invalid_markdown = client.post(
        "/api/admin/business-rules/drafts",
        content="# not a rulebook",
        headers={
            "Authorization": f"Bearer {'a' * 40}",
            "Content-Type": "text/markdown",
        },
    )
    unauthorized = client.post(
        "/api/admin/business-rules/drafts",
        content="# not a rulebook",
        headers={"Content-Type": "text/markdown"},
    )

    assert invalid_markdown.status_code == 422
    assert unauthorized.status_code == 401


def test_upload_requires_https_in_production(monkeypatch):
    monkeypatch.setenv("ADMIN_API_KEY", "a" * 40)
    monkeypatch.setenv("APP_ENV", "production")
    response = TestClient(_app()).get(
        "/api/admin/business-rules/current.md",
        headers={"Authorization": f"Bearer {'a' * 40}"},
    )

    assert response.status_code == 400
