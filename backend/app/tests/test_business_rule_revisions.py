import asyncio
import json
from pathlib import Path

import pytest

from app.agent.nodes.business_rules import BusinessRulesStore
from app.services import business_rule_revisions as revisions


RULEBOOK_PATH = (
    Path(__file__).resolve().parents[1] / "rules" / "business_rules.json"
)


class _Cursor:
    def __init__(
        self,
        *,
        active_row=None,
        active_hash_row=None,
        draft_row=None,
        refresh_row=None,
    ):
        self.active_row = active_row
        self.active_hash_row = active_hash_row
        self.draft_row = draft_row
        self.refresh_row = refresh_row
        self.row = None
        self.statements = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    async def execute(self, query, params=()):
        self.statements.append((query, params))
        if "SELECT active_revision_id" in query:
            self.row = self.active_row
        elif "SELECT rulebook_hash" in query:
            self.row = self.active_hash_row
        elif "SELECT base_revision_id" in query:
            self.row = self.draft_row
        elif "SELECT active.active_revision_id" in query:
            self.row = self.refresh_row
        else:
            self.row = None

    async def fetchone(self):
        return self.row

    async def fetchall(self):
        return []


class _Connection:
    def __init__(self, cursor):
        self._cursor = cursor
        self.committed = False
        self.rolled_back = False

    def cursor(self):
        return self._cursor

    async def begin(self):
        return None

    async def commit(self):
        self.committed = True

    async def rollback(self):
        self.rolled_back = True


class _Pool:
    def __init__(self, connection):
        self.connection = connection

    def acquire(self):
        return self

    async def __aenter__(self):
        return self.connection

    async def __aexit__(self, *_args):
        return None


def _new_rulebook():
    rulebook = json.loads(RULEBOOK_PATH.read_text(encoding="utf-8"))
    rulebook["_meta"]["version"] = "test-draft"
    candidate = BusinessRulesStore()
    candidate.load_data(rulebook)
    return rulebook, candidate


def test_publish_commits_only_a_valid_draft_based_on_active_version(monkeypatch):
    baseline = BusinessRulesStore(str(RULEBOOK_PATH))
    base_hash = baseline.version
    rulebook, candidate = _new_rulebook()
    revision_id = "d0eb08e2-b181-4b13-b712-029ee7b6dcf8"
    cursor = _Cursor(
        active_row=(None,),
        draft_row=(
            None,
            base_hash,
            json.dumps(rulebook),
            candidate.version,
            "draft",
        ),
    )
    connection = _Connection(cursor)
    monkeypatch.setattr(revisions, "get_pool", lambda: _Pool(connection))
    monkeypatch.setattr(revisions, "get_cached_schema", lambda: {})
    monkeypatch.setattr(revisions, "get_cached_relationships", lambda: [])
    monkeypatch.setattr(
        revisions, "validate_business_rule_schema_references", lambda *_args: None
    )

    async def no_refresh():
        return None

    monkeypatch.setattr(revisions, "refresh_active_business_rules", no_refresh)
    published = asyncio.run(
        revisions.publish_rule_draft(revision_id, base_hash)
    )

    assert connection.committed is True
    assert connection.rolled_back is False
    assert published["revision_id"] == revision_id
    assert published["rulebook_hash"] == candidate.version
    assert published["compiled_rulebook"] == rulebook
    assert any("UPDATE business_rule_active" in query for query, _ in cursor.statements)


def test_publish_rejects_stale_draft_and_rolls_back(monkeypatch):
    baseline = BusinessRulesStore(str(RULEBOOK_PATH))
    rulebook, candidate = _new_rulebook()
    cursor = _Cursor(
        active_row=("active-revision",),
        active_hash_row=("active-hash",),
        draft_row=(
            None,
            "stale-base-hash",
            json.dumps(rulebook),
            candidate.version,
            "draft",
        ),
    )
    connection = _Connection(cursor)
    monkeypatch.setattr(revisions, "get_pool", lambda: _Pool(connection))

    with pytest.raises(revisions.BusinessRulePublishConflictError):
        asyncio.run(
            revisions.publish_rule_draft(
                "draft-revision",
                fallback_rulebook_hash=baseline.version,
            )
        )

    assert connection.committed is False
    assert connection.rolled_back is True


def test_publish_rejects_rulebook_when_verified_schema_has_changed(monkeypatch):
    baseline = BusinessRulesStore(str(RULEBOOK_PATH))
    rulebook, candidate = _new_rulebook()
    cursor = _Cursor(
        active_row=(None,),
        draft_row=(
            None,
            baseline.version,
            json.dumps(rulebook),
            candidate.version,
            "draft",
        ),
    )
    connection = _Connection(cursor)
    monkeypatch.setattr(revisions, "get_pool", lambda: _Pool(connection))
    monkeypatch.setattr(revisions, "get_cached_schema", lambda: {})
    monkeypatch.setattr(revisions, "get_cached_relationships", lambda: [])

    def reject_schema_references(*_args):
        raise revisions.BusinessRulesMarkdownError("Schema reference not found.")

    monkeypatch.setattr(
        revisions,
        "validate_business_rule_schema_references",
        reject_schema_references,
    )

    with pytest.raises(revisions.BusinessRulePublishConflictError, match="schema changed"):
        asyncio.run(
            revisions.publish_rule_draft(
                "draft-revision",
                fallback_rulebook_hash=baseline.version,
            )
        )

    assert connection.committed is False
    assert connection.rolled_back is True
    assert not any(
        "UPDATE business_rule_active" in query for query, _ in cursor.statements
    )


def test_worker_refreshes_validated_snapshot_from_active_revision(monkeypatch):
    rulebook, candidate = _new_rulebook()
    cursor = _Cursor(
        refresh_row=(
            "active-revision",
            json.dumps(rulebook),
            candidate.version,
        )
    )
    monkeypatch.setenv("ADMIN_API_KEY", "a" * 40)
    monkeypatch.setattr(revisions, "get_pool", lambda: _Pool(_Connection(cursor)))
    store = BusinessRulesStore(str(RULEBOOK_PATH))
    monkeypatch.setattr(revisions, "get_business_rules_store", lambda: store)
    monkeypatch.setattr(revisions, "_active_revision_id", None)
    monkeypatch.setattr(revisions, "get_cached_schema", lambda: {})
    monkeypatch.setattr(revisions, "get_cached_relationships", lambda: [])
    monkeypatch.setattr(
        revisions, "validate_business_rule_schema_references", lambda *_args: None
    )

    asyncio.run(revisions.refresh_active_business_rules())

    assert store.version == candidate.version
    assert store.export_rulebook() == rulebook
