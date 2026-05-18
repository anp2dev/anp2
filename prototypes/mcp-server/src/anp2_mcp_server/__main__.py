"""`python -m anp2_mcp_server` entry point.

Builds the FastMCP server, configures stderr logging (stdout is reserved
for MCP JSON-RPC traffic (JP-redacted) see design doc (JP-redacted)6.5), and runs the stdio loop.
"""

from __future__ import annotations

import logging
import sys

from .server import build_server


def main() -> None:
    # CRITICAL: never log to stdout (JP-redacted) it is the MCP JSON-RPC channel.
    logging.basicConfig(
        stream=sys.stderr,
        level=logging.INFO,
        format="[anp2-mcp] %(asctime)s %(levelname)s %(message)s",
    )

    mcp = build_server()
    # FastMCP.run() defaults to stdio transport, which is what Claude Code /
    # Claude Desktop launch us with.
    mcp.run()


if __name__ == "__main__":
    main()
