"""mcp-server-sapbw: read-only MCP server for SAP BW-on-HANA metadata.

A system-agnostic Model Context Protocol server that exposes the metadata of any
BW-on-HANA system (via a named connection profile) as tools, resources, and prompts.

The package is layered (see .kiro/specs/mcp-server-sapbw/design.md):

    server.py      MCP surface (FastMCP tools / resources / prompts)
    services/      lineage, routine parser, latency, descriptions, docgen, analyzers
    repositories/  one module per metadata domain
    core/          profiles, connection pool, capability resolver, SQL dialect, cache
    models/        pydantic models (Provenance, Description, ...)
    connectors/    pluggable external-BI (Tableau / BOBJ) connectors

This is the scaffold produced by build prompt B0. No BW logic is implemented yet.
"""

__version__ = "0.1.0"
