# 录音转文稿 V1

这是独立运行、可接续到 Text Studio 的本地录音转文稿入口：

```text
wav / mp3 / m4a / mp4
→ ffmpeg 统一解码为 16 kHz 单声道 WAV
→ 本地 Qwen3-ASR-1.7B / Apple Silicon MLX
→ 轻度中文断句和口头填充词清理
→ 可复制、可下载、持久化的清理文稿
→ Text Studio 话术单元与后续编辑
```

启动：

```bash
./scripts/recording-transcript-start.sh
```

打开 <http://127.0.0.1:8771>。默认使用本地 Qwen3-ASR-1.7B MLX 模型；安装与环境配置见下文。服务不会下载模型、调用云端 ASR 或回退 Whisper。

清理只做标点、断句、自然分段、明显口头填充词和明显重复处理，不接入 Semantic Coverage、话术泛化、TTS 或数据库。上传文件只作为处理期间的临时副本，不生成 ASR JSON。


## 持久化与 Text Studio 接续

清理完成后，先原子写入 `runtime/recording-transcript/results/<job_id>.json`，再将任务标为完成。记录包含 `schema_version`、`job_id`、`filename`、`size`、`stage: completed`、`text`、`updated_at`；仅持久化最终清理文稿，不包含原始 ASR 分段。失败或处理中的任务不进入结果列表。服务重启后，已完成结果仍可打开，处理中任务不支持断点恢复。

录音页面的“已保存的稿清理结果”可以打开历史文稿，“在 Text Studio 继续”跳转到同主机 8770 端口并携带 `transcript_id`。非默认部署可在录音服务配置 `TEXT_STUDIO_URL`（例如 `http://localhost:8770/`）。两个服务需使用同一仓库运行数据目录。

Text Studio 可直接选择“稿清理结果”，或接收上述链接。导入复制正文为新的临时项目并整理话术单元，不改写清理结果。后续仍使用原有事实统一、原稿还原、泛化、搜索替换、风险、TTS 和智播入口。

API：录音服务 `GET /api/transcript/results` 列出清理结果，`GET /api/transcript/jobs/<job_id>` 也可读取持久化结果；Text Studio 提供 `GET /api/transcript/results` 与 `GET /api/transcript/result?job_id=<id>`。不需要跨服务写请求或跨域配置。

共享持久化代码位于 `recording_transcript/results.py`；`tests/test_transcript_handoff.py` 覆盖任务完成落盘、服务重建后恢复、Text Studio 接续与无效/失败结果隔离。已有仅存在内存、且服务已退出的旧任务无法追溯恢复；可重新转录。

## 一键启动故障修复

`stack-start.sh` 会设置 `PYTHONPATH`。直接运行 `server/*.py` 时，Python 又把 `server/` 放到搜索路径首位；此前代码仅在根目录不存在时插入根目录，造成同名 `server/recording_transcript.py` 遮蔽 `recording_transcript/` 包，8770 与 8771 均报 `recording_transcript is not a package`。两个服务现在始终将仓库根目录优先放入搜索路径。`tests/test_service_entrypoints.py` 使用一键启动相同的环境，验证三个脚本入口都能导入并显示帮助。

一键启动使用 `server/text_studio_entry.py`，加载 `web/text-studio-recording-link.js`，顶部提供“录音转文稿”入口。start 自动替换本仓库的旧 `server/text_studio.py` 监听；stop 同时处理新旧入口，并继续检查仓库路径以避免终止其他项目进程。Gateway 停止后等待监听退出，避免 restart 把尚未退出的旧监听误判为已启动。最终 status 非 READY 时 start 返回失败状态。

录音服务 `/api/health` 中 `asr_ready` 表示平台、依赖及模型文件预检查通过；服务 READY 不代表已完成真实音频识别或 TTS 播放验证。

## Qwen3-ASR-1.7B / MLX

录音转文稿仅使用 Qwen3-ASR-1.7B 的 MLX 权重，在 Apple Silicon GPU 本地推理；不调用或回退到 Whisper。`scripts/audio-ingest.py` 的 Whisper 能力原样保留给旧链路，本功能只复用其中的 ffmpeg 解码函数。上传、清理、8771 页面、任务状态和持久化格式不变。

- 后端：`recording_transcript/qwen_asr.py`。约 30 秒低能量停顿分块、中文、temperature=0、每块最多 1024 token；复用模型并串行处理 GPU 任务。
- 模型目录：`runtime/models/asr/qwen3-asr-1.7b-bf16/`；可通过 `RECORDING_TRANSCRIPT_MODEL` 或 `--model` 设置。只接受 1.7B Qwen3-ASR 架构，不读取旧链路的 `TTS_ASR_MODEL`。
- Python：`runtime/qwen-asr-venv/bin/python`，可通过 `RECORDING_TRANSCRIPT_PYTHON` 设置；不使用旧 `.venv-asr` 或自动回退到系统 Python。启动脚本开启 Hugging Face / Transformers 离线模式。
- `/api/health` 返回 `asr_backend: qwen3-asr-1.7b-mlx`、`asr_ready` 和 `asr_error`。就绪检查覆盖平台、依赖与模型文件，不代替实际推理测试。

安装示例（Apple Silicon，Python 3.11；本次实测 macOS 26.3）：

```bash
python3.11 -m venv runtime/qwen-asr-venv
runtime/qwen-asr-venv/bin/python -m pip install -r scripts/requirements-qwen-asr.txt
runtime/qwen-asr-venv/bin/hf download mlx-community/Qwen3-ASR-1.7B-bf16 \
  --revision e1f6c266914abc5a46e8756e02580f834a6cf8a7 \
  --local-dir runtime/models/asr/qwen3-asr-1.7b-bf16
./scripts/recording-transcript-start.sh
```

权重约 4.08GB，`model.safetensors` SHA-256：`2f080a3b769ae469aeaaa2dcb9e13a94141e54c9e6d5a7aa63392e0dc5a51789`。MLX 实现来自 [mlx-audio](https://github.com/Blaizzy/mlx-audio/tree/v0.3.1/mlx_audio/stt/models/qwen3_asr)，权重来自 [MLX 模型卡](https://huggingface.co/mlx-community/Qwen3-ASR-1.7B-bf16)。上游 Qwen 前端复用 Transformers 的 `WhisperFeatureExtractor` 计算音频特征；这不加载 Whisper ASR 模型或权重，也不提供 Whisper 回退。

### 真实录音验证（先验证、后集成）

2026-09-13，在本机 Apple Silicon / 24GB 内存、Python 3.11、mlx-audio 0.3.1、MLX 0.32.2 下，对仓库已有 `runtime/audio-ingest/桃子-首测-5m.mp3` 完整 300 秒录音离线转录两次：

- 模型加载 6.91 秒；两轮推理 31.95 秒、29.05 秒。
- 11 个分块连续覆盖 0～300 秒，两轮均输出 2050 字，文本 SHA-256 完全相同。
- MLX 峰值内存 5.28GiB，无空结果、崩溃或 Whisper 回退。
- 集成后先在隔离 HTTP 服务，再在正式 8771 上传同一录音，均成功完成 ffmpeg → Qwen → 原清理器 → JSON 持久化，清理后 2038 字，与落盘正文一致。正式任务名为“Qwen验证-桃子5分钟.mp3”，可在页面历史结果中打开。
- 41 项相关测试通过；浏览器确认历史结果展示、复制/下载按钮和 Text Studio 接续链接正常。

证据保存在 `runtime/qwen-asr-validation/`：`report.json`、`run-1/2.txt`、`run-1/2.json`、`http-result.json`、`live-result.json`、`最终文稿-Qwen.txt`、`environment.txt`。下载分片和验证音频保留，未删除旧 Whisper 模型或环境。

边界：这证明该样本可稳定完成，不是准确率基准；无逐字人工真值，仍可能有同音字、口语或分块接缝误识别。片段时间是分块位置，不是逐字时间戳。更长或异常高密度语音仍需额外实测。
