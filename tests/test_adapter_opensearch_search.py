"""MCM2-15b: OpenSearch SearchBackend conformance.

OpenSearch is the demanding case — vendor-specific query DSL, vendor-
specific index types, no SQL. The same SearchConformance suite still
applies; the adapter handles the translation.

Skipped automatically when OpenSearch isn't reachable at
MCM_TEST_OPENSEARCH_URL (default points at the docker-compose service
on 127.0.0.1:59200).
"""
from __future__ import annotations

import os

import pytest

from mcm_engine.testing.conformance import SearchConformance

DEFAULT_OS_URL = "http://127.0.0.1:59200"
TEST_OS_URL = os.environ.get("MCM_TEST_OPENSEARCH_URL", DEFAULT_OS_URL)


def _opensearch_available() -> bool:
    try:
        import opensearchpy  # noqa: F401
    except ImportError:
        return False
    try:
        import urllib.request
        with urllib.request.urlopen(f"{TEST_OS_URL}/_cluster/health", timeout=2) as r:
            return r.status == 200
    except Exception:
        return False


pytestmark = pytest.mark.skipif(
    not _opensearch_available(),
    reason=(
        f"OpenSearch not reachable at {TEST_OS_URL} "
        "(start with `docker compose -f tests/docker-compose.yml up -d opensearch` "
        "and `uv pip install -e '.[opensearch]'`)."
    ),
)


class TestOpenSearch(SearchConformance):
    @pytest.fixture
    def _shared(self, tmp_path):
        from mcm_engine.adapters.opensearch.search import OpenSearchSearch
        from mcm_engine.adapters.sqlite.storage import SqliteStorage

        # SqliteStorage holds the entities; OpenSearch indexes them.
        # Namespace the index per-test so isolation is automatic.
        storage = SqliteStorage(db_path=str(tmp_path / "os-search.db"))
        storage.ensure_schema()
        index_prefix = f"mcm-test-{tmp_path.name}-"
        search = OpenSearchSearch(
            url=TEST_OS_URL,
            index_prefix=index_prefix,
            storage=storage,
        )
        search.reset_indexes()
        return storage, search

    @pytest.fixture
    def storage(self, _shared):
        return _shared[0]

    @pytest.fixture
    def search(self, _shared):
        return _shared[1]
