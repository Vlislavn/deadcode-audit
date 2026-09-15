"""Entry point: ``python -m deadcode_audit <command>``."""

from __future__ import annotations

import sys

from deadcode_audit.cli import main

if __name__ == "__main__":
    sys.exit(main())
