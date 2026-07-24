"""Core layer: profile manager, read-only connection pool, capability resolver,
SQL dialect, and metadata cache.

This is the only layer that opens connections and builds SQL. Read-only enforcement
and secret scrubbing live here so a release quirk or a write attempt can never leak
into higher layers. Implemented in build prompt B1.
"""
