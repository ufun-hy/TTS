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

支持的接口只有：

- `GET /health`：健康检查，可不带 API Key。
- `GET /voices`：必须带 `Authorization: Bearer <API_KEY>`。
- `POST /speak`：必须带 API Key 和 JSON body，例如 `{"text":"欢迎进入直播间","voice":"default"}`。

软件端应在后台线程/async task 中调用 `/speak`，不要阻塞 UI 线程。`/speak` 返回代表该语音已经完成合成并播放，不是仅入队成功。

错误处理：`400` 参数错误，`401` 鉴权失败，`404` 音色不存在，`429` 请求过快，`5xx` 服务异常，连接/请求超时视为网络或 Funnel 不可用。不要无限重试。
