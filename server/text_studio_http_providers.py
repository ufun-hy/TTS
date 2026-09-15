"""Small HTTP adapters for local Ollama and OpenAI-compatible text models."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any
from urllib import error, request

try:
    from local_runtime.settings import SecretStoreError, secret_store
except ImportError:  # pragma: no cover - compatibility for direct script imports
    SecretStoreError = RuntimeError
    secret_store = None


OLLAMA_DEFAULT_URL = "http://127.0.0.1:11435"
OLLAMA_DEFAULT_MODEL = "qwen3:8b"


def _settings(root: Path) -> dict[str, Any]:
    data_root = Path(os.environ.get("AI_LIVE_STUDIO_DATA", str(root / "runtime"))).expanduser()
    path = data_root / "config" / "models.json"
    if not path.is_file():
        path = root / "runtime" / "text-studio" / "models.json"
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def provider_config(provider: str, root: Path) -> dict[str, str]:
    configured = _settings(root).get(provider, {})
    configured = configured if isinstance(configured, dict) else {}
    if provider == "ollama":
        return {
            "base_url": os.environ.get("TTS_OLLAMA_URL", str(configured.get("base_url", OLLAMA_DEFAULT_URL))).strip(),
            "model": os.environ.get("TTS_OLLAMA_MODEL", str(configured.get("model", OLLAMA_DEFAULT_MODEL))).strip(),
            "api_key": "",
        }
    return {
        "base_url": os.environ.get("TTS_OPENAI_COMPATIBLE_URL", str(configured.get("base_url", ""))).strip(),
        "model": os.environ.get("TTS_OPENAI_COMPATIBLE_MODEL", str(configured.get("model", ""))).strip(),
        "api_key": _api_key(root),
    }


def _api_key(root: Path) -> str:
    value = os.environ.get("TTS_OPENAI_COMPATIBLE_API_KEY", "")
    if value or secret_store is None:
        return value
    data_root = Path(os.environ.get("AI_LIVE_STUDIO_DATA", str(root / "runtime"))).expanduser()
    try:
        return secret_store(data_root).get()
    except (OSError, SecretStoreError):
        return ""


def _endpoint(base_url: str, suffix: str) -> str:
    base_url = base_url.rstrip("/")
    if base_url.endswith(suffix):
        return base_url
    return base_url + suffix


def _post_json(url: str, payload: dict[str, Any], timeout: int, api_key: str = "") -> dict[str, Any]:
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    try:
        with request.urlopen(request.Request(url, data=body, headers=headers, method="POST"), timeout=timeout) as response:
            decoded = json.load(response)
    except error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP {exc.code}: {detail[-1200:]}") from exc
    except (OSError, error.URLError) as exc:
        raise RuntimeError(str(exc)) from exc
    if not isinstance(decoded, dict):
        raise ValueError("provider response must be a JSON object")
    return decoded


def run_http_provider(provider: str, prompt: str, model: str, root: Path, timeout: int = 240) -> tuple[str, str]:
    config = provider_config(provider, root)
    model = model.strip() or config["model"]
    if not model:
        raise ValueError(f"{provider} model is not configured")
    if provider == "ollama":
        response = _post_json(_endpoint(config["base_url"], "/api/chat"), {
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "stream": False,
            "format": "json",
            "think": False,
            "keep_alive": "5m",
            "options": {"num_ctx": 4096, "num_predict": 4096, "temperature": 0.3},
        }, timeout)
        message = response.get("message")
        content = message.get("content") if isinstance(message, dict) else None
    else:
        if not config["base_url"]:
            raise ValueError("openai_compatible base URL is not configured")
        response = _post_json(_endpoint(config["base_url"], "/chat/completions"), {
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "stream": False,
            "temperature": 0.3,
        }, timeout, config["api_key"])
        choices = response.get("choices")
        message = choices[0].get("message") if isinstance(choices, list) and choices else None
        content = message.get("content") if isinstance(message, dict) else None
    if not isinstance(content, str) or not content.strip():
        raise ValueError(f"{provider} returned an empty response")
    return content, model


def unload_ollama(root: Path, model: str = "", timeout: int = 10) -> bool:
    """Ask Ollama to unload and verify /api/ps no longer lists the model."""
    config = provider_config("ollama", root)
    model = model.strip() or config["model"]
    if not model:
        return True
    try:
        _post_json(_endpoint(config["base_url"], "/api/generate"), {
            "model": model, "prompt": "", "stream": False, "keep_alive": 0,
        }, timeout)
        with request.urlopen(_endpoint(config["base_url"], "/api/ps"), timeout=timeout) as response:
            value = json.load(response)
    except (OSError, ValueError, error.URLError, RuntimeError):
        return False
    models = value.get("models", []) if isinstance(value, dict) else []
    return not any(isinstance(item, dict) and item.get("name") == model for item in models)


def unload_ollama_url(base_url: str, model: str, timeout: int = 10) -> bool:
    """Testable variant used by the runtime release path."""
    if not model:
        return True
    try:
        _post_json(_endpoint(base_url, "/api/generate"), {
            "model": model, "prompt": "", "stream": False, "keep_alive": 0,
        }, timeout)
        with request.urlopen(_endpoint(base_url, "/api/ps"), timeout=timeout) as response:
            value = json.load(response)
    except (OSError, ValueError, error.URLError, RuntimeError):
        return False
    models = value.get("models", []) if isinstance(value, dict) else []
    return not any(isinstance(item, dict) and item.get("name") == model for item in models)


def list_ollama_models(root: Path, timeout: int = 5) -> dict[str, Any]:
    config = provider_config("ollama", root)
    try:
        with request.urlopen(_endpoint(config["base_url"], "/api/tags"), timeout=timeout) as response:
            value = json.load(response)
        models = value.get("models", []) if isinstance(value, dict) else []
        entries = [{"id": item["name"], "name": item["name"], "description": "本机 Ollama 模型"}
                   for item in models if isinstance(item, dict) and isinstance(item.get("name"), str)]
        default = config["model"] if any(item["id"] == config["model"] for item in entries) else ""
        return {"models": entries, "default_model": default, "source": "Ollama /api/tags", "selection_supported": True, "error": ""}
    except (OSError, ValueError, error.URLError) as exc:
        return {"models": [], "default_model": config["model"], "source": "Ollama /api/tags", "selection_supported": True, "error": str(exc)}
