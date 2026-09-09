# 局域网 AI 音频缓存传输 V1

本阶段只负责 TTS 音频缓存、局域网 HTTP 拉取、任务顺序和消费确认，不负责播放、虚拟声卡、OBS 或直播平台接入。

## 目录和状态

`AudioCacheManager` 使用 `runtime/audio-cache/`，每个任务是一个独立目录：

```text
runtime/audio-cache/
├── pending/
├── ready/
├── processing/
├── completed/
└── failed/
```

目录移动是状态变更的原子操作。每个任务目录包含 `audio.wav` 和 `metadata.json`，元数据会记录文本、音色、原始/最终时长、速度、音量增益、异常和时间戳。

## 启动 AI 端

先启动现有 TTS Gateway，再启动缓存服务：

```bash
export TTS_API_KEY="..."
python3 scripts/audio-cache-server.py \
  --host 0.0.0.0 \
  --port 8000 \
  --tts-url http://127.0.0.1:8765
```

`config/audio-cache.example.json` 可修改 `preload_segments`、速度范围和音量目标。服务支持可选的 `AUDIO_CACHE_API_KEY`；设置后客户端请求需使用同一个 Bearer key。

## 生成并提前缓存

通过文本让 AI 端调用本机 TTS，再把结果放入缓存：

```bash
curl -X POST http://127.0.0.1:8000/audio/enqueue \
  -H 'Content-Type: application/json' \
  -d '{"id":"segment_001","sequence":1,"text":"今天给大家介绍这个商品","voice":"default","target_duration":30}'
```

批量提前生成最多 `preload_segments` 段：

```bash
curl -X POST http://127.0.0.1:8000/audio/preload \
  -H 'Content-Type: application/json' \
  -d '{"preload_segments":5,"segments":[{"id":"segment_001","text":"第一段","voice":"default","target_duration":30},{"id":"segment_002","text":"第二段","voice":"default","target_duration":30}]}'
```

如果需要传入已经生成的 WAV，可把 `audio_base64` 放入 `/audio/enqueue` 请求，服务仍会执行同样的处理和状态流转。

## API

| 接口 | 作用 |
| --- | --- |
| `GET /audio/next` | 按 `sequence` 顺序领取一条 `ready` 音频；领取后立即进入 `processing` |
| `GET /audio/files/{id}` | 下载 WAV |
| `GET /audio/status/{id}` | 查询任务状态，供客户端重启恢复 |
| `POST /audio/ack` | `{\"id\":\"segment_001\",\"status\":\"completed\"}` 确认消费 |
| `GET /health` | 返回各状态数量 |

无 ready 音频时 `/audio/next` 返回 `204`。ACK 是幂等的；客户端下载完成后先把 WAV 和本地 metadata 原子写入 cache，网络断开时不会丢失本地文件。

## 直播端客户端

复制并修改 `config/audio-client.example.json`：

```json
{
  "server": "http://192.168.x.x:8000",
  "poll_interval": 1000,
  "cache": "runtime/audio-client"
}
```

下载一条进行联调：

```bash
python3 scripts/audio-client.py --config config/audio-client.example.json --once
```

长期运行：

```bash
python3 scripts/audio-client.py --config config/audio-client.example.json
```

客户端不会播放音频。消费层拿到 `ClientAudio.path` 后返回 `completed` 或 `failed`，再调用 `AudioClient.ack()`；当前命令行客户端默认只下载并等待后续消费控制。

## 处理规则

- 速度因子按 `raw_duration / target_duration` 计算，并限制在配置的 `min/max`；超出范围仍使用边界值，同时写入 `processing_warnings`。
- 音量使用 FFmpeg `volumedetect` 的平均响度估算，增益受 `max_gain` 限制并保存到 metadata。
- 启用处理时需要本机存在 `ffmpeg`；处理失败的任务进入 `failed`，不会污染 `ready` 队列。

## V1 边界

当前不实现 WebSocket、RTP、WebRTC、RTMP、播放控制、虚拟声卡、OBS 和云端部署。
