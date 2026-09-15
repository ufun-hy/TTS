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
