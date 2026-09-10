# Windows Audio Playback V1

Windows Client 现在把传输状态和播放状态分开：

```text
server status: completed   = WAV 已传输到 Windows
playback_status: cached    = 本地待播放
playback_status: playing   = 正在播放
playback_status: played    = 已播放完成
playback_status: playback_failed = 单段播放失败
```

播放层只读取本地 cache，不修改服务端协议、原始 WAV、文本或 `sequence`。

## 使用

启动客户端后会自动连接并持续下载。下载完成后，GUI 在本地 WAV 原子落盘后发送现有 ACK；这只确认传输，不代表音频已播放。

点击“开始播放”后，播放线程按 metadata 中的 `sequence` 排序消费 `cached` 项。下载线程和播放线程互不阻塞：网络中断时仍会继续播放已经落盘的音频。

控制按钮：

- `开始播放`：从第一个未播放段开始。
- `暂停播放` / `继续播放`：只控制播放线程，下载继续运行。
- `停止播放`：停止当前播放并保留当前段为 `cached`，再次开始时不会跳过它。

播放速度和音量会写入 `config.json`：

```json
{
  "playback_speed": 1.0,
  "playback_volume": 100
}
```

速度只影响 Windows 播放命令，音量只影响播放命令，均不改写缓存 WAV。

## Windows 实现

播放层使用 Windows 自带 `winmm.dll` 的 MCI `waveaudio` 接口，通过 `ctypes` 控制打开、播放、暂停、继续、停止、速度和音量，不弹出外部播放器窗口，也不要求用户另装播放器。

## 构建安装包

在 Windows 构建机执行：

```powershell
python -m pip install pyinstaller
.\build\windows\build.ps1
```

脚本需要 Inno Setup 6，并生成：

```text
build/windows/output/AI-Audio-Client-Setup.exe
```

安装后目录包含：

```text
AI Audio Client/
├── AI-Audio-Client.exe
├── config.json
├── cache/
└── logs/
```

## 本机可验证项

`tests/test_playback.py` 使用可替换的 fake player 验证 3 段顺序播放、`played` 持久化和中断恢复。真实声音、暂停/继续、Windows 安装包及 Text Studio 全链路需要在 Windows 直播端执行。
