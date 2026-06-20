"""OpenSearch SearchBackend (MCM2-15b).

The demanding case for the adapter contract: no SQL, vendor-specific
JSON DSL, separate index per entity type, eventual consistency between
write and read (mitigated by an explicit refresh).

**v1 sync model:** ``search()`` pulls every row from the paired
``StorageBackend`` and re-indexes before querying. This is brute-force
correct but O(N) per query. Production deployments will replace this
with the watcher cascade (MCM2-23 in Phase 4a). The cost is acceptable
for a reference adapter validating the contract.
"""
from __future__ import annotations

from typing import Any, Optional

from opensearchpy import OpenSearch, helpers

from ...backends import (
    CONTRACT_VERSION,
    Capability,
    EntityType,
    SearchHit,
    StorageBackend,
)


# Per-entity, which fields go into the OpenSearch document and which
# field is the highest-weight match target (used to bias the BM25-style
# scoring via field boosts).
_INDEX_MAPPING: dict[EntityType, dict[str, Any]] = {
    EntityType.KNOWLEDGE: {
        "name": "knowledge",
        "primary":   "topic",   # weight A
        "secondary": "summary", # weight B
        "tertiary":  "detail",  # weight C
        "quaternary": "tags",   # weight D
    },
    EntityType.NEGATIVE: {
        "name": "negative",
        "primary":   "category",
        "secondary": "what_failed",
        "tertiary":  "why_failed",
        "quaternary": None,
    },
    EntityType.ERROR: {
        "name": "errors",
        "primary":   "pattern",
        "secondary": "root_cause",
        "tertiary":  "fix",
        "quaternary": "tags",
    },
    EntityType.RULE: {
        "name": "rules",
        "primary":   "title",
        "secondary": "keywords",
        "tertiary":  "description",
        "quaternary": "category",
    },
}


def _index_mapping_body() -> dict[str, Any]:
    return {
        "mappings": {
            "properties": {
                "id":        {"type": "long"},
                "primary":   {"type": "text", "analyzer": "english"},
                "secondary": {"type": "text", "analyzer": "english"},
                "tertiary":  {"type": "text", "analyzer": "english"},
                "quaternary":{"type": "text", "analyzer": "english"},
                "pinned":    {"type": "boolean"},
                "project":   {"type": "keyword"},
            }
        }
    }


def _row_to_doc(entity_type: EntityType, row: Any) -> dict[str, Any]:
    """Project a storage row dataclass into the OpenSearch doc shape."""
    mapping = _INDEX_MAPPING[entity_type]

    def _field(name):
        if name is None:
            return None
        return getattr(row, name, None)

    project = getattr(row, "project", None)
    return {
        "id":         row.id,
        "primary":    _field(mapping["primary"])   or "",
        "secondary":  _field(mapping["secondary"]) or "",
        "tertiary":   _field(mapping["tertiary"])  or "",
        "quaternary": _field(mapping["quaternary"]) or "",
        "pinned":     bool(getattr(row, "pinned", False)),
        "project":    project,
    }


class OpenSearchSearch:
    """SearchBackend on OpenSearch.

    Constructed with the OpenSearch URL and a ``storage`` reference
    used to pull rows for indexing. The ``index_prefix`` namespaces
    every index this instance owns — useful for parallel tests.
    """

    CONTRACT_VERSION: int = CONTRACT_VERSION
    capabilities: set[Capability] = set()

    def __init__(
        self,
        url: str,
        *,
        storage: StorageBackend,
        index_prefix: str = "mcm-",
    ):
        self._url = url
        self._prefix = index_prefix
        self._storage = storage
        # opensearch-py accepts either a URL list or a hosts dict.
        self._client = OpenSearch(
            hosts=[url],
            http_compress=True,
            use_ssl=False,
            verify_certs=False,
            ssl_show_warn=False,
            timeout=5,
        )

    # ---- index management ----

    def _index_name(self, entity_type: EntityType) -> str:
        return f"{self._prefix}{_INDEX_MAPPING[entity_type]['name']}"

    def reset_indexes(self) -> None:
        """Delete and recreate every owned index. Test convenience."""
        for et in EntityType:
            idx = self._index_name(et)
            if self._client.indices.exists(index=idx):
                self._client.indices.delete(index=idx)
            self._client.indices.create(index=idx, body=_index_mapping_body())

    def reindex(self, entity_type: Optional[EntityType] = None) -> None:
        """Rebuild the index for one (or all) entity types from
        ``storage``. The v1 sync mechanism — the watcher cascade in
        Phase 4a will replace this with incremental sync."""
        targets = {entity_type} if entity_type else set(EntityType)
        for et in targets:
            idx = self._index_name(et)
            if not self._client.indices.exists(index=idx):
                self._client.indices.create(index=idx, body=_index_mapping_body())
            # Bulk re-replace.
            actions = []
            for row in self._storage.iter_entries(et):
                doc = _row_to_doc(et, row)
                actions.append({
                    "_op_type": "index",
                    "_index": idx,
                    "_id": str(row.id),
                    "_source": doc,
                })
            if actions:
                helpers.bulk(self._client, actions, refresh="true")
            else:
                # Empty source — ensure the index is also empty.
                self._client.delete_by_query(
                    index=idx,
                    body={"query": {"match_all": {}}},
                    refresh=True,
                )

    # ---- SearchBackend Protocol ----

    def search(
        self,
        query: str,
        *,
        entity_types: Optional[set[EntityType]] = None,
        limit: int = 10,
        project: Optional[str] = None,
        caller: Optional[str] = None,
    ) -> list[SearchHit]:
        # v1 sync: re-index from storage every search. Slow but correct.
        # Replace with the watcher cascade in Phase 4a.
        self.reindex()

        targets = entity_types if entity_types is not None else set(EntityType)
        hits: list[SearchHit] = []
        for etype in targets:
            hits.extend(self._search_one(etype, query, limit=limit, project=project))
        hits.sort(key=lambda h: h.score, reverse=True)
        return hits[:limit]

    def search_plugin(
        self,
        scope: Any,
        query: str,
        limit: int = 10,
        *,
        caller: Optional[str] = None,
    ) -> list[str]:
        """Plugins on OpenSearch own their own indexes — SearchScope
        carries SQL-flavored metadata that doesn't translate. This
        adapter returns no plugin results in v1; plugin authors who
        want OpenSearch must register their own backend handle.
        """
        return []

    # ---- internals ----

    def _search_one(
        self,
        etype: EntityType,
        query: str,
        *,
        limit: int,
        project: Optional[str],
    ) -> list[SearchHit]:
        idx = self._index_name(etype)

        must = [{
            "multi_match": {
                "query": query,
                # Field boosts mirror the Postgres setweight A/B/C/D
                # scheme: primary 4x, secondary 2x, tertiary 1x, quat 0.5x.
                "fields": ["primary^4", "secondary^2", "tertiary", "quaternary^0.5"],
                "type": "best_fields",
            }
        }]
        filter_clauses: list[dict] = []
        # Mapping["name"]=="rules" has no project field — skip filter.
        if project is not None and _INDEX_MAPPING[etype]["name"] != "rules":
            filter_clauses.append({
                "bool": {
                    "should": [
                        {"term": {"project": project}},
                        {"bool": {"must_not": {"exists": {"field": "project"}}}},
                    ],
                    "minimum_should_match": 1,
                }
            })

        body = {
            "size": limit,
            "query": {"bool": {"must": must, "filter": filter_clauses}},
        }

        try:
            response = self._client.search(index=idx, body=body)
        except Exception:
            return []

        hits = []
        for hit in response.get("hits", {}).get("hits", []):
            src = hit["_source"]
            hits.append(SearchHit(
                entity_type=etype,
                entity_id=int(src["id"]),
                # OpenSearch _score is BM25 — already higher-better and
                # non-negative.
                score=float(hit["_score"]),
                is_pinned=bool(src.get("pinned", False)),
                is_stale=False,
                counters_snapshot={},
                row=None,
            ))
        return hits
