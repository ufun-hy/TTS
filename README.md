# Mac mini 本地 CosyVoice TTS API

最小链路：`POST /speak` → 常驻 CosyVoice3 → WAV → macOS 默认音频设备播放。

实现使用 [cosyvoice.cpp](https://github.com/Lourdle/cosyvoice.cpp) 的 macOS arm64 预编译包和 `Fun-CosyVoice3-0.5B-2512` 的 GGUF 模型。模型和运行时文件只放在 `runtime/`，不会提交 Git。

## 安装

需要 Apple Silicon macOS、Homebrew、`curl`、Python 3 和可用的 macOS 音频输出设备：

```bash
./scripts/install.sh
```

安装脚本会下载：

- `cosyvoice.cpp v0.1.1` macOS arm64 miniaudio release
- `CosyVoice3-2512_Q8_0.gguf`
- `speech_tokenizer_v3.int8.onnx`、`campplus.int8.onnx`
- 官方示例 `zero_shot_prompt.wav`，并生成 `prompt_speech.gguf`

模型来源：[Hugging Face 模型包](https://huggingface.co/Lourdle/Fun-CosyVoice3-0.5B-2512-GGUF)。该 GGUF 包是为 cosyvoice.cpp 准备的社区转换包；CosyVoice3 原始模型说明见 [QwenAudio/CosyVoice](https://github.com/QwenAudio/CosyVoice)。

## 启动

默认监听本机所有网卡 `0.0.0.0:8765`，并让 cosyvoice.cpp 自动选择可用后端（Apple Silicon 上优先 Metal）：

```bash
./start.sh
```

也可以显式使用同样的局域网监听模式：

```bash
./start.sh --lan
```

可通过环境变量调整端口或后端：`TTS_PORT=8765 COSYVOICE_BACKEND=auto ./start.sh`。只有明确设置 `COSYVOICE_BACKEND=cpu` 才会使用 CPU；启动输出会显示请求的后端模式，CosyVoice 日志会记录服务端运行信息。

## 本机测试

```bash
curl -X POST http://127.0.0.1:8765/speak \
  -H 'Content-Type: application/json' \
  -d '{"text":"欢迎进入直播间，这是接口测试。"}'
```

只有合成成功并且 `afplay` 播放完成后才返回：

```json
{"success": true}
```

空文本、缺少 `text` 或超过 200 个字符返回 400。多个请求由单个 FIFO worker 顺序合成和播放，单条失败不会停止 worker。

## 局域网测试

先在 Mac mini 执行 `./start.sh --lan`，再在另一台同一局域网设备上执行：

```bash
curl -X POST http://<MAC_LAN_IP>:8765/speak \
  -H 'Content-Type: application/json' \
  -d '{"text":"欢迎进入直播间，这是局域网测试。"}'
```

Mac mini 的局域网地址可用 `ipconfig getifaddr en1` 查看；不要做端口映射、隧道或公网 DNS。

## 停止与日志

在运行 `start.sh` 的终端按 `Ctrl-C`，它会同时停止网关和 CosyVoice 子进程。CosyVoice 日志在 `runtime/logs/cosyvoice-server.log`，生成的 WAV 在 `runtime/audio/`。

## 当前验收记录（2026-09-07，Mac mini M4 Pro）

- 模型启动：约 5 秒到 `/healthz` ready。
- 首次中文合成：CosyVoice 服务端 2.71 秒；文本 19 字符。
- 暖机中文合成：CosyVoice 服务端 2.17 秒；文本 20 字符。
- 音频：实际生成并播放 WAV，24 kHz、单声道；`afplay` 返回成功。
- 加速：`COSYVOICE_BACKEND=auto` 合成约 2 秒级；同文本显式 CPU 对照为 8.73 秒。合成期间 `IOAccelerator` 的 GPU Device Utilization 最高 99%，因此本次实际使用 Metal。显式 `--backend metal` 在该 release/macOS 组合下初始化失败，默认保持 `auto`。
- 进程物理内存：`footprint` 采样峰值 319 MB；这是进程 physical footprint，不等同于统一内存中 GPU/驱动的全部占用。
- FIFO：3 条不同文本连续提交，按队列顺序完整播放，均返回成功；未观察到重叠。
- 校验：空文本、缺少 `text`、201 字符文本均实际返回 400。
- 局域网：`--lan` 后通过 `192.168.3.92:8765` 本机 LAN 地址请求成功；当前执行环境没有第二台局域网设备，因此“另一台设备”验收尚未完成。
- 重启：停止服务、重新启动后再次合成成功。

网关日志会记录每条语音的合成、播放和总耗时；CosyVoice 服务日志在 `runtime/logs/cosyvoice-server.log`。

## 已知限制

- 这是本地验证服务，不包含鉴权；默认监听所有网卡，仅建议在可信局域网使用。
- 依赖 macOS 默认音频设备和 `/usr/bin/afplay`。
- 当前请求是整段 WAV 返回后再播放，没有流式播放。
- 当前最大文本长度是 200 个 Unicode 字符。
