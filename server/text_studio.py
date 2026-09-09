#!/usr/bin/env python3
"""Local web studio for text generalization and TTS preview."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

MAX_BODY_BYTES = 4 * 1024 * 1024
MAX_PARAGRAPHS_PER_REQUEST = 2000
MAX_PARAGRAPH_CHARS = 4000
DEFAULT_BATCH_SIZE = 8
DEFAULT_TIMEOUT_SECONDS = 240
SENTENCE_RE = re.compile(r"(?<=[。！？!?])\s*")


def split_paragraphs(text: str) -> list[dict[str, Any]]:
    normalized = text.replace("\r\n", "\n").replace("\r", "\n").strip()
    if not normalized:
        return []
    raw = [part.strip() for part in re.split(r"\n\s*\n+", normalized) if part.strip()]
    paragraphs: list[dict[str, Any]] = []
    for index, paragraph in enumerate(raw, 1):
        sentences = [part.strip() for part in SENTENCE_RE.split(paragraph) if part.strip()]
        paragraphs.append({
            "id": f"p{index:04d}",
            "index": index,
            "original_text": paragraph,
            "sentences": sentences,
        })
    return paragraphs


def _extract_json(text: str) -> Any:
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = re.sub(r"^```(?:json)?\s*", "", stripped, flags=re.IGNORECASE)
        stripped = re.sub(r"\s*```$", "", stripped)
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        decoder = json.JSONDecoder()
        for pos, char in enumerate(stripped):
            if char not in "[{":
                continue
            try:
                value, _ = decoder.raw_decode(stripped[pos:])
                return value
            except json.JSONDecodeError:
                continue
    raise ValueError("model output does not contain valid JSON")


def _validate_model_result(raw: Any, expected_ids: list[str]) -> list[dict[str, Any]]:
    if not isinstance(raw, dict) or not isinstance(raw.get("paragraphs"), list):
        raise ValueError("model output must contain paragraphs[]")
    by_id: dict[str, dict[str, Any]] = {}
    for item in raw["paragraphs"]:
        if not isinstance(item, dict):
            continue
        paragraph_id = item.get("id")
        candidates = item.get("candidates")
        if not isinstance(paragraph_id, str) or not isinstance(candidates, list):
            continue
        cleaned = [candidate.strip() for candidate in candidates if isinstance(candidate, str) and candidate.strip()]
        if cleaned:
            by_id[paragraph_id] = {"id": paragraph_id, "candidates": cleaned}
    missing = [paragraph_id for paragraph_id in expected_ids if paragraph_id not in by_id]
    if missing:
        raise ValueError(f"model output missing paragraph ids: {', '.join(missing)}")
    return [by_id[paragraph_id] for paragraph_id in expected_ids]


def _provider_command(provider: str) -> list[str] | None:
    if provider == "codex":
        raw = os.environ.get(
            "TTS_TEXT_STUDIO_CODEX_CMD",
            "codex exec --skip-git-repo-check --color never -",
        )
    elif provider == "chatgpt":
        raw = os.environ.get("TTS_TEXT_STUDIO_CHATGPT_CMD", "")
    else:
        return None
    if not raw.strip():
        return None
    return shlex.split(raw)


def provider_available(provider: str) -> bool:
    command = _provider_command(provider)
    return bool(command and shutil.which(command[0]))


def _build_prompt(paragraphs: list[dict[str, Any]], candidate_count: int, instruction: str) -> str:
    source = [{"id": item["id"], "text": item["original_text"]} for item in paragraphs]
    extra = instruction.strip()
    return f"""你是直播话术泛化助手。请直接改写下面的直播原稿段落。

每个段落生成 {candidate_count} 个可以直接朗读的完整候选版本。

必须遵守：
- 原稿是事实来源。商品、价格、数量、重量、规格、优惠条件、物流、售后、时间等事实必须保持不变。
- 不补充原稿没有的优惠、承诺、销量、认证、赠品、库存或其他事实。
- 不要总结、压缩成提纲，也不要为了拉长时长增加废话。
- 保持原段落主要信息顺序和直播口语节奏，可以调整语序和断句，让表达更自然。
- 每个候选必须是一整段可直接送入 TTS 的自然口语，不要输出占位词、候选标签或序号。
- 不要分析，不要解释。
{('- 额外要求：' + extra) if extra else ''}

只返回一个 JSON 对象，结构必须是：paragraphs 数组；每项只有 id 和 candidates。candidates 必须恰好包含 {candidate_count} 个非空字符串。

输入：
{json.dumps({'paragraphs': source}, ensure_ascii=False)}
"""


def _run_provider(provider: str, prompt: str, timeout_seconds: int) -> Any:
    command = _provider_command(provider)
    if not command:
        raise RuntimeError(f"provider {provider!r} is not configured")
    if not shutil.which(command[0]):
        raise RuntimeError(f"provider command not found: {command[0]}")
    env = os.environ.copy()
    env.setdefault("NO_COLOR", "1")
    proc = subprocess.run(
        command,
        input=prompt,
        text=True,
        capture_output=True,
        timeout=timeout_seconds,
        cwd=Path(__file__).resolve().parents[1],
        env=env,
    )
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "provider failed").strip()
        raise RuntimeError(detail[-2000:])
    return _extract_json(proc.stdout)


def generalize_paragraphs(
    paragraphs: list[dict[str, Any]],
    provider: str,
    candidate_count: int,
    instruction: str,
    batch_size: int = DEFAULT_BATCH_SIZE,
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
) -> list[dict[str, Any]]:
    if provider not in {"codex", "chatgpt"}:
        raise ValueError("provider must be codex or chatgpt")
    if not 1 <= candidate_count <= 5:
        raise ValueError("candidate_count must be between 1 and 5")
    if not paragraphs or len(paragraphs) > MAX_PARAGRAPHS_PER_REQUEST:
        raise ValueError(f"paragraphs must contain 1 to {MAX_PARAGRAPHS_PER_REQUEST} items")

    normalized: list[dict[str, Any]] = []
    for item in paragraphs:
        if not isinstance(item, dict):
            raise ValueError("each paragraph must be an object")
        paragraph_id = item.get("id")
        original_text = item.get("original_text") or item.get("text")
        if not isinstance(paragraph_id, str) or not paragraph_id.strip():
            raise ValueError("each paragraph needs an id")
        if not isinstance(original_text, str) or not original_text.strip():
            raise ValueError(f"paragraph {paragraph_id!r} needs original_text")
        if len(original_text) > MAX_PARAGRAPH_CHARS:
            raise ValueError(f"paragraph {paragraph_id!r} exceeds {MAX_PARAGRAPH_CHARS} characters")
        normalized.append({"id": paragraph_id.strip(), "original_text": original_text.strip()})

    results: list[dict[str, Any]] = []
    for start in range(0, len(normalized), batch_size):
        batch = normalized[start:start + batch_size]
        prompt = _build_prompt(batch, candidate_count, instruction)
        raw = _run_provider(provider, prompt, timeout_seconds)
        validated = _validate_model_result(raw, [item["id"] for item in batch])
        for item in validated:
            item["candidates"] = item["candidates"][:candidate_count]
        results.extend(validated)
    return results


def _read_keychain_api_key() -> str:
    key = os.environ.get("TTS_API_KEY", "")
    if key:
        return key
    security = Path("/usr/bin/security")
    if not security.exists():
        return ""
    service = os.environ.get("TTS_KEYCHAIN_SERVICE", "com.ufun.tts.api-key")
    try:
        proc = subprocess.run(
            [str(security), "find-generic-password", "-a", os.environ.get("USER", ""), "-s", service, "-w"],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return proc.stdout.strip() if proc.returncode == 0 else ""


def _tts_health(gateway_url: str) -> bool:
    try:
        with urllib.request.urlopen(f"{gateway_url.rstrip('/')}/health", timeout=2) as response:
            body = json.load(response)
        return response.status == 200 and body.get("status") == "ok"
    except (OSError, ValueError, urllib.error.URLError):
        return False


def _tts_preview(text: str, voice: str, gateway_url: str) -> dict[str, Any]:
    key = _read_keychain_api_key()
    if not key:
        raise RuntimeError("TTS API key is not available")
    payload = json.dumps({"text": text, "voice": voice}, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        f"{gateway_url.rstrip('/')}/speak",
        data=payload,
        headers={
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=900) as response:
            return json.load(response)
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(detail or f"TTS HTTP {exc.code}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(str(exc)) from exc


class StudioServer(ThreadingHTTPServer):
    daemon_threads = True


def make_handler(root: Path, gateway_url: str):
    html_path = root / "web" / "text-studio.html"

    class Handler(BaseHTTPRequestHandler):
        server_version = "tts-text-studio/1.0"

        def _json(self, status: int, body: dict[str, Any]) -> None:
            encoded = json.dumps(body, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(encoded)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(encoded)

        def _read_json(self) -> dict[str, Any]:
            length = int(self.headers.get("Content-Length", "0"))
            if length <= 0 or length > MAX_BODY_BYTES:
                raise ValueError("request body must be between 1 byte and 4 MiB")
            body = json.loads(self.rfile.read(length))
            if not isinstance(body, dict):
                raise ValueError("request body must be a JSON object")
            return body

        def do_GET(self) -> None:  # noqa: N802
            path = self.path.split("?", 1)[0]
            if path in {"/", "/index.html"}:
                try:
                    body = html_path.read_bytes()
                except OSError as exc:
                    self._json(500, {"error": str(exc)})
                    return
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)
                return
            if path == "/api/health":
                self._json(200, {
                    "status": "ok",
                    "providers": {
                        "codex": provider_available("codex"),
                        "chatgpt": provider_available("chatgpt"),
                    },
                    "tts": {
                        "ready": _tts_health(gateway_url),
                        "gateway_url": gateway_url,
                    },
                })
                return
            self._json(404, {"error": "not_found"})

        def do_POST(self) -> None:  # noqa: N802
            path = self.path.split("?", 1)[0]
            try:
                body = self._read_json()
                if path == "/api/parse":
                    text = body.get("text")
                    if not isinstance(text, str):
                        raise ValueError("text must be a string")
                    paragraphs = split_paragraphs(text)
                    self._json(200, {
                        "paragraphs": paragraphs,
                        "paragraph_count": len(paragraphs),
                        "character_count": len(text),
                    })
                    return
                if path == "/api/generalize":
                    paragraphs = body.get("paragraphs")
                    if not isinstance(paragraphs, list):
                        raise ValueError("paragraphs must be an array")
                    provider = body.get("provider", "codex")
                    candidate_count = int(body.get("candidate_count", 3))
                    instruction = body.get("instruction", "")
                    if not isinstance(provider, str) or not isinstance(instruction, str):
                        raise ValueError("invalid provider or instruction")
                    started = time.monotonic()
                    result = generalize_paragraphs(
                        paragraphs,
                        provider=provider,
                        candidate_count=candidate_count,
                        instruction=instruction,
                    )
                    self._json(200, {
                        "provider": provider,
                        "candidate_count": candidate_count,
                        "paragraphs": result,
                        "elapsed_seconds": round(time.monotonic() - started, 3),
                    })
                    return
                if path == "/api/tts/preview":
                    text = body.get("text")
                    voice = body.get("voice", "default")
                    if not isinstance(text, str) or not text.strip():
                        raise ValueError("text is required")
                    if not isinstance(voice, str) or not voice.strip():
                        raise ValueError("voice is required")
                    result = _tts_preview(text.strip(), voice.strip(), gateway_url)
                    self._json(200, result)
                    return
                self._json(404, {"error": "not_found"})
            except subprocess.TimeoutExpired:
                self._json(504, {"error": "provider_timeout"})
            except (ValueError, TypeError, json.JSONDecodeError) as exc:
                self._json(400, {"error": str(exc)})
            except RuntimeError as exc:
                self._json(502, {"error": str(exc)})
            except Exception as exc:
                self._json(500, {"error": str(exc)})

        def log_message(self, fmt: str, *args: Any) -> None:
            print(f"text-studio {self.address_string()} - {fmt % args}", flush=True)

    return Handler


def main() -> int:
    parser = argparse.ArgumentParser(description="Local text generalization web studio")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8770)
    parser.add_argument("--tts-gateway-url", default=os.environ.get("TTS_GATEWAY_URL", "http://127.0.0.1:8765"))
    args = parser.parse_args()

    root = Path(__file__).resolve().parents[1]
    html_path = root / "web" / "text-studio.html"
    if not html_path.is_file():
        print(f"Missing {html_path}", file=sys.stderr)
        return 1

    server = StudioServer((args.host, args.port), make_handler(root, args.tts_gateway_url))
    print(f"Text Studio: http://{args.host}:{args.port}", flush=True)
    print(f"Codex provider: {'ready' if provider_available('codex') else 'unavailable'}", flush=True)
    print(f"ChatGPT provider: {'ready' if provider_available('chatgpt') else 'not configured'}", flush=True)
    print(f"TTS gateway: {args.tts_gateway_url}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
