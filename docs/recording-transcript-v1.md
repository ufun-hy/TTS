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

`qwen_asr.transcribe()` 把所有调用交给同一个常驻 GPU 工作线程，模型导入、加载及推理都在该线程完成。网页上传、音色注册和数据集脚本共用这个入口；任务失败会向调用方返回异常，后续任务仍可继续。

仅给上传任务加锁不够：每次上传新建线程时，mlx-lm 缓存的 `generation_stream` 可能属于上一个已退出的线程，后续任务因此报 `There is no Stream(gpu, 2) in current thread.`。2026-09-27 用同一段 12 秒音频复现了“首次成功、换线程后失败”，固定 GPU 工作线程后连续调用成功。更新后需要重启录音转文稿服务让新代码生效；健康接口只检查环境，验收应包含在同一服务进程连续提交多个录音。

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

### 长录音接续时的项目容量

项目保存和上下文分组共用 20,000 个话术单元的上限（`server/context_variants.py:MAX_UNITS`）。这里统计的是句子拆分后的编辑单元，不是原稿的自然段；长录音即使只有几百个自然段，也可能拆成几千至上万个单元。项目容量与单次泛化请求限制分开，界面仍按 8 个单元或小上下文组发送模型请求，HTTP 请求体仍限制为 16 MiB。

2026-09-27，“阔腿裤-9月27日.mp3”的完整清理结果为 168,450 字、706 个自然段，解析为 7,692 个单元。此前解析成功，但保存时触发 `project exceeds 5000 paragraphs`。修复后在独立本地 HTTP 服务上验证全文解析、保存、加载、1,084 个上下文组创建及带组数据的再次保存，内容和顺序完整一致，原始转写结果未修改。测试请求体约 3.4 MB。超过 20,000 个单元时仍需分项目保存，错误会显示实际数量及上限。

部署修改需刷新 Text Studio 服务；活动或暂停的直播、泛化及音频准备任务应先妥善处理，避免重启清除未持久化状态。无需重新进行 ASR。

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

后台常驻、launchd 状态、认证及迁移说明见 [本地服务管理](stack-services.md)。

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

2026-09-27 线程归属修复验证：18 项相关自动测试通过，包含不同调用线程、同时调用、单次失败后继续，以及文稿持久化接续。原先“调用线程每次更换”的真实模型复现，在修复后连续三次均成功。正式 8771 服务重启后，在同一进程连续上传两份 12 秒参考 WAV 和此前失败的“阔腿裤-9月27日.mp3”的 20 秒片段，三次均完成（约 6.80、1.56、2.04 秒），未出现 Stream 错误。完整长录音未在此短样本验收中重跑；记录保存在 `runtime/diagnostics/asr-stream-20260927/fixed-new-thread.json` 和 `http-verified.json`。

## 旧本地资源清理

项目运行只需要当前 Qwen 模型与 `runtime/qwen-asr-venv`。确认 8771 已用 Qwen 正常完成真实录音后，可删除旧 ASR 模型目录和旧专用 Python 环境。不要删除用户录音、`runtime/recording-transcript/results/` 或当前 Qwen 模型目录。
