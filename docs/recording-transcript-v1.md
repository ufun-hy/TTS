# 录音转文稿 V1

录音转文稿是独立运行、可接续到 Text Studio 的本地功能：

```text
wav / mp3 / m4a / mp4
→ ffmpeg 统一解码为 16 kHz 单声道 WAV
→ 本地 Qwen3-ASR-1.7B / Apple Silicon MLX
→ 轻度中文断句和口头整理
→ 可复制、可下载、持久化的文稿
→ Text Studio 话术单元与后续编辑
```

## 启动

```bash
./scripts/recording-transcript-start.sh
```

打开 <http://127.0.0.1:8771>。

服务只使用本地 Qwen3-ASR-1.7B MLX 模型，不提供 ASR 后端切换或自动降级。模型和 Python 环境缺失时直接报错，不会在线下载模型。

## Qwen3-ASR-1.7B / MLX

- 后端：`recording_transcript/qwen_asr.py`
- 通用音频解码：`recording_transcript/audio.py`
- 模型目录：`runtime/models/asr/qwen3-asr-1.7b-bf16/`
- Python 环境：`runtime/qwen-asr-venv/bin/python`
- 依赖：`scripts/requirements-qwen-asr.txt`
- 可通过 `RECORDING_TRANSCRIPT_MODEL` 指定模型目录
- 可通过 `RECORDING_TRANSCRIPT_PYTHON` 指定 Python
- 推理固定使用 Apple Silicon MLX GPU
- 默认约 30 秒分块、中文、temperature=0、每块最多 1024 token

安装示例：

```bash
python3.11 -m venv runtime/qwen-asr-venv
runtime/qwen-asr-venv/bin/python -m pip install -r scripts/requirements-qwen-asr.txt
runtime/qwen-asr-venv/bin/hf download mlx-community/Qwen3-ASR-1.7B-bf16 \
  --revision e1f6c266914abc5a46e8756e02580f834a6cf8a7 \
  --local-dir runtime/models/asr/qwen3-asr-1.7b-bf16
```

启动脚本开启 Hugging Face / Transformers 离线模式，因此运行时不会因为缺少本地权重而隐式联网拉取。

## 文稿整理

ASR 完成后只进行轻度整理：

- 标点与断句
- 自然分段
- 明显口头填充词
- 明显口吃或重复

不在这里做 Semantic Coverage、话术泛化、营销改写或 TTS。最终目标是尽量保留原始信息和顺序，同时让录音文字可以直接阅读。

## 持久化与 Text Studio 接续

完成后结果写入：

```text
runtime/recording-transcript/results/<job_id>.json
```

记录包含 `schema_version`、`job_id`、`filename`、`size`、`stage`、`text`、`updated_at`。只持久化最终清理文稿，不保存上传的临时音频副本。

录音页面可以打开历史结果，并通过“在 Text Studio 继续”跳转到 8770。Text Studio 读取同一结果目录，以副本方式整理为话术单元，不修改原始文稿结果。

相关 API：

```text
GET /api/health
GET /api/transcript/results
GET /api/transcript/jobs/<job_id>
```

Text Studio：

```text
GET /api/transcript/results
GET /api/transcript/result?job_id=<id>
```

## 一键启动

一键管理：

```bash
./scripts/stack-start.sh
./scripts/stack-stop.sh
./scripts/stack-restart.sh
./scripts/stack-status.sh
```

服务：

```text
8765  TTS Gateway
8000  Audio Cache
8770  Text Studio
8771  Recording Transcript
```

Text Studio 使用 `server/text_studio_entry.py` 启动，并加载 `web/text-studio-recording-link.js`，顶部提供“录音转文稿”入口。

`/api/health` 中：

- `asr_backend` 应为 `qwen3-asr-1.7b-mlx`
- `asr_ready` 表示平台、依赖和模型文件预检查通过
- `asr_error` 在未就绪时给出原因

## 真实录音验证

2026-09-13，在 Apple Silicon / 24GB 内存、Python 3.11、mlx-audio 0.3.1、MLX 0.32.2 环境下，使用 300 秒真实直播录音完成本地验证：

- 模型加载约 7 秒
- 两轮完整推理约 32 秒、29 秒
- 两轮文本结果一致
- MLX 峰值内存约 5.28 GiB
- 正式 8771 上传链路完成 `ffmpeg → Qwen → 文稿整理 → JSON 持久化`

该结果只证明当前真实样本可以稳定运行，不代表所有口音、背景音乐或长录音场景都已经达到同等准确率。

## 旧本地资源清理

项目运行只需要当前 Qwen 模型与 `runtime/qwen-asr-venv`。确认 8771 已用 Qwen 正常完成真实录音后，可删除旧 ASR 模型目录和旧专用 Python 环境。不要删除用户录音、`runtime/recording-transcript/results/` 或当前 Qwen 模型目录。
