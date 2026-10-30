"""API-key-protected draft, preview, and publication endpoints for business rules."""

import logging
import re
from uuid import UUID

import aiomysql
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import PlainTextResponse

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
from app.api.admin_auth import require_admin
from app.services.business_rule_markdown import (
    BusinessRulesMarkdownError,
    MAX_MARKDOWN_BYTES,
    parse_business_rules_markdown,
    render_business_rules_markdown,
    validate_business_rule_schema_references,
)
from app.services.business_rule_revisions import (
    BusinessRuleDraftNotFoundError,
    BusinessRulePublishConflictError,
    BusinessRuleRevisionStateError,
    BusinessRuleStorageError,
    create_rule_draft,
    get_active_revision_info,
    get_rule_draft,
    list_rule_revisions,
    publish_rule_draft,
    refresh_active_business_rules,
)

logger = logging.getLogger(__name__)
router = APIRouter(
    prefix="/api/admin/business-rules",
    tags=["admin-business-rules"],
    dependencies=[Depends(require_admin)],
)


def _rule_diff(current: dict, proposed: dict) -> dict:
    def keyed(items: list, key: str) -> dict[str, dict]:
        return {
            item[key]: item
            for item in items
            if isinstance(item, dict) and isinstance(item.get(key), str)
        }

    groups = (
        ("terms", "term"),
        ("ontology_entities", "entity"),
    )
    changes = {}
    for collection, key in groups:
        old = keyed(current.get(collection, []), key)
        new = keyed(proposed.get(collection, []), key)
        added = sorted(new.keys() - old.keys())
        removed = sorted(old.keys() - new.keys())
        changed = sorted(
            name for name in old.keys() & new.keys()
            if old[name] != new[name]
        )
        changes[collection] = {
            "added": added,
            "removed": removed,
            "changed": changed,
        }
    return changes


def _draft_preview(current: dict, proposed: dict) -> dict:
    return {
        "rulebook_version": proposed["_meta"]["version"],
        "term_count": len(proposed.get("terms", [])),
        "ontology_entity_count": len(proposed.get("ontology_entities", [])),
        "changes": _rule_diff(current, proposed),
    }


def _next_rulebook_version(version: str) -> str:
    semantic_version = re.fullmatch(r"(\d+)\.(\d+)\.(\d+)", version)
    if semantic_version:
        major, minor, patch = semantic_version.groups()
        return f"{major}.{minor}.{int(patch) + 1}"

    draft_version = re.fullmatch(r"(.+\.draft\.)(\d+)", version)
    if draft_version:
        prefix, draft_number = draft_version.groups()
        return f"{prefix}{int(draft_number) + 1}"
    return f"{version}.draft.1"


def _raise_storage_unavailable(error: Exception) -> HTTPException:
    logger.error(
        "Business-rule admin storage operation failed: %s",
        error,
        exc_info=(type(error), error, error.__traceback__),
    )
    return HTTPException(
        status_code=503,
        detail="Business-rule revision storage is unavailable.",
    )


async def _read_markdown_body(request: Request) -> str:
    content_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    if content_type not in {"text/markdown", "text/plain"}:
        raise HTTPException(
            status_code=415,
            detail="Upload the Markdown document as text/markdown.",
        )

    content_length = request.headers.get("content-length")
    if content_length:
        try:
            if int(content_length) > MAX_MARKDOWN_BYTES:
                raise HTTPException(
                    status_code=413,
                    detail=f"Markdown uploads are limited to {MAX_MARKDOWN_BYTES} bytes.",
                )
        except ValueError as e:
            raise HTTPException(
                status_code=400,
                detail="Invalid Content-Length header.",
            ) from e

    chunks = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > MAX_MARKDOWN_BYTES:
            raise HTTPException(
                status_code=413,
                detail=f"Markdown uploads are limited to {MAX_MARKDOWN_BYTES} bytes.",
            )
        chunks.append(chunk)
    try:
        return b"".join(chunks).decode("utf-8")
    except UnicodeDecodeError as e:
        raise HTTPException(
            status_code=400,
            detail="Markdown uploads must be UTF-8 encoded.",
        ) from e


@router.get("/current.md", response_class=PlainTextResponse)
async def export_current_rulebook() -> Response:
    try:
        await refresh_active_business_rules()
        markdown = render_business_rules_markdown(
            get_business_rules_store().export_rulebook()
        )
    except (aiomysql.MySQLError, BusinessRuleStorageError) as e:
        raise _raise_storage_unavailable(e) from e
    return PlainTextResponse(
        markdown,
        media_type="text/markdown",
        headers={"Content-Disposition": 'attachment; filename="business-rules.md"'},
    )


@router.get("/revisions")
async def revisions(limit: int = 50) -> dict:
    if not 1 <= limit <= 100:
        raise HTTPException(
            status_code=422,
            detail="The revision limit must be between 1 and 100.",
        )
    try:
        return {"revisions": await list_rule_revisions(limit)}
    except aiomysql.MySQLError as e:
        raise _raise_storage_unavailable(e) from e


@router.post("/drafts", status_code=201)
async def create_draft(request: Request) -> dict:
    markdown = await _read_markdown_body(request)
    try:
        rulebook = parse_business_rules_markdown(markdown)
        validate_business_rule_schema_references(
            rulebook,
            get_cached_schema(),
            get_cached_relationships(),
        )
        candidate = BusinessRulesStore()
        candidate.load_data(rulebook)
    except (BusinessRulesMarkdownError, BusinessRulesError) as e:
        raise HTTPException(status_code=422, detail=str(e)) from e
    except SchemaGroundingError as e:
        raise _raise_storage_unavailable(e) from e

    try:
        await refresh_active_business_rules()
        current_store = get_business_rules_store()
        current_rulebook = current_store.export_rulebook()
        base_revision_id, active_hash = await get_active_revision_info()
        base_rulebook_hash = active_hash or current_store.version
        if rulebook["_meta"]["version"] == current_rulebook["_meta"]["version"]:
            rulebook["_meta"]["version"] = _next_rulebook_version(
                current_rulebook["_meta"]["version"]
            )
            markdown = render_business_rules_markdown(rulebook)
            candidate = BusinessRulesStore()
            candidate.load_data(rulebook)
        revision_id = await create_rule_draft(
            markdown=markdown,
            rulebook=rulebook,
            rulebook_hash=candidate.version,
            base_revision_id=base_revision_id,
            base_rulebook_hash=base_rulebook_hash,
        )
    except HTTPException:
        raise
    except (aiomysql.MySQLError, BusinessRuleStorageError) as e:
        raise _raise_storage_unavailable(e) from e

    return {
        "revision_id": revision_id,
        "status": "draft",
        "base_revision_id": base_revision_id,
        "base_rulebook_hash": base_rulebook_hash,
        "preview": _draft_preview(current_rulebook, rulebook),
    }


@router.get("/drafts/{revision_id}")
async def draft_details(revision_id: UUID) -> dict:
    try:
        draft = await get_rule_draft(str(revision_id))
    except BusinessRuleDraftNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    except (aiomysql.MySQLError, BusinessRuleStorageError) as e:
        raise _raise_storage_unavailable(e) from e

    try:
        await refresh_active_business_rules()
        current_rulebook = get_business_rules_store().export_rulebook()
    except (aiomysql.MySQLError, BusinessRuleStorageError) as e:
        raise _raise_storage_unavailable(e) from e
    return {
        "revision_id": draft["revision_id"],
        "status": draft["status"],
        "base_revision_id": draft["base_revision_id"],
        "base_rulebook_hash": draft["base_rulebook_hash"],
        "rulebook_hash": draft["rulebook_hash"],
        "created_at": draft["created_at"],
        "published_at": draft["published_at"],
        "preview": _draft_preview(current_rulebook, draft["compiled_rulebook"]),
        "document_markdown": draft["document_markdown"],
    }


@router.post("/drafts/{revision_id}/publish")
async def publish_draft(revision_id: UUID) -> dict:
    try:
        await refresh_active_business_rules()
        store = get_business_rules_store()
        published = await publish_rule_draft(
            str(revision_id),
            fallback_rulebook_hash=store.version,
        )
    except BusinessRuleDraftNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    except BusinessRulePublishConflictError as e:
        raise HTTPException(status_code=409, detail=str(e)) from e
    except BusinessRuleRevisionStateError as e:
        raise HTTPException(status_code=409, detail=str(e)) from e
    except (aiomysql.MySQLError, BusinessRuleStorageError) as e:
        raise _raise_storage_unavailable(e) from e

    return {
        "revision_id": published["revision_id"],
        "rulebook_hash": published["rulebook_hash"],
        "status": "published",
        "term_count": len(published["compiled_rulebook"].get("terms", [])),
        "ontology_entity_count": len(
            published["compiled_rulebook"].get("ontology_entities", [])
        ),
    }
