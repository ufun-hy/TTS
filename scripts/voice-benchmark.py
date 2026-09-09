#!/usr/bin/env python3
"""Generate the first A/B/C comparison from explicitly reviewed local references."""

import argparse
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from voice_datasets.benchmark import run


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path, help="new output directory")
    args = parser.parse_args()
    raise SystemExit(0 if run(ROOT, args.output) else 1)
