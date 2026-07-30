"""Launcher for the MCP server.

Hosts vary in what they preserve when spawning a stdio MCP server. Microsoft
Scout, for one, normalizes the server entry and drops `cwd` and `env`, so
`python -m hybrid_routing.mcp_server` fails with ModuleNotFoundError: the
package is never on sys.path.

This script exists to be launched by absolute path, with no cwd and no
environment set up:

    python "<repo>/mcp_launcher.py"

Python puts a directly-executed script's own directory on sys.path[0], and
this file sits at the repo root, so importing the package just works. Nothing
here depends on the working directory or on the package being installed.

If you would rather install the package (`pip install -e .`), then
`python -m hybrid_routing.mcp_server` works too and this launcher is optional.
"""

from __future__ import annotations

import sys
from pathlib import Path

# Belt and braces: sys.path[0] is already this directory when run as a script,
# but not when this file is imported, so make the root explicit either way.
_ROOT = str(Path(__file__).resolve().parent)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from hybrid_routing.mcp_server import serve  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(serve())
