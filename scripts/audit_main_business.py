"""Run exact unmodified main@35d92fb tests in separate processes and report each result.

This audit never edits or skips main tests. Expected failures remain failures.
"""
from __future__ import annotations
import argparse
import json
from pathlib import Path
import subprocess
import sys
import time

TESTS = (
    "test_context_variants", "test_live_session_blocks", "test_semantic_tts_blocks",
    "test_context_live", "test_first_round_preparation", "test_prepared_live_session",
    "test_live_session", "test_prohibited_reviews", "test_prohibited_speech",
    "test_text_studio", "test_tts_client", "test_tts_gateway",
)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", default="main-business-audit")
    parser.add_argument("--timeout", type=int, default=180)
    args = parser.parse_args()
    root = Path(args.output)
    root.mkdir(parents=True, exist_ok=True)
    results = []
    for name in TESTS:
        command = [sys.executable, "-m", "unittest", "-v", "tests.main_business_baseline." + name]
        started = time.monotonic()
        try:
            result = subprocess.run(command, capture_output=True, text=True, encoding="utf-8",
                                    errors="replace", timeout=args.timeout)
            code = result.returncode
            log = result.stdout + "\n" + result.stderr
            status = "PASS" if code == 0 else "FAIL"
        except subprocess.TimeoutExpired as exc:
            code, status = 124, "TIMEOUT"
            log = str(exc)
        (root / (name + ".log")).write_text(log, encoding="utf-8")
        results.append({"test_file": name + ".py", "status": status, "exit_code": code,
                        "elapsed_seconds": round(time.monotonic() - started, 2)})
        print(f"{status:7s} {name} ({results[-1]['elapsed_seconds']}s)", flush=True)
    passed = sum(r["status"] == "PASS" for r in results)
    output = {"baseline": "main@35d92fb", "total": len(results),
              "passed": passed, "failed_or_timed_out": len(results) - passed,
              "tests": results}
    (root / "summary.json").write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"MAIN BUSINESS FIDELITY: {passed}/{len(results)} files passed. See {root}/summary.json")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
