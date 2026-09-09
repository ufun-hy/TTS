#!/usr/bin/env python3
"""Export manually reviewed natural speech without a duration or count quota."""

import argparse
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from voice_datasets.review import export_review


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("speaker_dir", type=Path)
    parser.add_argument("alignment", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    manifest = export_review(args.speaker_dir, args.alignment, args.output)
    print(f"Exported {len(manifest)} fully reviewed utterances to {args.output}")


if __name__ == "__main__":
    main()
