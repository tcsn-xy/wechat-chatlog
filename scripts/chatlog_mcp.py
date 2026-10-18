#!/usr/bin/env python3
"""Start the read-only chatlog MCP server over stdio (no network listener)."""
from chatlog_core.mcp_server import main

if __name__ == '__main__':
    main()
