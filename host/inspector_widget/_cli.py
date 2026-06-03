"""Console-script shim for the Inspector Widget CLI.

The real CLI implementation lives at ``host/cli.py`` — a loose script that sits
*beside* (not inside) the ``inspector_widget`` package, so it is not importable
as ``inspector_widget.cli``.  Rather than move the file (a later mechanical
rename pass owns that), ``host/cli.py`` is shipped in the wheel as a top-level
module via ``py-modules`` (see pyproject.toml ``[tool.setuptools] py-modules``).
That makes the top-level ``cli`` module importable after a plain ``pip install``,
so this thin wrapper simply imports it and delegates to its ``main()``.
"""

from __future__ import annotations

import sys


def main(argv=None) -> int:
    import cli  # top-level module shipped from host/cli.py

    return cli.main(argv)


if __name__ == "__main__":
    sys.exit(main())
