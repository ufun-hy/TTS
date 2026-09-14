#!/usr/bin/env python3
"""CLI entrypoint for the managed auxiliary services."""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from server.stack_services import main

if __name__ == '__main__':
    raise SystemExit(main())
