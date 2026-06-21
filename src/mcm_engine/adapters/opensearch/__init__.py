"""OpenSearch SearchBackend adapter (MCM2-15b).

Importing this module requires ``opensearch-py`` (the ``opensearch`` extra).
"""
from .search import OpenSearchSearch

__all__ = ["OpenSearchSearch"]
