"""Console-script shim for the Inspector Widget MCP server.

The real MCP server implementation lives at ``host/mcp_server.py`` — a loose
script beside (not inside) the ``inspector_widget`` package, so it is not
importable as ``inspector_widget.mcp_server``.  Rather than move the file (a
later mechanical rename pass owns that), ``host/mcp_server.py`` is shipped in the
wheel as a top-level module via ``py-modules`` (see pyproject.toml
``[tool.setuptools] py-modules``).  That makes the top-level ``mcp_server``
module importable after a plain ``pip install``, so this thin wrapper simply
imports it and delegates to its ``main()``.
"""

from __future__ import annotations

import sys


def main(argv=None) -> int:
    import mcp_server  # top-level module shipped from host/mcp_server.py

    return mcp_server.main(argv)


if __name__ == "__main__":
    raise SystemExit(main())
