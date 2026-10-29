"""Persistence and live activation for versioned business-rule snapshots."""

import asyncio
import json
import logging
import os
from typing import Any, Dict
from uuid import uuid4

import aiomysql

from app.agent.nodes.business_rules import (
    BusinessRulesError,
    BusinessRulesStore,
    get_business_rules_store,
)
from app.agent.nodes.schema_grounding import (
    SchemaGroundingError,
    get_cached_relationships,
    get_cached_schema,
)
from app.db.mysql_client import get_pool
from app.services.business_rule_markdown import (
    BusinessRulesMarkdownError,
    validate_business_rule_schema_references,
)

logger = logging.getLogger(__name__)

REVISION_TABLE = "business_rule_revisions"
ACTIVE_TABLE = "business_rule_active"
_active_revision_id: str | None = None
_refresh_lock = asyncio.Lock()


class BusinessRuleStorageError(RuntimeError):
    """Raised when the versioned rulebook storage is unavailable or inconsistent."""


class BusinessRuleDraftNotFoundError(LookupError):
    """Raised when a requested rule draft does not exist."""


class BusinessRulePublishConflictError(RuntimeError):
    """Raised when a draft was based on an outdated active rulebook."""


class BusinessRuleRevisionStateError(RuntimeError):
    """Raised when a revision cannot transition from its current status."""


def _decode_rulebook(value: Any) -> Dict[str, Any]:
    try:
        if isinstance(value, bytes):
            value = value.decode("utf-8")
        if isinstance(value, str):
            value = json.loads(value)
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        raise BusinessRuleStorageError(
            "The stored business rulebook is not valid JSON."
        ) from e
    if not isinstance(value, dict):
        raise BusinessRuleStorageError(
            "The stored business rulebook is not a JSON object."
        )
    return value


async def validate_business_rule_storage() -> None:
    """Verify that the manually-applied migration is ready for admin operations."""
    pool = get_pool()
    async with pool.acquire() as connection:
        async with connection.cursor() as cursor:
            await cursor.execute(
                """
                SELECT COUNT(*)
                FROM information_schema.tables
                WHERE table_schema = DATABASE()
                  AND table_name IN (%s, %s)
                """,
                (REVISION_TABLE, ACTIVE_TABLE),
            )
            result = await cursor.fetchone()
            if not result or result[0] != 2:
                raise BusinessRuleStorageError(
                    "Business-rule admin storage is missing. Apply the "
                    "business-rule revisions migration before enabling ADMIN_API_KEY."
                )
            await cursor.execute(
                f"SELECT state_id FROM {ACTIVE_TABLE} WHERE state_id = 1"
            )
            if await cursor.fetchone() is None:
                raise BusinessRuleStorageError(
                    "Business-rule active state is not initialized. Reapply the "
                    "business-rule revisions migration."
                )


async def get_active_revision_info() -> tuple[str | None, str | None]:
    pool = get_pool()
    async with pool.acquire() as connection:
        async with connection.cursor() as cursor:
            await cursor.execute(
                f"""
                SELECT active.active_revision_id, revision.rulebook_hash
                FROM {ACTIVE_TABLE} AS active
                LEFT JOIN {REVISION_TABLE} AS revision
                  ON revision.revision_id = active.active_revision_id
                WHERE active.state_id = 1
                """
            )
            row = await cursor.fetchone()
    if row is None:
        raise BusinessRuleStorageError(
            "Business-rule active state is not initialized."
        )
    if row[0] is not None and row[1] is None:
        raise BusinessRuleStorageError(
            "The active business-rule revision is missing its content hash."
        )
    return row[0], row[1]


async def refresh_active_business_rules() -> None:
    """Refresh this worker's validated snapshot when another worker publishes."""
    global _active_revision_id
    if not os.getenv("ADMIN_API_KEY"):
        return
    async with _refresh_lock:
        pool = get_pool()
        async with pool.acquire() as connection:
            async with connection.cursor() as cursor:
                await cursor.execute(
                    f"""
                    SELECT active.active_revision_id, revision.compiled_rulebook,
                           revision.rulebook_hash
                    FROM {ACTIVE_TABLE} AS active
                    LEFT JOIN {REVISION_TABLE} AS revision
                      ON revision.revision_id = active.active_revision_id
                    WHERE active.state_id = 1
                    """
                )
                row = await cursor.fetchone()
        if row is None:
            raise BusinessRuleStorageError(
                "Business-rule active state is not initialized."
            )
        revision_id, raw_rulebook, stored_hash = row
        if revision_id is None:
            return
        if raw_rulebook is None or stored_hash is None:
            raise BusinessRuleStorageError(
                "The active business-rule revision is missing its content."
            )
        revision_id = str(revision_id)
        if revision_id == _active_revision_id:
            return

        rulebook = _decode_rulebook(raw_rulebook)
        candidate = BusinessRulesStore()
        try:
            candidate.load_data(rulebook)
        except BusinessRulesError as e:
            raise BusinessRuleStorageError(
                "The active business-rule revision failed validation."
            ) from e
        if candidate.version != stored_hash:
            raise BusinessRuleStorageError(
                "The active business-rule revision hash does not match its content."
            )
        try:
            validate_business_rule_schema_references(
                rulebook,
                get_cached_schema(),
                get_cached_relationships(),
            )
        except (ValueError, SchemaGroundingError) as e:
            raise BusinessRuleStorageError(
                "The active business-rule revision references unavailable schema."
            ) from e
        store = get_business_rules_store()
        try:
            store.load_data(rulebook)
        except BusinessRulesError as e:
            raise BusinessRuleStorageError(
                "The active business-rule revision failed validation."
            ) from e
        _active_revision_id = revision_id
        logger.info("Activated business-rule revision %s.", revision_id)


async def create_rule_draft(
    markdown: str,
    rulebook: Dict[str, Any],
    rulebook_hash: str,
    base_revision_id: str | None,
    base_rulebook_hash: str,
) -> str:
    revision_id = str(uuid4())
    compiled = json.dumps(
        rulebook, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    pool = get_pool()
    async with pool.acquire() as connection:
        async with connection.cursor() as cursor:
            await cursor.execute(
                f"""
                INSERT INTO {REVISION_TABLE} (
                    revision_id, base_revision_id, base_rulebook_hash,
                    document_markdown, compiled_rulebook, rulebook_hash,
                    status, created_by
                )
                VALUES (%s, %s, %s, %s, %s, %s, 'draft', 'api_key')
                """,
                (
                    revision_id,
                    base_revision_id,
                    base_rulebook_hash,
                    markdown,
                    compiled,
                    rulebook_hash,
                ),
            )
    return revision_id


async def get_rule_draft(revision_id: str) -> Dict[str, Any]:
    pool = get_pool()
    async with pool.acquire() as connection:
        async with connection.cursor() as cursor:
            await cursor.execute(
                f"""
                SELECT revision_id, base_revision_id, base_rulebook_hash,
                       document_markdown, compiled_rulebook, rulebook_hash,
                       status, created_at, published_at
                FROM {REVISION_TABLE}
                WHERE revision_id = %s
                """,
                (revision_id,),
            )
            row = await cursor.fetchone()
    if row is None:
        raise BusinessRuleDraftNotFoundError(
            f"Business-rule revision {revision_id} was not found."
        )
    return {
        "revision_id": str(row[0]),
        "base_revision_id": str(row[1]) if row[1] is not None else None,
        "base_rulebook_hash": row[2],
        "document_markdown": row[3],
        "compiled_rulebook": _decode_rulebook(row[4]),
        "rulebook_hash": row[5],
        "status": row[6],
        "created_at": row[7],
        "published_at": row[8],
    }


async def list_rule_revisions(limit: int = 50) -> list[Dict[str, Any]]:
    pool = get_pool()
    async with pool.acquire() as connection:
        async with connection.cursor() as cursor:
            await cursor.execute(
                f"""
                SELECT revision_id, base_revision_id, rulebook_hash, status,
                       created_at, published_at
                FROM {REVISION_TABLE}
                ORDER BY created_at DESC
                LIMIT %s
                """,
                (limit,),
            )
            rows = await cursor.fetchall()
    return [
        {
            "revision_id": str(row[0]),
            "base_revision_id": str(row[1]) if row[1] is not None else None,
            "rulebook_hash": row[2],
            "status": row[3],
            "created_at": row[4],
            "published_at": row[5],
        }
        for row in rows
    ]


async def publish_rule_draft(
    revision_id: str,
    fallback_rulebook_hash: str,
) -> Dict[str, Any]:
    pool = get_pool()
    async with pool.acquire() as connection:
        await connection.begin()
        try:
            async with connection.cursor() as cursor:
                await cursor.execute(
                    f"""
                    SELECT active_revision_id
                    FROM {ACTIVE_TABLE}
                    WHERE state_id = 1
                    FOR UPDATE
                    """
                )
                active_row = await cursor.fetchone()
                if active_row is None:
                    raise BusinessRuleStorageError(
                        "Business-rule active state is not initialized."
                    )
                current_revision_id = (
                    str(active_row[0]) if active_row[0] is not None else None
                )
                if current_revision_id is None:
                    current_rulebook_hash = fallback_rulebook_hash
                else:
                    await cursor.execute(
                        f"""
                        SELECT rulebook_hash
                        FROM {REVISION_TABLE}
                        WHERE revision_id = %s
                        """,
                        (current_revision_id,),
                    )
                    active_hash_row = await cursor.fetchone()
                    if active_hash_row is None:
                        raise BusinessRuleStorageError(
                            "The active business-rule revision is missing."
                        )
                    current_rulebook_hash = active_hash_row[0]

                await cursor.execute(
                    f"""
                    SELECT base_revision_id, base_rulebook_hash,
                           compiled_rulebook, rulebook_hash, status
                    FROM {REVISION_TABLE}
                    WHERE revision_id = %s
                    FOR UPDATE
                    """,
                    (revision_id,),
                )
                draft = await cursor.fetchone()
                if draft is None:
                    raise BusinessRuleDraftNotFoundError(
                        f"Business-rule revision {revision_id} was not found."
                    )
                if draft[4] != "draft":
                    raise BusinessRuleRevisionStateError(
                        f"Business-rule revision {revision_id} is not a draft."
                    )
                base_revision_id = (
                    str(draft[0]) if draft[0] is not None else None
                )
                if (
                    base_revision_id != current_revision_id
                    or draft[1] != current_rulebook_hash
                ):
                    raise BusinessRulePublishConflictError(
                        "The active rulebook changed after this draft was created. "
                        "Export the latest rules and create a new draft."
                    )
                rulebook = _decode_rulebook(draft[2])
                candidate = BusinessRulesStore()
                try:
                    candidate.load_data(rulebook)
                except BusinessRulesError as e:
                    raise BusinessRuleStorageError(
                        "The draft rulebook failed validation before publication."
                    ) from e
                rulebook_hash = draft[3]
                if candidate.version != rulebook_hash:
                    raise BusinessRuleStorageError(
                        "The draft rulebook hash does not match its stored content."
                    )
                try:
                    validate_business_rule_schema_references(
                        rulebook,
                        get_cached_schema(),
                        get_cached_relationships(),
                    )
                except SchemaGroundingError as e:
                    raise BusinessRuleStorageError(
                        "The live schema is unavailable; the draft cannot be published."
                    ) from e
                except BusinessRulesMarkdownError as e:
                    raise BusinessRulePublishConflictError(
                        "The verified schema changed after this draft was created. "
                        "Export the latest rules and create a new draft."
                    ) from e
                await cursor.execute(
                    f"""
                    UPDATE {REVISION_TABLE}
                    SET status = 'superseded'
                    WHERE status = 'published'
                    """
                )
                await cursor.execute(
                    f"""
                    UPDATE {REVISION_TABLE}
                    SET status = 'published', published_at = CURRENT_TIMESTAMP
                    WHERE revision_id = %s AND status = 'draft'
                    """,
                    (revision_id,),
                )
                await cursor.execute(
                    f"""
                    UPDATE {ACTIVE_TABLE}
                    SET active_revision_id = %s
                    WHERE state_id = 1
                    """,
                    (revision_id,),
                )
            await connection.commit()
        except BaseException:
            await connection.rollback()
            raise

    await refresh_active_business_rules()
    logger.info(
        "Published business-rule revision %s with hash %s.",
        revision_id,
        rulebook_hash,
    )
    return {
        "revision_id": revision_id,
        "rulebook_hash": rulebook_hash,
        "compiled_rulebook": rulebook,
    }
