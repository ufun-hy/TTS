# Mac mini 本地 CosyVoice TTS API

当前链路：`HTTP /speak` → 鉴权 → FIFO → CosyVoice3 → `afplay` 播放。

实现使用 [cosyvoice.cpp](https://github.com/Lourdle/cosyvoice.cpp) v0.1.3 macOS arm64 运行时和 `Fun-CosyVoice3-0.5B-2512` Q8_0 GGUF 模型。模型、音频、日志和运行时文件均不提交 Git。

## 安装

```bash
./scripts/install.sh
```

安装内容：

- `CosyVoice3-2512_Q8_0.gguf`
- `speech_tokenizer_v3.int8.onnx`、`campplus.int8.onnx`
- 官方示例提示音并生成 `runtime/models/prompt_speech.gguf`
- macOS arm64 `cosyvoice.cpp v0.1.3`（会自动替换旧版 v0.1.1）

## 常驻服务

整套本地应用的手动启动与后台常驻说明见 [本地服务管理](docs/stack-services.md)，包含 Text Studio、音频缓存和录音转文稿。

首次安装 launchd LaunchAgent，并在 macOS 登录后自动启动：

```bash
./scripts/service-install.sh
```

API Key 会生成并保存到 macOS Keychain，不写入 Git、plist、README 或日志。

管理服务：

```bash
./scripts/service-status.sh
./scripts/service-start.sh
./scripts/service-stop.sh
./scripts/service-restart.sh
./scripts/service-uninstall.sh
```

服务日志：`runtime/logs/service.log`、`runtime/logs/service.error.log`、`runtime/logs/cosyvoice-server.log`。

如果手工运行 `./start.sh`，脚本会从 Keychain 读取 API Key；也可以临时设置 `TTS_API_KEY` 环境变量。

## 健康检查

```bash
curl http://127.0.0.1:8765/health
```

模型未加载或播放 worker 未运行时返回 503，不会伪造 200：

```json
{"status":"ok","tts":"ready","queue":"ready","queue_depth":0}
```

## 本地 / 局域网 API

Gateway 默认监听 `0.0.0.0:8765`，CosyVoice 引擎只监听 `127.0.0.1:8766`。`8765` 不应通过路由器端口映射直接暴露公网。

读取本机 API Key 并请求：

```bash
API_KEY="$(security find-generic-password -a "$USER" -s com.ufun.tts.api-key -w)"
curl -X POST http://127.0.0.1:8765/speak \
  -H "Authorization: Bearer $API_KEY" \
  -H 'Content-Type: application/json' \
  -d '{"text":"欢迎进入直播间，这是接口测试。","voice":"default"}'
```

局域网设备把地址替换为 Mac mini 的 LAN 地址，例如：

```bash
curl -X POST http://192.168.3.92:8765/speak \
  -H "Authorization: Bearer $API_KEY" \
  -H 'Content-Type: application/json' \
  -d '{"text":"这是局域网语音测试。","voice":"default"}'
```

未携带或携带错误 API Key 返回 401；不存在音色返回 404 `voice_not_found`；超长文本、空文本、错误 Content-Type 返回 4xx。默认限流为每分钟 30 个请求，可用 `TTS_RATE_LIMIT_PER_MINUTE` 调整。

## 音色

三位授权主播的数据整理、对齐及 Zero-shot 比较规范见 [docs/voice-datasets.md](docs/voice-datasets.md)。独立数据集保存在 `runtime/voice-datasets/`，按实际有效素材量整理，不设固定总时长；固定测试话术保存在 `config/voice-benchmark-texts.json`。

查询当前可用音色：

```bash
curl http://127.0.0.1:8765/voices \
  -H "Authorization: Bearer $API_KEY"
```

当前配置在 [voices.json](./voices.json)，已注册 `default`、经确认的 `speaker_a`、`speaker_b`、`speaker_c`，以及石榴女声 `shiliu_1`（显示名“石榴1”）。业务端只传 Voice ID，不传文件路径：

```json
{"text":"欢迎进入直播间","voice":"speaker_c"}
```

添加音色：

```bash
mkdir -p voices/host_female
# 放入 voices/host_female/reference.wav
# 放入 voices/host_female/reference.txt
./scripts/voice-prepare.sh host_female
```

`reference.txt` 必须是音频中实际说出的准确文字。脚本会生成 `runtime/models/voices/host_female.gguf` 并更新 `voices.json`。Gateway 会在配置文件变化后自动注册/删除音色，不需要重启 CosyVoice 主进程。

没有真实且已确认的 Reference 时，不注册或伪造音色。当前 `speaker_c` 的 Reference 和运行时提示音来自 `runtime/voice-datasets/`，这些运行数据不提交 Git；在另一台机器部署时需重新准备对应运行时文件。

## Tailscale Funnel 公网 HTTPS

本项目只使用 Tailscale Funnel，不做路由器端口映射。先安装官方 macOS Tailscale 客户端并完成登录；当前机器如果尚未安装，`scripts/funnel-enable.sh` 会明确报错，不会伪造公网状态。

启用、停用和查看状态：

```bash
./scripts/funnel-enable.sh
./scripts/funnel-status.sh
./scripts/funnel-disable.sh
```

当前官方 CLI 使用 `tailscale funnel --bg 8765`，后台配置会在 Tailscale/设备重启后恢复。真实公网地址以 `funnel-status.sh` 输出为准。

本次实际启用的公网地址：

```text
https://ufunmac-mini.tail352fe1.ts.net
```

目标链路：

```text
公网 HTTPS Tunnel → 127.0.0.1:8765 → API Key → Gateway
```

公网请求必须继续携带 API Key。禁止路由器端口映射或直接暴露 8765。

软件端调用约定见 [docs/software-api.md](docs/software-api.md)，示例配置见 [config/tts.example.json](config/tts.example.json)。

## 直播话术时间轴 V1

### 时间轴 ASR JSON 输入

时间轴仍接受已准备好的带时间戳 ASR JSON，可直接交给重切分：

```bash
python3 scripts/timeline-resegment.py runtime/asr/live.json runtime/resegmented.json
```

项目内录音转文字统一使用 Qwen3-ASR-1.7B；时间轴模块继续兼容已有 ASR JSON 和旧 TXT 输入。

### 录音转文稿 V1

独立的录音转文稿页面支持 wav/mp3/m4a/mp4，固定使用本地 `Qwen3-ASR-1.7B + Apple Silicon MLX`，只输出轻度整理后的连续中文文稿，不显示时间戳或 ASR JSON：

```bash
./scripts/recording-transcript-start.sh
# 打开 http://127.0.0.1:8771
```

详见 [docs/recording-transcript-v1.md](docs/recording-transcript-v1.md)。清理结果会持久化，并可直接接续到 Text Studio；不改变 Semantic Coverage、Timeline Runtime、TTS Gateway、Audio Cache 或 Windows Client。

文本层入口见 [docs/timeline-speech-v1.md](docs/timeline-speech-v1.md)。它接收带时间戳的 ASR JSON，使用本机 Ollama 提取意图并生成多版本话术，保持 Segment 顺序和原节奏，输出可供后续 `/speak` 使用的模板。

Generalize 默认采用 Semantic Coverage V1：Analyze 提取语义骨架和 `semantic_points` 后统一生成完整 `candidates[]`，Rewrite 不再携带完整原文，程序执行 hard_keep、semantic coverage、similarity 和 Natural Duration 审核；旧的 slots/combination 逻辑保留为 legacy mode。详见 [docs/timeline-generalize-robustness-v1.md](docs/timeline-generalize-robustness-v1.md)。

```bash
python3 scripts/timeline-generalize.py input.json runtime/timeline.json --model qwen3:8b
```

时间轴运行仍不会自动播放或推流；局域网音频缓存传输见下节。

timestamped ASR 会先进行 Sentence Reconstruction：合并连续碎片、保留 `source_segment_ids` 和原文，再使用 [docs/timeline-resegment-v1.md](docs/timeline-resegment-v1.md) 做 Natural Segmentation。长 ASR Segment 和旧 TXT 输入继续兼容原有规则，再进入 Generalize。

Generalize 的批处理恢复和失败隔离见 [docs/timeline-generalize-robustness-v1.md](docs/timeline-generalize-robustness-v1.md)。

Natural Timeline 重切分会额外保留语义边界和真实 pause，详见 [docs/timeline-resegment-v1.md](docs/timeline-resegment-v1.md)。Generalize 和 Runtime 优先使用 `speech_duration`，Pause 由 Runtime 控制，不计入 TTS 文本时长审核。

运行时入口见 [docs/timeline-runtime-v1.md](docs/timeline-runtime-v1.md)。

```bash
python3 scripts/timeline-run.py runtime/timeline.json --dry-run --lookahead 3
python3 scripts/timeline-run.py runtime/timeline.json --voice default --lookahead 3
```

## 局域网 AI 音频缓存传输 V1

缓存服务和 Python 拉取客户端已独立于播放链路，负责 TTS 音频入队、WAV 处理、顺序拉取和 ACK 消费确认。详细 API、状态目录、断线恢复和测试方式见 [docs/audio-cache-v1.md](docs/audio-cache-v1.md)。

启动现有 TTS Gateway 后，在 AI 机器启动缓存服务：

```bash
export TTS_API_KEY="..."
python3 scripts/audio-cache-server.py --host 0.0.0.0 --port 8000
```

直播端修改 `config/audio-client.example.json` 后运行：

```bash
python3 scripts/audio-client.py --config config/audio-client.example.json
```

该阶段不接入播放、虚拟声卡、OBS 或直播平台。

## Windows Legacy Audio Client

Windows 图形客户端入口仍为 `windows_client.py`，作为可复用的播放组件保留。Windows V1 测试机应使用下方完整的 `AI Live Studio` 安装包；旧 `AI-Audio-Client-Setup.exe` 不再是 V1 主要交付物。

Windows 连续播放层见 [docs/windows-playback-v1.md](docs/windows-playback-v1.md)：传输完成状态和本地 `cached/playing/played` 状态分离，下载与播放线程独立，支持顺序播放、暂停、继续和停止；语速、音量在 Mac 入缓存前完成。

## Live Session V1

Text Studio 页面内置 AI 直播控制区：选择可用声音、播放速度和音量，在原稿完成泛化并确认/编辑候选后点击“开始智播”，服务会直接使用当前项目的最终话术单元文本调用 TTS Gateway，由 Mac Audio Processor 生成最终 WAV 后写入 Audio Cache，Windows 客户端自动拉取。启动 Text Studio 时可用 `AUDIO_CACHE_URL` 和 `AUDIO_CACHE_API_KEY` 指定缓存服务。

CosyVoice 默认输出的 Float32 WAV 会在 Windows MCI 播放前按需转换为 PCM16，原始文件保留，兼容缓存与失败片段恢复说明见 [Windows WAV 格式兼容](docs/windows-wav-compatibility.md)。

```bash
export AUDIO_CACHE_URL="http://127.0.0.1:8000"
./scripts/text-studio-start.sh
```

状态接口为 `/api/live/status`；另有暂停/继续、停止和重置接口。停止只停止后续生成并保留已有缓存，重置只清理当前 Session 标记的缓存项。直播连续播放问题的实测原因与处理选项见 [智播连续播放问题现状](docs/live-playback-diagnosis.md)。

## 当前限制

- API Key 保护 `/speak` 和 `/voices`；`/health` 可公开访问，只返回健康状态和队列长度，不暴露本机路径。
- FIFO 是单 worker，返回成功前必须完成合成和本机播放。
- 日志记录 job ID、来源、音色、文本长度、排队/合成/播放耗时和结果，不记录全文文本或 API Key。
- 当前模型为 `Fun-CosyVoice3-0.5B-2512` Q8_0 GGUF，默认后端为更稳定的 `cpu`；如需实验 Metal，可设置 `COSYVOICE_BACKEND=auto`。

Text Studio 支持接续已有清洗版项目、按完整句子和话题整理话术单元、统一全部候选事实，以及临时/正式项目分离。用法与限制见 [Text Studio 验收说明](docs/text-studio-facts-restore.md#本轮验收候选导入与项目保存)。

Mac 运行链路的核查证据、已知环境冲突与可靠性修复任务见
[Mac 运行链路可靠性任务](docs/mac-runtime-reliability-v1-task.md)。

## 待执行的开发任务

- [Mac 当前链路核查与可靠性修复](docs/mac-runtime-reliability-v1-task.md)：包含 2026-09-15 的只读运行快照、隔离复现和修复边界。
- [Windows 单机离线版 V1](docs/windows-offline-runtime-v1-task.md)：面向 i5-12400、RTX 3060 Ti 8GB、32GB 内存，包含 GPU POC、阶段调度、播放、打包和更新验收。

以上是交给执行智能体的任务文档，不代表相关改造已经完成或目标硬件已经通过验收。

当前 Windows 分支已包含：单 GPU 文件锁与运行状态、Qwen3-ASR CUDA worker、CosyVoice Vulkan TTS、Ollama/兼容 Chat Completions provider、DPAPI 设置接口、严格 Session 播放、Windows 启动器、启动检查和更新清单校验。Windows 测试安装器由 GitHub Actions 组装固定 Python/PyTorch/qwen-asr、Ollama standalone、CosyVoice、FFmpeg 和必要 DLL；模型仍由外部模型包提供。

Windows 单机模式由 TTS Gateway 内的 `ManagedEngine` 作为唯一 CosyVoice 进程生命周期来源；启动器不再另起独立 CosyVoice 进程。GPU owner 只在引擎/worker 真实退出并确认释放后归还，停止失败会保持 `ERROR`/owner 状态。

Windows 测试安装包入口为 `AI Live Studio`，artifact 为 `AI-Live-Studio-Windows-Test-Setup.exe`。安装到 `C:\Program Files\AI Live Studio`，用户数据在 `%LOCALAPPDATA%\AI-Live-Studio`，默认模型目录为 `D:\AI-Live-Studio-Models`；没有 D 盘时可从启动检查或开始菜单 `Change Model Directory` 选择其他盘符。安装包不下载模型、不安装系统 Python、不需要 CUDA Toolkit 或独立 Ollama。真实 Windows 电脑启动、模型识别、Vulkan DLL、ASR→Ollama→CosyVoice 显存切换仍需下一轮验收。
