# Windows Audio Playback V1

Windows Client 现在把传输状态和播放状态分开：

```text
server status: completed   = WAV 已传输到 Windows
playback_status: cached    = 本地待播放
playback_status: buffering = 启动或恢复播放前的安全库存累积
playback_status: playing   = 正在播放
playback_status: played    = 已播放完成
playback_status: playback_failed = 单段播放失败
```

播放层只读取本地 cache，不修改服务端协议、原始 WAV、文本或 `sequence`。

## 使用

启动客户端后会自动连接并持续下载。下载完成后，GUI 在本地 WAV 原子落盘后发送现有 ACK；这只确认传输，不代表音频已播放。

点击“开始播放”后，播放线程按 metadata 中的 `sequence` 排序消费 `cached` 项。Live Session 首次启动和库存耗尽后的 `rebuffering` 都会累计 12 秒安全库存；标记为 `buffering` 的项目不会被提前播放，最终项目可强制释放。下载线程和播放线程互不阻塞：网络中断时仍会继续播放已经落盘的音频。

控制按钮：

- `开始播放`：从第一个未播放段开始。
- `暂停播放` / `继续播放`：只控制播放线程，下载继续运行。
- `停止播放`：停止当前播放并保留当前段为 `cached`，再次开始时不会跳过它。

播放速度和音量由 Mac Live Session 配置并在进入 Audio Cache 前写入最终 WAV。Windows 不保存、不解释、不二次调整这两个参数。

## Windows 实现

播放层使用 Windows 自带 `winmm.dll` 的 MCI `waveaudio` 接口，通过 `ctypes` 只控制打开、播放、暂停、继续、停止和关闭，不弹出外部播放器窗口，也不要求用户另装播放器。速度和音量由 Mac 在缓存前处理。

播放补丁 `5396416` 的实现如下，整合状态见 [整理执行记录](branch-consolidation-results.md)。已有缓存时的片间调度采用增量元数据索引（`audio_client/playback_cache.py`）：每次选段枚举目录，但只重新读取新增或变化的 JSON；播放器自身写入状态后显式使对应索引失效，兼容 FAT32 的低精度时间戳。播放回调从内存快照统计，不再同步重扫整个缓存；GUI 的定时刷新仍会读取增量索引，以显示播放/暂停期间新下载的库存。会话切换在播放线程选段时处理。

每段开始和结束各合并为一次 JSON 写入，PCM16 播放前只做一次 WAV 检查。日志分别记录选段、调度、WAV 准备、MCI open/set/play/close。`inter_segment_gap_ms` 现在使用高精度单调计时，含义是上一段 MCI 停止被观察到，至下一段 play 命令返回的间隔；不包含停止轮询之前的误差，也不等于声卡回录静音长度。旧版同名字段只测量部分控制器阶段，不能直接与新字段比较。

修复前后 Windows 实机对照、原始日志和复跑命令见 [片间调度耗时验证](windows-playback-switch-latency.md)。

Float32 WAV 的 MCI 兼容、`pcm16/` 派生缓存、失败片段恢复和 Windows 验收步骤见 [Windows WAV 格式兼容](windows-wav-compatibility.md)。

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

`tests/test_playback.py` 使用可替换的 fake player 验证 3 段顺序播放、`played` 持久化、中断恢复和当前会话 Float32 失败恢复。真实声音、暂停/继续、Windows 安装包及 Text Studio 全链路仍需在 Windows 直播端执行。
