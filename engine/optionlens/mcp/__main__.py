"""`python -m optionlens.mcp`: the OptionLens MCP server over stdio (optionlens/mcp/server.py)."""
import sys

from .server import main

sys.exit(main())
