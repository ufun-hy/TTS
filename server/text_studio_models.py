"""Model discovery and invocation options for Text Studio's local providers."""
from __future__ import annotations

import json
from contextlib import contextmanager
import os
from pathlib import Path
import re
import selectors
import shlex
import subprocess
import time
from typing import Any

DEFAULT_CODEX_COMMAND = "codex exec --skip-git-repo-check --color never -"


def validate_model(value: Any) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ValueError("model must be a string")
    value = value.strip()
    if value and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,199}", value):
        raise ValueError("模型 ID 格式无效（最多 200 字符，仅支持字母、数字、点、下划线、冒号、斜杠和连字符）")
    return value


def provider_command(provider: str, model: str = "") -> list[str] | None:
    raw = (os.environ.get("TTS_TEXT_STUDIO_CODEX_CMD", DEFAULT_CODEX_COMMAND) if provider == "codex"
           else os.environ.get("TTS_TEXT_STUDIO_AGY_CMD", "agy --mode plan --output-format json --print -") if provider == "agy"
           else os.environ.get("TTS_TEXT_STUDIO_GEMINI_CMD", "gemini --output-format json") if provider == "gemini"
           else os.environ.get("TTS_TEXT_STUDIO_CHATGPT_CMD", "") if provider == "chatgpt" else "")
    command = shlex.split(raw) if raw.strip() else None
    model = validate_model(model)
    if not model:
        return command
    if provider == "agy" and command and Path(command[0]).name == "agy":
        return agy_command(command, model)
    if provider == "gemini" and native_gemini(command):
        return gemini_command(command, model)
    if provider != "codex" or not native_codex(command):
        raise ValueError("当前 Provider 的自定义命令不支持模型选择，请选择“跟随默认配置”。模型选择需要原生 codex exec 命令。")
    # Explicit selection overrides configured command model flags, without changing other options.
    cleaned = command[:2]
    index = 2
    while index < len(command):
        arg = command[index]
        if arg == "--":
            cleaned.extend(command[index:])
            break
        if arg in {"-m", "--model"}:
            index += 2
            continue
        if arg.startswith("--model=") or (arg.startswith("-m") and len(arg) > 2):
            index += 1
            continue
        if arg in {"-c", "--config"} and index + 1 < len(command) and command[index + 1].split("=", 1)[0].strip() == "model":
            index += 2
            continue
        if arg.startswith("--config=model=") or arg.startswith("-cmodel="):
            index += 1
            continue
        cleaned.append(arg)
        index += 1
    return cleaned[:2] + ["--model", model] + cleaned[2:]


def native_codex(command: list[str] | None) -> bool:
    return bool(command and len(command) > 1 and Path(command[0]).name in {"codex", "codex.exe"} and command[1] == "exec")


def catalog_command(command: list[str], root: Path) -> tuple[list[str], Path]:
    """Keep config/model/profile/cwd overrides in discovery consistent with exec."""
    options: list[str] = []
    cwd = root
    index = 2
    while index < len(command):
        arg = command[index]
        name, separator, value = arg.partition("=")
        if not arg.startswith("--") and arg[:2] in {"-c", "-m", "-p", "-C"} and len(arg) > 2:
            name, separator, value = arg[:2], "=", arg[2:]
        if name in {"-c", "--config", "-m", "--model", "-p", "--profile", "-C", "--cd", "--enable", "--disable"}:
            if not separator:
                index += 1
                if index >= len(command):
                    raise ValueError(f"Codex 命令选项 {name} 缺少参数")
                value = command[index]
            if name in {"-C", "--cd"}:
                cwd = (root / value).resolve()
            elif name in {"-m", "--model"}:
                options.extend(["-c", "model=" + json.dumps(value)])
            else:
                options.extend([name, value])
        elif arg in {"--oss", "--local-provider", "--ignore-user-config"}:
            raise ValueError("此 Codex 命令使用了独立 Provider 或配置模式，当前无法读取匹配的模型列表；仍可跟随默认配置。")
        elif arg == "--":
            break
        index += 1
    return [command[0], *options, "app-server", "--listen", "stdio://"], cwd


@contextmanager
def _rpc_process(argv: list[str], cwd: Path, label: str):
    """Bounded JSON-lines RPC transport shared by Codex and Gemini discovery."""
    proc = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=cwd)
    deadline = time.monotonic() + 20
    buffer = b""
    stderr = b""
    request_id = 0
    with selectors.DefaultSelector() as selector:
        selector.register(proc.stdout, selectors.EVENT_READ, "stdout")
        selector.register(proc.stderr, selectors.EVENT_READ, "stderr")

        def send(message: dict[str, Any]) -> None:
            proc.stdin.write((json.dumps(message) + "\n").encode())
            proc.stdin.flush()

        def request(method: str, params: dict[str, Any]) -> dict[str, Any]:
            nonlocal request_id, buffer, stderr
            request_id += 1
            send({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params})
            while True:
                while b"\n" in buffer:
                    line, buffer = buffer.split(b"\n", 1)
                    message = json.loads(line)
                    if message.get("id") == request_id:
                        if "error" in message:
                            raise RuntimeError(message["error"].get("message", str(message["error"])))
                        return message["result"]
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError(f"读取 {label} 模型列表超时（20 秒），请检查 CLI、登录或网络后刷新。")
                events = selector.select(remaining)
                for key, _ in events:
                    chunk = os.read(key.fileobj.fileno(), 65536)
                    if not chunk:
                        selector.unregister(key.fileobj)
                        if key.data == "stdout":
                            raise RuntimeError(f"{label} 模型接口已退出：" + stderr.decode(errors="replace")[-1500:])
                    elif key.data == "stdout":
                        buffer += chunk
                    else:
                        stderr = (stderr + chunk)[-8000:]

        try:
            yield request, send
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
            for pipe in (proc.stdin, proc.stdout, proc.stderr):
                pipe.close()


def discover_codex_models(command: list[str], root: Path) -> dict[str, Any]:
    argv, cwd = catalog_command(command, root)
    with _rpc_process(argv, cwd, "Codex") as (request, send):
        request("initialize", {"clientInfo": {"name": "text_studio", "version": "1.0"}})
        send({"method": "initialized", "params": {}})
        config = request("config/read", {"cwd": str(cwd), "includeLayers": False})["config"]
        models = []
        cursor = None
        while True:
            result = request("model/list", {"limit": 100, "includeHidden": False, "cursor": cursor})
            models.extend({"id": item["model"], "name": item.get("displayName", item["model"]),
                           "description": item.get("description", ""), "is_default": item.get("isDefault", False)}
                          for item in result["data"] if not item.get("hidden") and "text" in item.get("inputModalities", ["text"]))
            cursor = result.get("nextCursor")
            if not cursor:
                break
        default_model = config.get("model") or next((item["id"] for item in models if item["is_default"]), "")
        return {"models": models, "default_model": default_model, "source": "codex app-server model/list"}


def native_gemini(command: list[str] | None) -> bool:
    return bool(command and Path(command[0]).name in {"gemini", "gemini.cmd"})


def gemini_command(command: list[str], model: str = "", *, discover: bool = False) -> list[str]:
    cleaned = [command[0]]
    index = 1
    while index < len(command):
        arg = command[index]
        if (model and arg in {"-m", "--model"}) or (discover and arg in {"-o", "--output-format"}):
            index += 2
            continue
        if model and (arg.startswith("--model=") or (arg.startswith("-m") and len(arg) > 2)):
            index += 1
            continue
        if discover and (arg.startswith("--output-format=") or (arg.startswith("-o") and len(arg) > 2)):
            index += 1
            continue
        if discover and (arg in {"-p", "--prompt", "-i", "--prompt-interactive", "--resume", "-r"} or not arg.startswith("-")):
            raise ValueError("此 Gemini 自定义命令包含会话或提示参数，无法安全读取模型列表；请选择默认命令。")
        cleaned.append(arg)
        # Preserve model option values when discovering the configured default.
        if discover and arg in {"-m", "--model"}:
            index += 1
            if index >= len(command):
                raise ValueError("Gemini --model 缺少参数")
            cleaned.append(command[index])
        index += 1
    return cleaned + (["--acp"] if discover else ["--model", model] if model else [])


def discover_gemini_models(command: list[str], root: Path) -> dict[str, Any]:
    with _rpc_process(gemini_command(command, discover=True), root, "Gemini") as (request, _):
        request("initialize", {"protocolVersion": 1, "clientCapabilities": {},
                               "clientInfo": {"name": "text-studio", "version": "1.0"}})
        try:
            result = request("session/new", {"cwd": str(root), "mcpServers": []})
        except RuntimeError as exc:
            raise RuntimeError(f"Gemini 模型读取失败：{exc}。首次使用请在终端运行 gemini 完成登录，再刷新模型列表。") from exc
        catalog = result["models"]
        return {"models": [{"id": item["modelId"], "name": item["name"],
                            "description": item.get("description") or ""}
                           for item in catalog["availableModels"]],
                "default_model": catalog["currentModelId"], "source": "gemini ACP session/new"}


def agy_command(command: list[str], model: str) -> list[str]:
    cleaned = [command[0]]
    index = 1
    while index < len(command):
        arg = command[index]
        if arg == "--model":
            index += 2
            continue
        if arg.startswith("--model="):
            index += 1
            continue
        cleaned.append(arg)
        index += 1
    # Go flag parsing stops at the positional stdin marker, so insert before all other args.
    return cleaned[:1] + ["--model", model] + cleaned[1:]


def agy_prompt_command(command: list[str], prompt: str) -> list[str]:
    """agy 1.1.27 print mode requires the prompt as an argument, not stdin '-'."""
    command = list(command)
    for index, arg in enumerate(command):
        if arg in {"--print", "-p", "--prompt"}:
            if index + 1 >= len(command) or command[index + 1] != "-":
                raise ValueError("agy 自定义命令须使用 --print - 作为 Text Studio 的 Prompt 占位符")
            command[index + 1] = prompt
            return command
    return command + ["--print", prompt]


def discover_agy_models(command: list[str], root: Path) -> dict[str, Any]:
    proc = subprocess.run([command[0], "models"], capture_output=True, text=True, cwd=root, timeout=20)
    if proc.returncode:
        raise RuntimeError("agy 模型列表读取失败：" + (proc.stderr or proc.stdout).strip()[-1500:])
    models = []
    for line in proc.stdout.splitlines():
        model_id, separator, name = line.partition("\t")
        if separator and model_id:
            models.append({"id": validate_model(model_id), "name": name.strip(), "description": "通过 Antigravity 账号调用"})
    if not models:
        raise ValueError("agy 未返回可识别的 Gemini 模型列表，请在终端运行 agy models 检查登录和网络。")
    default = ""
    settings = Path.home() / ".gemini" / "antigravity-cli" / "settings.json"
    if settings.is_file():
        configured = json.loads(settings.read_text()).get("model", "")
        default = next((item["id"] for item in models if configured in {item["id"], item["name"]}), configured)
    for index, arg in enumerate(command[1:], 1):
        if arg == "--model" and index + 1 < len(command):
            default = command[index + 1]
        elif arg.startswith("--model="):
            default = arg.split("=", 1)[1]
    return {"models": models, "default_model": default, "source": "agy models"}


def list_models(provider: str, root: Path) -> dict[str, Any]:
    result = {"provider": provider, "models": [], "default_model": "", "selection_supported": False, "error": ""}
    try:
        command = provider_command(provider)
        if provider == "agy" and command and Path(command[0]).name == "agy":
            result["selection_supported"] = True
            result.update(discover_agy_models(command, root))
            return result
        if provider == "gemini" and native_gemini(command):
            result["selection_supported"] = True
            result.update(discover_gemini_models(command, root))
            return result
        if provider != "codex" or not native_codex(command):
            result["error"] = "当前 Provider 未提供模型发现/选择接口；跟随本地命令的默认配置。"
            return result
        result["selection_supported"] = True
        result.update(discover_codex_models(command, root))
    except (OSError, ValueError, RuntimeError, TimeoutError, KeyError, subprocess.TimeoutExpired) as exc:
        result["error"] = str(exc)
    return result
