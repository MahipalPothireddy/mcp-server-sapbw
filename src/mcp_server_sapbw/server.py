"""MCP server entry point.

This module will host the FastMCP instance and register the tool / resource / prompt
surface. Per the build plan, the actual bootstrap and conventions are implemented in
build prompt B3 (task 11) — and only after the B2 capability-discovery gate has cleared.

For now this is an honest stub so that the ``mcp-server-sapbw`` console entry point
resolves. It intentionally does not start a server or touch any BW system.
"""

from __future__ import annotations

import sys

from . import __version__


def main() -> None:
    """Console entry point stub.

    The FastMCP bootstrap (stdio transport, tool registration) is delivered in
    build prompt B3. Until then, running the server is not yet supported.
    """
    sys.stderr.write(
        f"mcp-server-sapbw {__version__}: scaffold only.\n"
        "The MCP server bootstrap is implemented in build prompt B3 "
        "(after the B2 capability-discovery gate). Nothing to run yet.\n"
    )
    raise SystemExit(1)


if __name__ == "__main__":
    main()
