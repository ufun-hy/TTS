# 录音转文稿 V1

这是独立运行、可接续到 Text Studio 的本地录音转文稿入口：

```text
wav / mp3 / m4a / mp4
→ ffmpeg 统一解码为 16 kHz 单声道 WAV
→ 本地 mlx-whisper / whisper-large-v3-turbo
→ 轻度中文断句和口头填充词清理
→ 可复制、可下载、持久化的清理文稿
→ Text Studio 话术单元与后续编辑
```

启动：

```bash
./scripts/recording-transcript-start.sh
```

打开 <http://127.0.0.1:8771>。默认使用 `runtime/models/asr/large-v3-turbo/`；也可以通过 `TTS_ASR_MODEL` 或 `--model` 指定已有的本地模型目录。服务固定使用 `mlx-whisper`，不会下载模型或调用云端 ASR。

清理只做标点、断句、自然分段、明显口头填充词和明显重复处理，不接入 Semantic Coverage、话术泛化、TTS 或数据库。上传文件只作为处理期间的临时副本，不生成 ASR JSON。


## 持久化与 Text Studio 接续

清理完成后，先原子写入 `runtime/recording-transcript/results/<job_id>.json`，再将任务标为完成。记录包含 `schema_version`、`job_id`、`filename`、`size`、`stage: completed`、`text`、`updated_at`；仅持久化最终清理文稿，不包含原始 ASR 分段。失败或处理中的任务不进入结果列表。服务重启后，已完成结果仍可打开，处理中任务不支持断点恢复。

录音页面的“已保存的稿清理结果”可以打开历史文稿，“在 Text Studio 继续”跳转到同主机 8770 端口并携带 `transcript_id`。非默认部署可在录音服务配置 `TEXT_STUDIO_URL`（例如 `http://localhost:8770/`）。两个服务需使用同一仓库运行数据目录。

Text Studio 可直接选择“稿清理结果”，或接收上述链接。导入复制正文为新的临时项目并整理话术单元，不改写清理结果。后续仍使用原有事实统一、原稿还原、泛化、搜索替换、风险、TTS 和智播入口。

API：录音服务 `GET /api/transcript/results` 列出清理结果，`GET /api/transcript/jobs/<job_id>` 也可读取持久化结果；Text Studio 提供 `GET /api/transcript/results` 与 `GET /api/transcript/result?job_id=<id>`。不需要跨服务写请求或跨域配置。

共享持久化代码位于 `recording_transcript/results.py`；`tests/test_transcript_handoff.py` 覆盖任务完成落盘、服务重建后恢复、Text Studio 接续与无效/失败结果隔离。已有仅存在内存、且服务已退出的旧任务无法追溯恢复；可重新转录。
