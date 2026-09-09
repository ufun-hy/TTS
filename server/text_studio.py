#!/usr/bin/env python3
"""Local web studio for text generalization, project persistence, and TTS preview."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

if __package__:
    from .text_studio_models import provider_command as _provider_command, list_models, validate_model, agy_prompt_command
    from .live_session import LiveSessionError, build_live_manager
else:
    from text_studio_models import provider_command as _provider_command, list_models, validate_model, agy_prompt_command
    from live_session import LiveSessionError, build_live_manager

MAX_BODY_BYTES = 16 * 1024 * 1024
MAX_PARAGRAPHS_PER_REQUEST = 2000
MAX_PARAGRAPH_CHARS = 4000
DEFAULT_BATCH_SIZE = 8
DEFAULT_TIMEOUT_SECONDS = 240
PROJECT_SCHEMA_VERSION = 1
PROJECT_ID_RE = re.compile(r"^[A-Za-z0-9_-]{8,80}$")
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


def _capture_provider_output(diagnostic: dict[str, Any], stdout: Any, stderr: Any) -> None:
    """Preserve provider output before parsing, including partial timeout output."""
    def as_text(value: Any) -> str:
        return value.decode("utf-8", errors="replace") if isinstance(value, bytes) else (value or "")
    stdout, stderr = as_text(stdout), as_text(stderr)
    Path(diagnostic["response_path"]).write_text(stdout, encoding="utf-8")
    Path(diagnostic["stderr_path"]).write_text(stderr, encoding="utf-8")
    for field in ("model", "provider"):
        match = re.search(rf"^{field}:\s*(.+)$", stderr, re.MULTILINE)
        if match:
            diagnostic["model" if field == "model" else "model_provider"] = match.group(1).strip()
    diagnostic["empty_response"] = not stdout.strip()
    diagnostic["markdown_wrapped"] = stdout.lstrip().startswith("```")


def _run_provider(provider: str, prompt: str, timeout_seconds: int,
                  diagnostic: dict[str, Any] | None = None, model: str = "") -> Any:
    command = _provider_command(provider, model)
    if provider == "agy" and command:
        command = agy_prompt_command(command, prompt)
    if not command:
        raise RuntimeError(f"provider {provider!r} is not configured")
    if not shutil.which(command[0]):
        raise RuntimeError(f"provider command not found: {command[0]}")
    env = os.environ.copy()
    env.setdefault("NO_COLOR", "1")
    try:
        proc = subprocess.run(
            command,
            input=None if provider == "agy" else prompt,
            text=True,
            capture_output=True,
            timeout=timeout_seconds,
            cwd=Path(__file__).resolve().parents[1],
            env=env,
        )
    except subprocess.TimeoutExpired as exc:
        if diagnostic is not None:
            _capture_provider_output(diagnostic, exc.stdout, exc.stderr)
        raise
    if diagnostic is not None:
        diagnostic["returncode"] = proc.returncode
        _capture_provider_output(diagnostic, proc.stdout, proc.stderr)
    if provider == "agy":
        try:
            envelope = json.loads(proc.stdout)
        except json.JSONDecodeError:
            envelope = None
        if isinstance(envelope, dict):
            if diagnostic is not None:
                diagnostic["model_provider"] = "antigravity"
                diagnostic["conversation_id"] = envelope.get("conversation_id", "")
                diagnostic["model"] = envelope.get("model") or "unknown"
            response = envelope.get("response")
            if envelope.get("status") != "SUCCESS" or proc.returncode:
                raise RuntimeError(f"agy 模型 {model or '默认'} 调用失败：{envelope.get('error') or response or proc.stderr or envelope.get('status')}")
            if not isinstance(response, str) or not response.strip():
                denied = envelope.get("denied_actions")
                reason = f"无头模式权限被拒绝：{denied}" if denied else "CLI 返回空 response"
                raise RuntimeError(f"agy 模型 {model or '默认'} 未生成结果：{reason}。{proc.stderr.strip()}")
            return _extract_json(response)
        if proc.returncode == 0:
            raise ValueError("agy 未返回有效 JSON；请使用 --output-format json")
    if provider == "gemini":
        try:
            envelope = json.loads(proc.stdout or (proc.stderr if proc.returncode else ""))
        except json.JSONDecodeError:
            envelope = None
        if isinstance(envelope, dict):
            used_models = list((envelope.get("stats") or {}).get("models", {}))
            if diagnostic is not None:
                diagnostic["models_used"] = used_models
                diagnostic["model"] = ", ".join(used_models) or "unknown"
                diagnostic["model_provider"] = "google"
            if envelope.get("error"):
                detail = envelope["error"]
                reason = detail.get("message", str(detail)) if isinstance(detail, dict) else str(detail)
                raise RuntimeError(f"Gemini 模型 {model or '默认'} 调用失败：{reason}。如未登录，请在终端运行 gemini 完成登录。")
            if proc.returncode == 0:
                response = envelope.get("response")
                if not isinstance(response, str) or not response.strip():
                    raise ValueError("Gemini CLI 未返回非空 response 字段")
                return _extract_json(response)
        elif proc.returncode == 0:
            raise ValueError("Gemini CLI 未返回有效 JSON；请使用 --output-format json")
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "provider failed").strip()
        if model:
            errors = [line.removeprefix("ERROR: ") for line in detail.splitlines() if line.startswith("ERROR:")]
            reason = errors[-1] if errors else detail[-2000:]
            raise RuntimeError(f"模型 {model} 调用失败：{reason}")
        raise RuntimeError(detail[-2000:])
    return _extract_json(proc.stdout)


def generalize_paragraphs(
    paragraphs: list[dict[str, Any]],
    provider: str,
    candidate_count: int,
    instruction: str,
    batch_size: int = DEFAULT_BATCH_SIZE,
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
    diagnostic_root: Path | None = None,
    project_id: str = "",
    paragraph_indexes: list[int] | None = None,
    model: str = "",
) -> list[dict[str, Any]]:
    if provider not in {"codex", "chatgpt", "gemini", "agy"}:
        raise ValueError("provider must be codex, chatgpt, gemini or agy")
    if not 1 <= candidate_count <= 5:
        raise ValueError("candidate_count must be between 1 and 5")
    if not paragraphs or len(paragraphs) > MAX_PARAGRAPHS_PER_REQUEST:
        raise ValueError(f"paragraphs must contain 1 to {MAX_PARAGRAPHS_PER_REQUEST} items")

    model = validate_model(model)
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

    if paragraph_indexes is not None and (
        not isinstance(paragraph_indexes, list) or len(paragraph_indexes) != len(normalized)
        or any(type(index) is not int or index < 0 for index in paragraph_indexes)
    ):
        raise ValueError("paragraph_indexes must contain one nonnegative integer per paragraph")

    results: list[dict[str, Any]] = []
    for start in range(0, len(normalized), batch_size):
        batch = normalized[start:start + batch_size]
        prompt = _build_prompt(batch, candidate_count, instruction)
        diagnostic = None
        if diagnostic_root is not None:
            run_id = uuid.uuid4().hex
            indexes = (paragraph_indexes or list(range(len(normalized))))[start:start + len(batch)]
            batch_id = f"batch-{indexes[0] // batch_size + 1:03d}"
            paths = {}
            for kind in ("request", "response"):
                directory = diagnostic_root / "debug" / kind / run_id
                directory.mkdir(parents=True, exist_ok=True)
                paths[kind] = directory
            diagnostic = {
                "project_id": project_id, "run_id": run_id, "batch_id": batch_id,
                "paragraph_start": indexes[0], "paragraph_end": indexes[-1],
                "paragraph_indexes": indexes, "paragraph_ids": [item["id"] for item in batch],
                "provider": provider, "model": "unknown", "requested_model": model, "paragraph_count": len(batch),
                "input_chars": sum(len(item["original_text"]) for item in batch),
                "prompt_chars": len(prompt), "estimated_tokens": len(prompt),
                "token_estimate_method": "rough 1 token/character; not tokenizer measurement",
                "start_time": _utc_timestamp(), "end_time": "", "status": "running", "error": "",
                "response_path": str(paths["response"] / f"{batch_id}-response.txt"),
                "stderr_path": str(paths["response"] / f"{batch_id}-stderr.txt"),
            }
            (paths["request"] / f"{batch_id}-request.json").write_text(json.dumps({
                **diagnostic, "command": (agy_prompt_command(_provider_command(provider, model), prompt)
                            if provider == "agy" else _provider_command(provider, model)), "prompt": prompt,
                "candidate_count": candidate_count, "timeout_seconds": timeout_seconds,
            }, ensure_ascii=False, indent=2), encoding="utf-8")
            log_dir = diagnostic_root / "logs"
            log_dir.mkdir(parents=True, exist_ok=True)
            log_path = log_dir / f"{run_id}-{batch_id}.json"
            log_path.write_text(json.dumps(diagnostic, ensure_ascii=False, indent=2), encoding="utf-8")
        try:
            options = {}
            if diagnostic is not None:
                options["diagnostic"] = diagnostic
            if model:
                options["model"] = model
            raw = _run_provider(provider, prompt, timeout_seconds, **options)
            validated = _validate_model_result(raw, [item["id"] for item in batch])
            if diagnostic is not None:
                diagnostic["status"] = "success"
        except Exception as exc:
            if diagnostic is not None:
                diagnostic.update(status="failed", error=str(exc), error_type=type(exc).__name__)
            raise
        finally:
            if diagnostic is not None:
                diagnostic["end_time"] = _utc_timestamp()
                log_path.write_text(json.dumps(diagnostic, ensure_ascii=False, indent=2), encoding="utf-8")
        for item in validated:
            item["candidates"] = item["candidates"][:candidate_count]
        results.extend(validated)
    return results


def _projects_root(root: Path) -> Path:
    path = root / "runtime" / "text-studio" / "projects"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _validate_project_id(project_id: str) -> str:
    if not isinstance(project_id, str) or not PROJECT_ID_RE.fullmatch(project_id):
        raise ValueError("invalid project_id")
    return project_id


def _project_file(root: Path, project_id: str) -> Path:
    project_id = _validate_project_id(project_id)
    return _projects_root(root) / project_id / "project.json"


def _utc_timestamp() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _new_project_id() -> str:
    return f"{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:8]}"


def _project_summary(project: dict[str, Any]) -> dict[str, Any]:
    paragraphs = project.get("paragraphs") if isinstance(project.get("paragraphs"), list) else []
    generated = sum(
        1
        for item in paragraphs
        if isinstance(item, dict) and isinstance(item.get("candidates"), list) and item.get("candidates")
    )
    return {
        "project_id": project.get("project_id", ""),
        "name": project.get("name", "未命名项目"),
        "source_name": project.get("source_name", ""),
        "created_at": project.get("created_at", ""),
        "updated_at": project.get("updated_at", ""),
        "paragraph_count": len(paragraphs),
        "generated_count": generated,
    }


def save_project(root: Path, payload: dict[str, Any]) -> dict[str, Any]:
    project_id = payload.get("project_id")
    if project_id:
        project_id = _validate_project_id(project_id)
    else:
        project_id = _new_project_id()

    paragraphs = payload.get("paragraphs")
    if not isinstance(paragraphs, list):
        raise ValueError("project paragraphs must be an array")
    if len(paragraphs) > MAX_PARAGRAPHS_PER_REQUEST:
        raise ValueError(f"project exceeds {MAX_PARAGRAPHS_PER_REQUEST} paragraphs")

    name = payload.get("name", "未命名项目")
    if not isinstance(name, str) or not name.strip():
        name = "未命名项目"
    name = name.strip()[:120]

    source_text = payload.get("source_text", "")
    source_name = payload.get("source_name", "")
    provider = payload.get("provider", "codex")
    model = validate_model(payload.get("model", ""))
    instruction = payload.get("instruction", "")
    risk_findings = payload.get("risk_findings", [])
    replacement_history = payload.get("replacement_history", [])
    voice = payload.get("voice", "default")
    candidate_count = payload.get("candidate_count", 3)

    if not isinstance(source_text, str) or not isinstance(source_name, str):
        raise ValueError("invalid project source")
    if not isinstance(provider, str) or not isinstance(instruction, str) or not isinstance(voice, str):
        raise ValueError("invalid project settings")
    if not isinstance(risk_findings, list) or not isinstance(replacement_history, list):
        raise ValueError("invalid text processing state")
    try:
        candidate_count = int(candidate_count)
    except (TypeError, ValueError) as exc:
        raise ValueError("invalid candidate_count") from exc
    if not 1 <= candidate_count <= 5:
        raise ValueError("candidate_count must be between 1 and 5")

    path = _project_file(root, project_id)
    created_at = _utc_timestamp()
    if path.is_file():
        try:
            existing = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(existing, dict) and isinstance(existing.get("created_at"), str):
                created_at = existing["created_at"]
        except (OSError, ValueError, json.JSONDecodeError):
            pass

    now = _utc_timestamp()
    project = {
        "schema_version": PROJECT_SCHEMA_VERSION,
        "project_id": project_id,
        "name": name,
        "source_name": source_name[:255],
        "source_text": source_text,
        "provider": provider,
        "model": model,
        "candidate_count": candidate_count,
        "voice": voice[:80],
        "instruction": instruction,
        "risk_findings": risk_findings,
        "replacement_history": replacement_history,
        "paragraphs": paragraphs,
        "created_at": created_at,
        "updated_at": now,
    }

    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name("project.json.tmp")
    temp.write_text(json.dumps(project, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temp, path)
    return project


def load_project(root: Path, project_id: str) -> dict[str, Any]:
    path = _project_file(root, project_id)
    if not path.is_file():
        raise FileNotFoundError(project_id)
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("project file must contain an object")
    return raw


def list_projects(root: Path) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    projects_root = _projects_root(root)
    for path in projects_root.glob("*/project.json"):
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, json.JSONDecodeError):
            continue
        if not isinstance(raw, dict):
            continue
        project_id = raw.get("project_id")
        if not isinstance(project_id, str) or not PROJECT_ID_RE.fullmatch(project_id):
            continue
        items.append(_project_summary(raw))
    items.sort(key=lambda item: item.get("updated_at", ""), reverse=True)
    return items


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


def make_handler(
    root: Path,
    gateway_url: str,
    audio_cache_url: str = "http://127.0.0.1:8000",
    tts_api_key: str = "",
    audio_cache_api_key: str = "",
):
    html_path = root / "web" / "text-studio.html"
    live = build_live_manager(gateway_url, audio_cache_url, tts_api_key, audio_cache_api_key)

    class Handler(BaseHTTPRequestHandler):
        server_version = "tts-text-studio/1.1"

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
                raise ValueError("request body must be between 1 byte and 16 MiB")
            body = json.loads(self.rfile.read(length))
            if not isinstance(body, dict):
                raise ValueError("request body must be a JSON object")
            return body

        def do_GET(self) -> None:  # noqa: N802
            parsed = urllib.parse.urlparse(self.path)
            path = parsed.path
            query = urllib.parse.parse_qs(parsed.query)

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
                        "gemini": provider_available("gemini"),
                        "agy": provider_available("agy"),
                        "chatgpt": provider_available("chatgpt"),
                    },
                    "tts": {
                        "ready": _tts_health(gateway_url),
                        "gateway_url": gateway_url,
                    },
                })
                return

            if path == "/api/live/status":
                self._json(200, live.status())
                return

            if path == "/api/live/voices":
                self._json(200, live.voices())
                return

            if path == "/api/models":
                provider = (query.get("provider") or ["codex"])[0]
                if provider not in {"codex", "chatgpt", "gemini", "agy"}:
                    self._json(400, {"error": "provider must be codex, chatgpt, gemini or agy"})
                    return
                self._json(200, list_models(provider, root))
                return

            if path == "/api/projects":
                self._json(200, {"projects": list_projects(root)})
                return

            if path == "/api/project":
                project_id = (query.get("project_id") or [""])[0]
                try:
                    project = load_project(root, project_id)
                except FileNotFoundError:
                    self._json(404, {"error": "project_not_found"})
                    return
                self._json(200, {"project": project})
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
                    model = validate_model(body.get("model", ""))
                    instruction = body.get("instruction", "")
                    if not isinstance(provider, str) or not isinstance(instruction, str):
                        raise ValueError("invalid provider or instruction")
                    started = time.monotonic()
                    result = generalize_paragraphs(
                        paragraphs,
                        provider=provider,
                        candidate_count=candidate_count,
                        instruction=instruction,
                        diagnostic_root=root / "runtime" / "text-studio",
                        project_id=str(body.get("project_id", "")),
                        paragraph_indexes=body.get("paragraph_indexes"),
                        model=model,
                    )
                    self._json(200, {
                        "provider": provider,
                        "model": model,
                        "candidate_count": candidate_count,
                        "paragraphs": result,
                        "elapsed_seconds": round(time.monotonic() - started, 3),
                    })
                    return

                if path == "/api/project/save":
                    project = save_project(root, body)
                    self._json(200, {
                        "project_id": project["project_id"],
                        "project": project,
                        "summary": _project_summary(project),
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

                if path == "/api/live/start":
                    result = live.start(body.get("voice", "default"), body.get("text", ""))
                    self._json(202, result)
                    return

                if path == "/api/live/pause":
                    self._json(200, live.pause())
                    return

                if path == "/api/live/resume":
                    self._json(200, live.resume())
                    return

                if path == "/api/live/stop":
                    self._json(200, live.stop())
                    return

                if path == "/api/live/reset":
                    self._json(200, live.reset())
                    return

                self._json(404, {"error": "not_found"})
            except subprocess.TimeoutExpired:
                self._json(504, {"error": "provider_timeout"})
            except LiveSessionError as exc:
                self._json(exc.status, {"error": str(exc)})
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
    parser.add_argument("--audio-cache-url", default=os.environ.get("AUDIO_CACHE_URL", "http://127.0.0.1:8000"))
    parser.add_argument("--tts-api-key", default=os.environ.get("TTS_API_KEY", ""))
    parser.add_argument("--audio-cache-api-key", default=os.environ.get("AUDIO_CACHE_API_KEY", ""))
    args = parser.parse_args()

    root = Path(__file__).resolve().parents[1]
    html_path = root / "web" / "text-studio.html"
    if not html_path.is_file():
        print(f"Missing {html_path}", file=sys.stderr)
        return 1

    _projects_root(root)
    server = StudioServer((args.host, args.port), make_handler(
        root,
        args.tts_gateway_url,
        args.audio_cache_url,
        args.tts_api_key,
        args.audio_cache_api_key,
    ))
    print(f"Text Studio: http://{args.host}:{args.port}", flush=True)
    print(f"Codex provider: {'ready' if provider_available('codex') else 'unavailable'}", flush=True)
    print(f"ChatGPT provider: {'ready' if provider_available('chatgpt') else 'not configured'}", flush=True)
    print(f"TTS gateway: {args.tts_gateway_url}", flush=True)
    print(f"Audio cache: {args.audio_cache_url}", flush=True)
    print(f"Projects: {_projects_root(root)}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
