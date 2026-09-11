# 录音转文稿 V1

这是独立于 Text Studio 的本地录音转文稿入口：

```text
wav / mp3 / m4a / mp4
→ ffmpeg 统一解码为 16 kHz 单声道 WAV
→ 本地 mlx-whisper / whisper-large-v3-turbo
→ 轻度中文断句和口头填充词清理
→ 可复制、可下载的 TXT 文稿
```

启动：

```bash
./scripts/recording-transcript-start.sh
```

打开 <http://127.0.0.1:8771>。默认使用 `runtime/models/asr/large-v3-turbo/`；也可以通过 `TTS_ASR_MODEL` 或 `--model` 指定已有的本地模型目录。服务固定使用 `mlx-whisper`，不会下载模型或调用云端 ASR。

清理只做标点、断句、自然分段、明显口头填充词和明显重复处理，不接入 Semantic Coverage、话术泛化、TTS 或数据库。上传文件只作为处理期间的临时副本，不生成 ASR JSON。
