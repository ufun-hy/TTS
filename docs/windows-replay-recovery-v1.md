# Windows 库存回放任务持久化：V1.1

## 本轮代码
- 新增 `audio_cache/replay_journal.py`：`replay-state.json` 保留当前回放 Session ID、用户选择的音频 ID 顺序、任务状态，使用临时文件 + fsync + os.replace 原子更新。
- Audio Cache 首次访问回放控制时，从磁盘恢复任务；**重启前为 running / paused 都恢复成 paused**。stopped 仍为 stopped。
- 新回放任务先持久化为 paused，再复制 WAV 进入缓存队列；完成队列准备并持久化 running 后才开放播放。
- 暂停 / 继续 / 停止的回放状态先持久化再发布，异常时避免宣称已成功。
- 损坏或校验失败的 journal 直接忽略，不允许自动开播；未知旧回放 Session 仍默认 stopped。
- Text Studio 的库存回放状态提示「重启后已恢复，等待手动继续」，允许用户主动点击继续。
- 补充 `tests/test_replay_journal.py`、`tests/test_library_replay.py` 覆盖任务状态、重启、确认恢复与损坏数据。

## 尚未解决，不得宣称已完成
- Audio Cache 保留 `ready / processing / completed` 的磁盘队列，但**已经被 Windows 下载、正在播放或者进入 processing 的条目，无法保证重启后准确接续**。当前恢复的是会话选择和未领取队列，并不是无损断点续播。
- 若 Audio Cache 与独立 Windows 播放服务同时异常退出，仍需核对播放器本地缓存与 ACK，才能决定怎样安全地重新排队；贸然把 processing 全部改回 ready 会重复播放。
- Windows 系统重启后需有服务启动链路才能读取 journal，本轮没有增加开机自启或精确播放位置恢复。
- 回放完成状态的自动收敛、旧 Session 传输缓存的空间回收、分页/项目分组、存储配额仍未实现。
- Runtime Host、WinMM 播放时序及 WebView2 客户端仍未经过 Windows 实机验收。
- GitHub 端已写入测试源文件，本轮未取得测试执行通过结果。

## Windows 实机验收建议
1. 生成三段真实 WAV，创建库存回放 Session，播放第一段后暂停，记下 Session ID 和本地缓存。
2. 仅重启 Audio Cache：查询状态应显示同一 Session 且 paused；不能自行播放。
3. 点击「继续回放」：只继续当前 ready 队列；核对是否出现 processing 条目缺失，不能假设完整恢复。
4. 重新启动 UI：不创建新的回放任务，状态应由服务端恢复。
5. 已停止的 Session 重启后不允许直接 resume。
6. 故意写入损坏的 replay-state.json（仅测试环境），确认不会自动恢复运行或误播。
7. 运行：`python -m unittest tests.test_replay_journal tests.test_library_replay tests.test_audio_library tests.test_generation_playback_split`。
8. 待以上通过，再验收 Windows 完整播放链路及长期稳定性。
