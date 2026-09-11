# Voice assets

每个音色使用一个目录：

```text
voices/host_female/reference.wav
voices/host_female/reference.txt
```

`reference.wav` 必须是干净的单人语音，`reference.txt` 必须是音频中实际说出的准确文字。真实音频不会提交 Git。

Voice ID 只允许英文、数字、下划线和连字符；中文显示名可在 `server/live_session.py` 中配置。例如石榴女声的 API ID 是 `shiliu_1`，显示名是“石榴1”。

使用 `scripts/voice-prepare.sh host_female` 生成运行时的 `prompt_speech.gguf` 并注册到 `voices.json`。Gateway 会在下一次请求前自动同步音色，不需要重启 CosyVoice。
