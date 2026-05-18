"""ANP2 MCP server (JP-redacted) exposes the ANP2 network to MCP-compatible clients.

Run via:

    python -m anp2_mcp_server

or, after install:

    anp2-mcp-server

See README.md for the .mcp.json / claude_desktop_config.json stanza.
"""

from .server import build_server

__version__ = "0.1.0"
__all__ = ["build_server"]
