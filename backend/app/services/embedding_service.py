"""
embedding_service.py — Hybrid (keyword + semantic) schema table retrieval.

Solves the "all 49 tables sent to the LLM every time" problem: narrows the
schema down to the handful of tables actually relevant to a question,
before Node 4/6 ever see it.

Two signals, fused together:
  - SPARSE (BM25 keyword): catches exact table/column name mentions —
    "show me all invoices" -> table "invoices" guaranteed included.
  - DENSE (local embeddings): catches meaning-based matches with no
    shared words — "who's paying us" -> table "invoices" via similarity.

A minimum-score floor is applied before fusion so a table with only weak,
noisy keyword overlap (and no real semantic relevance) can't get dragged
into the top-k just because BM25 found a coincidental partial match.

Built once per schema refresh (cheap at ~49 tables), reused for every
question until the schema cache refreshes again.
"""

import logging
import os
import asyncio
from typing import Any, Dict, List, Optional
import re
import numpy as np
from fastembed import TextEmbedding
from rank_bm25 import BM25Okapi

logger = logging.getLogger(__name__)

EMBEDDING_MODEL = os.getenv("SCHEMA_EMBEDDING_MODEL", "BAAI/bge-small-en-v1.5")
# Keeps the downloaded model in your project. Without this, Windows may store
# it in a temp folder that gets cleaned, forcing a re-download.
EMBEDDING_CACHE_DIR = os.getenv("SCHEMA_EMBEDDING_CACHE_DIR", "models_cache")

DEFAULT_TOP_K = 6

# Minimum score a table must clear in AT LEAST ONE of the two rankings to
# be eligible for the fused top-k at all. Stops BM25's weak/coincidental
# keyword overlap alone from forcing a clearly-irrelevant table in, when
# neither signal actually thinks it's a good match.
MIN_DENSE_SIMILARITY = 0.35
MIN_BM25_SCORE = 0.5

RRF_K = 60  # standard Reciprocal Rank Fusion constant


class EmbeddingServiceError(Exception):
    """Raised when embedding generation or index building fails."""
    pass


_model: Optional[TextEmbedding] = None


def _get_model() -> TextEmbedding:
    """Loads the model once and reuses it. The first run downloads about 130 MB."""
    global _model
    if _model is None:
        try:
            _model = TextEmbedding(
                model_name=EMBEDDING_MODEL,
                cache_dir=EMBEDDING_CACHE_DIR,
            )
        except Exception as e:
            raise EmbeddingServiceError(
                f"Could not load embedding model '{EMBEDDING_MODEL}': {e}"
            ) from e
    return _model


def _embed_sync(texts: List[str]) -> np.ndarray:
    model = _get_model()
    vectors = np.array(list(model.embed(texts)), dtype=np.float32)
    norms = np.linalg.norm(vectors, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    return vectors / norms


async def _embed_texts(texts: List[str]) -> np.ndarray:
    """Runs the blocking model call in a worker thread so the server doesn't freeze."""
    try:
        return await asyncio.to_thread(_embed_sync, texts)
    except EmbeddingServiceError:
        raise
    except Exception as e:
        raise EmbeddingServiceError(f"Embedding call failed: {e}") from e


class SchemaIndex:
    """Per-table searchable text + embeddings, rebuilt on each schema refresh."""

    def __init__(self) -> None:
        self.table_names: List[str] = []
        self.table_texts: List[str] = []
        self.embeddings: Optional[np.ndarray] = None  # (n_tables, dim), unit-normalized
        self.bm25: Optional[BM25Okapi] = None

    def is_built(self) -> bool:
        return self.bm25 is not None and self.embeddings is not None


_index = SchemaIndex()


_WORD_PATTERN = re.compile(r"[a-z0-9]+")


def _tokenize(text: str) -> List[str]:
    return _WORD_PATTERN.findall(text.lower().replace("_", " "))


def _table_name_variants(name: str) -> List[str]:
    """
    Simple singular/plural variants for a table name, so 'users' matches
    a question containing 'user' and vice versa. Not a full stemmer —
    just handles the common trailing-s case, which covers most real
    table-naming conventions.
    """
    name_lower = name.lower()
    variants = {name_lower}
    if name_lower.endswith("s"):
        variants.add(name_lower[:-1])
    else:
        variants.add(f"{name_lower}s")
    return list(variants)


def _guaranteed_table_names(question: str, table_names: List[str]) -> set[str]:
    question_tokens = set(_tokenize(question))
    return {
        name
        for name in table_names
        if any(
            set(_tokenize(variant)) <= question_tokens
            for variant in _table_name_variants(name)
        )
    }


def _schema_to_table_text(table: str, columns: List[Dict[str, Any]]) -> str:
    col_names = ", ".join(
        c["column"] for c in columns if isinstance(c, dict) and "column" in c
    )
    return f"Table '{table}' with columns: {col_names}"


async def build_schema_index(schema_context: Dict[str, List[Dict[str, Any]]]) -> None:
    """
    Rebuilds the hybrid retrieval index from the cached schema. Call this
    right after refresh_schema_cache() so the index always matches the
    live schema.
    """
    table_names = list(schema_context.keys())
    table_texts = [_schema_to_table_text(t, schema_context[t]) for t in table_names]

    embeddings = await _embed_texts(table_texts)
    bm25 = BM25Okapi([_tokenize(t) for t in table_texts])

    _index.table_names = table_names
    _index.table_texts = table_texts
    _index.embeddings = embeddings
    _index.bm25 = bm25

    logger.info(
        "Schema index built: %d table(s) embedded + keyword-indexed", len(table_names)
    )


async def retrieve_relevant_tables(question: str, top_k: int = DEFAULT_TOP_K) -> List[str]:
    """
    Returns the top_k table names most relevant to the question, fusing
    exact/near-exact keyword matches with semantic similarity.

    Guarantees: any table whose name (or simple singular/plural variant)
    appears as a literal whole word in the question is always included,
    regardless of ranking.
    """
    if not _index.is_built():
        raise EmbeddingServiceError(
            "Schema index has not been built yet. Call build_schema_index() first."
        )

    # --- Guaranteed exact/near-exact name matches (plural-aware) ---
    guaranteed = _guaranteed_table_names(question, _index.table_names)

    # --- Sparse (BM25) ranking ---
    bm25_scores = _index.bm25.get_scores(_tokenize(question))
    sparse_ranked_all = sorted(
        range(len(_index.table_names)), key=lambda i: bm25_scores[i], reverse=True
    )

    # --- Dense (semantic) ranking ---
    try:
        query_vec = (await _embed_texts([question]))[0]
        dense_scores = _index.embeddings @ query_vec
        dense_ranked_all = sorted(
            range(len(_index.table_names)), key=lambda i: dense_scores[i], reverse=True
        )
    except EmbeddingServiceError as e:
        logger.warning("Dense retrieval failed, falling back to sparse only: %s", e)
        dense_scores = np.zeros(len(_index.table_names))
        dense_ranked_all = sparse_ranked_all

    # --- Score floor: a table only enters fusion if at least one method
    # actually rated it meaningfully. Prevents BM25's weak/coincidental
    # keyword overlap alone from forcing an irrelevant table into top-k
    # when semantic similarity also says it's a poor match. ---
    eligible = {
        i for i in range(len(_index.table_names))
        if dense_scores[i] >= MIN_DENSE_SIMILARITY or bm25_scores[i] >= MIN_BM25_SCORE
    }
    sparse_ranked = [i for i in sparse_ranked_all if i in eligible]
    dense_ranked = [i for i in dense_ranked_all if i in eligible]

    # --- Reciprocal Rank Fusion — combines both rankings without needing
    # to tune relative score weights (BM25 and cosine scores aren't on the
    # same scale, so averaging raw scores would be meaningless). ---
    fused_scores: Dict[str, float] = {}
    for rank, idx in enumerate(sparse_ranked):
        name = _index.table_names[idx]
        fused_scores[name] = fused_scores.get(name, 0.0) + 1.0 / (RRF_K + rank + 1)
    for rank, idx in enumerate(dense_ranked):
        name = _index.table_names[idx]
        fused_scores[name] = fused_scores.get(name, 0.0) + 1.0 / (RRF_K + rank + 1)

    ranked_names = sorted(fused_scores, key=lambda n: fused_scores[n], reverse=True)

    # --- Assemble final list: guaranteed matches first, then fill from
    # the fused ranking. If the floor left too few eligible tables to
    # reach top_k (e.g. a truly unrelated question), fall back to the
    # unfiltered ranking so we still return exactly top_k tables — Node 4
    # is what decides "insufficient" from here, not this function. ---
    result = list(guaranteed)
    for name in ranked_names:
        if len(result) >= top_k:
            break
        if name not in result:
            result.append(name)

    if len(result) < top_k:
        fallback_ranked = sorted(
            range(len(_index.table_names)),
            key=lambda i: dense_scores[i] + bm25_scores[i],
            reverse=True,
        )
        for idx in fallback_ranked:
            if len(result) >= top_k:
                break
            name = _index.table_names[idx]
            if name not in result:
                result.append(name)

    result = result[:top_k]

    logger.info("Retrieved %d relevant table(s) for %r: %s", len(result), question, result)
    return result


def filter_schema_to_tables(
    schema_context: Dict[str, List[Dict[str, Any]]], table_names: List[str]
) -> Dict[str, List[Dict[str, Any]]]:
    """Slices the full schema dict down to only the given tables."""
    return {t: schema_context[t] for t in table_names if t in schema_context}