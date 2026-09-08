# Mac mini 本地 CosyVoice TTS API

当前链路：`HTTP /speak` → 鉴权 → FIFO → CosyVoice3 → `afplay` 播放。

实现使用 [cosyvoice.cpp](https://github.com/Lourdle/cosyvoice.cpp) v0.1.1 macOS arm64 运行时和 `Fun-CosyVoice3-0.5B-2512` Q8_0 GGUF 模型。模型、音频、日志和运行时文件均不提交 Git。

## 安装

```bash
./scripts/install.sh
```

安装内容：

- `CosyVoice3-2512_Q8_0.gguf`
- `speech_tokenizer_v3.int8.onnx`、`campplus.int8.onnx`
- 官方示例提示音并生成 `runtime/models/prompt_speech.gguf`
- macOS arm64 `cosyvoice.cpp v0.1.1`

## 常驻服务

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

查询当前可用音色：

```bash
curl http://127.0.0.1:8765/voices \
  -H "Authorization: Bearer $API_KEY"
```

当前配置在 [voices.json](./voices.json)，默认只有已经存在真实 `prompt_speech.gguf` 的 `default` 音色。业务端只传 Voice ID，不传文件路径：

```json
{"text":"欢迎进入直播间","voice":"default"}
```

添加音色：

```bash
mkdir -p voices/host_female
# 放入 voices/host_female/reference.wav
# 放入 voices/host_female/reference.txt
./scripts/voice-prepare.sh host_female
```

`reference.txt` 必须是音频中实际说出的准确文字。脚本会生成 `runtime/models/voices/host_female.gguf` 并更新 `voices.json`。Gateway 会在配置文件变化后自动注册/删除音色，不需要重启 CosyVoice 主进程。

如果没有真实第二音色样本，不注册或伪造 `host_female` / `host_male`。

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

### 本地音频 ASR 输入

`audio-ingest.py` 将本地 wav/mp3/m4a/mp4（mp4 会先抽取音轨）交给已安装的本地 ASR 后端，并输出包含 Segment 与 word timestamp 的结构化 JSON：

```bash
python3 scripts/audio-ingest.py /path/to/live.mp3 runtime/asr/live.json --model /path/to/local/model
```

支持自动选择 `mlx-whisper`、`faster-whisper`、`openai-whisper` 或 `whisper.cpp`，优先使用 `mlx-whisper`。也可以通过 `--backend` 固定后端；模型必须通过 `--model` 或 `TTS_ASR_MODEL` 指定，脚本不会隐式下载模型。未检测到本地 ASR 后端时命令会直接报错。

生成的 JSON 可直接交给重切分：

```bash
python3 scripts/timeline-resegment.py runtime/asr/live.json runtime/resegmented.json
```

Mac Apple Silicon 首测已验证 `mlx-whisper 0.4.3 + mlx 0.29.3 + whisper-large-v3-turbo`。模型来自 [mlx-community/whisper-large-v3-turbo](https://huggingface.co/mlx-community/whisper-large-v3-turbo)，本地目录为 `runtime/models/asr/large-v3-turbo/`，约 1.51 GiB，不提交 Git。项目专用环境在 `.venv-asr/`，真实运行示例：

```bash
.venv-asr/bin/python scripts/audio-ingest.py \
  /path/to/live.mp3 runtime/asr/live.json \
  --backend mlx-whisper \
  --model "$PWD/runtime/models/asr/large-v3-turbo" \
  --language zh
```

模型路径必须显式指定；脚本不会在运行时联网下载模型。

文本层入口见 [docs/timeline-speech-v1.md](docs/timeline-speech-v1.md)。它接收带时间戳的 ASR JSON，使用本机 Ollama 提取意图并生成多版本话术，保持 Segment 顺序和原节奏，输出可供后续 `/speak` 使用的模板。

Generalize 默认采用 Semantic Coverage V1：Analyze 提取语义骨架和 `semantic_points` 后统一生成完整 `candidates[]`，Rewrite 不再携带完整原文，程序执行 hard_keep、semantic coverage、similarity 和 Natural Duration 审核；旧的 slots/combination 逻辑保留为 legacy mode。详见 [docs/timeline-generalize-robustness-v1.md](docs/timeline-generalize-robustness-v1.md)。

```bash
python3 scripts/timeline-generalize.py input.json runtime/timeline.json --model qwen3:8b
```

这一阶段不会自动播放或推流；Lookahead TTS 缓存属于下一阶段。

timestamped ASR 会先进行 Sentence Reconstruction：合并连续碎片、保留 `source_segment_ids` 和原文，再使用 [docs/timeline-resegment-v1.md](docs/timeline-resegment-v1.md) 做 Natural Segmentation。长 ASR Segment 和旧 TXT 输入继续兼容原有规则，再进入 Generalize。

Generalize 的批处理恢复和失败隔离见 [docs/timeline-generalize-robustness-v1.md](docs/timeline-generalize-robustness-v1.md)。

Natural Timeline 重切分会额外保留语义边界和真实 pause，详见 [docs/timeline-resegment-v1.md](docs/timeline-resegment-v1.md)。Generalize 和 Runtime 优先使用 `speech_duration`，Pause 由 Runtime 控制，不计入 TTS 文本时长审核。

运行时入口见 [docs/timeline-runtime-v1.md](docs/timeline-runtime-v1.md)。

```bash
python3 scripts/timeline-run.py runtime/timeline.json --dry-run --lookahead 3
python3 scripts/timeline-run.py runtime/timeline.json --voice default --lookahead 3
```

## 当前限制

- API Key 保护 `/speak` 和 `/voices`；`/health` 可公开访问，只返回健康状态和队列长度，不暴露本机路径。
- FIFO 是单 worker，返回成功前必须完成合成和本机播放。
- 日志记录 job ID、来源、音色、文本长度、排队/合成/播放耗时和结果，不记录全文文本或 API Key。
- 当前模型为 `Fun-CosyVoice3-0.5B-2512` Q8_0 GGUF，默认后端 `auto`；当前 Mac mini M4 Pro 实测使用 Metal。
