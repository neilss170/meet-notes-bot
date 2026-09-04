"""Allow ``python -m meetbot`` to invoke the CLI."""

from __future__ import annotations

import sys

from meetbot.cli import main

if __name__ == "__main__":
    sys.exit(main())
