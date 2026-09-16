# Windows V1 测试安装包

GitHub Actions 在 `windows-latest` 上构建完整测试安装包：

```powershell
python -m pip install pyinstaller
choco install innosetup -y
.\build\windows\build.ps1
```

产物：

```text
build/windows/output/AI-Live-Studio-Windows-Test-Setup.exe
```

构建阶段下载并锁定可分发的 Windows 运行时：

- CPython 3.11.9 embeddable runtime
- CUDA 12.4 PyTorch 2.6.0 与固定 `qwen-asr` 依赖
- Ollama 0.34.0 standalone
- CosyVoice v0.1.3（commit `1616b12`）及 ONNX/GGML/Vulkan DLL
- FFmpeg/ffprobe n8.1

安装器只把程序写入 `C:\Program Files\AI Live Studio`。用户数据写入
`%LOCALAPPDATA%\AI-Live-Studio`，卸载和升级不包含该目录，也不包含外部模型目录。

双击 `AI Live Studio` 后，入口会执行启动检查、启动 Runtime、等待本地服务健康，最后打开
`http://127.0.0.1:8770/`。模型默认目录为 `D:\AI-Live-Studio-Models`；没有 D 盘或模型路径变化时可在启动检查中选择目录，也可从开始菜单运行 `Change Model Directory`。

单机 Runtime 的音频下载与 WinMM 播放由 `windows_playback_service.py` 后台运行，不依赖 Tkinter；旧 `windows_client.py` 仅保留给独立的旧 Audio Client 构建。

外部模型包至少需要：

```text
D:\AI-Live-Studio-Models\
├─ asr\Qwen3-ASR-1.7B\
├─ llm\Modelfile
├─ llm\ollama-store\
└─ tts\
   ├─ CosyVoice3-2512_Q8_0.gguf
   └─ voices.json
```

`voices.json` 中引用的 `prompt_speech` 文件也必须存在。安装包不会联网下载模型，不安装系统 Python、不修改 PATH、不要求 CUDA Toolkit 或独立 Ollama。

## 单机运行诊断

Windows 单机版本的后台服务由 `scripts/windows-runtime.py` 管理，模型和用户数据放在安装目录之外：

```powershell
.\scripts\windows-runtime.ps1 check -Models D:\AI-Live-Studio-Models
.\scripts\windows-runtime.ps1 status
.\scripts\windows-runtime.ps1 stop
.\scripts\windows-runtime.ps1 import-llm -Models D:\AI-Live-Studio-Models
```

`status` 会同时显示进程的 `pid`、`running`、`owned`、预期入口、监听 PID 和
`health_details`。进程身份按可执行文件与入口命令校验；陈旧 PID、PID 被复用、未跟踪监听器和
HTTP 超时会明确列在 `issues` 中。`stop` 只对身份匹配的进程执行 `taskkill`，不会按端口终止外部进程。
CosyVoice 的按需引擎端口 `8766` 会通过父 PID 归属到 TTS Gateway；若不是其子进程，也会作为未跟踪监听器报告。

目标机器不需要 CUDA Toolkit；只需要匹配的 NVIDIA Driver。TTS 默认使用 Vulkan，避免预编译 GGML CUDA 后端在 CosyVoice 上产生噪声；ASR 仍使用 PyTorch CUDA。启动器使用 `cosyvoice-server.exe`、Ollama standalone 和外部模型目录，缺少模型、运行库或端口冲突时停止并报告可操作路径。`import-llm` 只从离线包的 Modelfile 注册 GGUF，不联网下载；可用 `--dry-run` 查看解析后的命令，不会启动进程。

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

Windows workflow 同时运行 Runtime 回归、WinMM 测试、完整运行时组装、PyInstaller、Inno Setup 和 artifact upload。由于完整 CUDA PyTorch + Ollama payload 超过 Windows 单个 Setup.exe 的 4.2GB 限制，artifact 名称为 `AI-Live-Studio-Windows-Test-Setup`，包含可双击的 EXE、Inno 数据分片和 SHA256 文件；分发时须保持它们在同一目录。只有 GitHub Actions 对相应提交实际运行完成，才可报告 Windows CI 通过；本机回归和 dry-run 不替代该状态。
