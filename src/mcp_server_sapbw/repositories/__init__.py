"""Repository layer: one module per metadata domain (chains, providers,
transformations, queries, hana, texts).

Repositories check the capability record before building any SQL, stamp every returned
record with provenance, and return pydantic models rather than raw rows. They contain no
tool registration and no cross-domain orchestration. Implemented from build prompt B3 on.
"""
