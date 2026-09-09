# Software API contract

业务软件只依赖配置，不感知 Tailscale、Funnel、CosyVoice、GGUF 或 Mac LAN IP：

```json
{
  "tts": {
    "base_url": "https://your-device.your-tailnet.ts.net",
    "api_key": "由操作系统安全存储提供",
    "default_voice": "default",
    "connect_timeout_seconds": 5,
    "request_timeout_seconds": 30
  }
}
```

TTS Gateway 支持的接口只有：

- `GET /health`：健康检查，可不带 API Key。
- `GET /voices`：必须带 `Authorization: Bearer <API_KEY>`。
- `POST /speak`：必须带 API Key 和 JSON body，例如 `{"text":"欢迎进入直播间","voice":"default"}`。

软件端应在后台线程/async task 中调用 `/speak`，不要阻塞 UI 线程。`/speak` 返回代表该语音已经完成合成并播放，不是仅入队成功。

错误处理：`400` 参数错误，`401` 鉴权失败，`404` 音色不存在，`429` 请求过快，`5xx` 服务异常，连接/请求超时视为网络或 Funnel 不可用。不要无限重试。

## Text Studio Live Session V1

Text Studio 在原有页面内提供单实例直播控制。启动时可用以下配置指定 Audio Cache：

```bash
export AUDIO_CACHE_URL="http://127.0.0.1:8000"
export AUDIO_CACHE_API_KEY="..." # 仅当 Audio Cache 启用了鉴权
```

接口：

| 接口 | 作用 |
| --- | --- |
| `POST /api/live/start` | `{ "voice": "default", "segments": [{ "id": "p0001", "text": "确认后的直播文本" }] }`，创建并异步启动 Session |
| `GET /api/live/status` | 返回状态、已生成/待传输/处理中/已传段数和 Windows 客户端在线状态 |
| `POST /api/live/pause` / `resume` | 暂停或继续生成 |
| `POST /api/live/stop` | 停止继续生成，保留已经进入缓存的音频 |
| `POST /api/live/reset` | 停止当前任务并按 Session ID 清理其缓存 |

Live Session 只接收 Text Studio 已确认的自然段文本，保持传入顺序，调用 TTS Gateway `/synthesize`，再把 WAV 送入 Audio Cache `/audio/enqueue`。仅当某一段超过 Gateway 单次长度限制时，才按固定字符块生成 `p0005-01` 这类技术子段；不会重新做语义断句或泛化。Windows 客户端继续使用现有 `/audio/next` 拉取和 ACK 流程。
