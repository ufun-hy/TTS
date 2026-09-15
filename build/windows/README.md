# Windows 构建

构建机需要 Windows、Python、PyInstaller 和 Inno Setup。运行：

```powershell
python -m pip install pyinstaller
.\build\windows\build.ps1
```

仅运行 PyInstaller 时，产物位于：

```text
dist/AI-Audio-Client.exe
```

安装 Inno Setup 后，脚本会继续生成：

```text
build/windows/output/AI-Audio-Client-Setup.exe
```

安装包包含 GUI、`config.json`、`cache/` 和 `logs/`。用户修改的 `config.json` 使用 `onlyifdoesntexist` 保留，不会被升级覆盖。

## 单机运行预览

Windows 单机版本的后台服务由 `scripts/windows-runtime.py` 管理，模型和用户数据放在安装目录之外：

```powershell
python .\scripts\windows-runtime.py start --models D:\AI-Live-Studio-Models
python .\scripts\windows-runtime.py status
python .\scripts\windows-runtime.py stop
python .\scripts\windows-runtime.py import-llm --models D:\AI-Live-Studio-Models
```

目标机器不需要 CUDA Toolkit；需要匹配的 NVIDIA Driver。启动器使用 `cosyvoice-server.exe`、Ollama standalone 和外部模型目录，缺少模型时停止并报告路径。`import-llm` 只从离线包的 Modelfile 注册 GGUF，不联网下载；可用 `--dry-run` 查看解析后的命令，不会启动进程。

目标硬件 POC：

```powershell
python .\scripts\windows-poc\run.py `
  --audio D:\samples\sample.wav `
  --asr-model D:\AI-Live-Studio-Models\asr\Qwen3-ASR-1.7B `
  --tts-url http://127.0.0.1:8766 `
  --output D:\poc-results\sample.json
```

该命令不会下载权重；目标 GPU 的真实 RTF、显存和连续推流结果必须单独记录。

## 真机 Gate 前的可靠性修复

3060 Ti 的 GPU Gate 暂停，先完成代码与无 GPU 回归验证。

- Windows PID 存活检查使用 `OpenProcess(SYNCHRONIZE)` 和零超时等待；不发送信号。访问拒绝或无法确定状态时保留 owner。
- `stop` 和启动失败后的回收仅移除确认已退出的 PID；失败记录保留在 `runtime/windows-processes.json`，`stop` 返回非零，可在处理原因后再次执行。
- 更新清单、安装包与 HTTP 重定向均要求 HTTPS；当前不开放 localhost HTTP 特例。配置不合法时不发出网络请求，也不复用上一次下载引用。
- Preview/Ollama 释放失败时，Runtime 保留原任务的验证回调和 owner，可在不重启程序的情况下重试。不会清除仍未确认退出的进程或模型。

### 重试释放失败资源

仅对 `/api/runtime/status` 返回 `state=ERROR` 且 `recovery_available=true` 的任务执行。使用状态中的 `operation` 标识，过期标识、仍在执行的任务和没有验证器的任务均拒绝恢复：

```powershell
$runtimeState = Invoke-RestMethod http://127.0.0.1:8770/api/runtime/status
$recoveryBody = @{ operation = $runtimeState.operation } | ConvertTo-Json
Invoke-RestMethod http://127.0.0.1:8770/api/runtime/recover `
  -Method Post -ContentType application/json -Body $recoveryBody
```

只有重新确认原 TTS 引擎已退出或原 Ollama 模型已卸载后，才释放文件锁并回到 IDLE；未确认返回 409 并保留错误。该接口仅允许回环请求，浏览器请求须同源。Live Session 继续使用现有停止/重置流程；未知或外部 ASR owner 不会被此接口强行清除。

### CI

Windows workflow 同时监听 `main` 和 `codex/windows-offline-runtime-v1` 的相关路径变更，并保留 `workflow_dispatch`。新增回归覆盖真实子进程持锁、停止失败保留 PID、HTTPS 降级拒绝及请求结束后的资源恢复。只有 GitHub Actions 对相应提交实际运行完成，才可报告 Windows CI 通过；本机回归和 dry-run 不替代该状态。
