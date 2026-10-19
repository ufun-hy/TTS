# Windows 独立库存回放：第三部分第二批实现及验收

## 实现内容
- Audio Cache 新增 `/audio/replay/start`、`/audio/replay/status`、`/audio/replay/pause|resume|stop`。
- 启动回放必须提交 1–500 个已归档音频 ID，按所选 ID 次序写入独立 replay Session；不会调用 CosyVoice。
- 复制归档 WAV 至当前传输队列，不重新做变速/音量处理，不修改原始库存。
- 复用旧 AudioClient→WinMM 播放路径及 Session 领取协议，降低设备层变更风险。
- 与仍处于 running 的其他播放 Session 冲突时拒绝开始回放，防止夺取播放器。
- Text Studio 增加 `/api/audio/replay/*` 代理；库存列表可勾选多个 WAV，控制开始、暂停、恢复、停止。
- 新增 `tests/test_library_replay.py`，验证复用归档文件、次序、互斥、停止不删除库存。

## 仍然未完成的条件
- **这是复用旧 Session / Audio Cache 的独立库存回放 V1，并非全局独立播放引擎。**
- 独立回放 Session 的控制状态目前仅内存保留；Audio Cache 重启会丢失回放状态，磁盘中的已入队项仍存在。重新接管/清理孤立回放任务、断电后的精确续播未实现。
- 界面只能按最近 100 条库存勾选；尚无项目分组、播放列表持久化、分页和多小时库存大规模操作。
- 播放进度展示依赖缓存状态，当前没有准确显示“正在播放第几段”的新界面。
- 旧 WinMM 播放链路仍需 Windows 实机验证，包括切段空白和缓冲重置。
- 停止播放不删除原始库存，但传输层可能存在已下载文件，需要另做空间回收策略。
- 音频生成仍使用旧缓冲回压，不具备已验收的 6–12 小时持续离线预生成能力。
- **本次 GitHub 修改尚未取得任何真实 Windows GUI / WinMM / GPU 测试通过报告。**

## 实机测试
1. 生成至少三段合法 WAV，确认已归档。
2. 停止 TTS 引擎，只启动既有 Audio Cache + Windows 播放服务。
3. 勾选库存条目后开始回放，确认声音按勾选顺序输出。
4. 暂停回放、继续回放、停止回放，确认原始文件数量及校验和不变。
5. 活跃直播播放时尝试回放，确认接口拒绝冲突。
6. 关闭桌面窗口并从托盘恢复，回放不中断。
7. 重启 Audio Cache，记录回放 Session 恢复缺口并确认没有误播旧 Session。
8. 运行 `python -m unittest tests.test_library_replay tests.test_audio_library tests.test_generation_playback_split tests.test_live_session tests.test_text_studio`，再跑仓库完整回归。

## 后续
优先解决 Session 重启恢复、库存列表分页/项目归属、磁盘配额，以及播放/生成不依赖对方的长期独立生命周期。
