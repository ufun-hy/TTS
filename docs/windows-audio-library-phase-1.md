# Windows 本地音频库存：第三部分第一批交付

## 已完成
- 新增 `audio_cache/library.py`，保存独立于传输/播放状态的音频文件和元数据。
- `AudioCacheManager.process_pending` 在 live_session 生成音频处理成功后，将已处理的 WAV 另存至 `audio_cache_root/library/<id>/audio.wav`，同时保存 `metadata.json`。
- 文件先复制到临时目录再原子替换，只有写入完成才允许变成 ready；写盘失败不会把该条音频当成成功入库。
- ACK 后释放传输缓存、重置 Session 时清理传输缓存，均不删除库存文件。
- 音频库存可在 Audio Cache 进程重启后重新扫描。
- 新增受原有 Audio Cache Bearer 校验保护的 `GET /audio/library?session_id=&limit=` 与 `GET /audio/library/files/<id>`。
- Text Studio 增加 `GET /api/audio/library` 查询代理及库存列表。
- `tests/test_audio_library.py` 覆盖 ACK、Session cleanup、重新初始化后的库存保存。

## 当前边界
**尚未完成完全独立的历史库存播放。** 当前生成和播放共用 Session 的限制仍在，只有独立保存和查询已完成。下一批才可以设计独立 playback session，允许不启动 TTS 播放选定库存。

- 当前只将 `source=live_session` 的合成内容收入库存，其他接口入库行为不变。
- 历史库存尚无分页游标、按项目筛选、删除管理、配额和磁盘满额保护。只提供最多 500 条查询，界面显示最近 100 条，非总数统计。
- 生成仍受原有 buffer_high_seconds/buffer_low_seconds 回压规则限制，无法声称暂停播放后可以持续合成 6–12 小时。
- 仍使用旧 Audio Cache、WinMM 和本地轮询链路；未重构单宿主 Runtime。
- 所有变更都尚未在真实 Windows 上运行，测试源代码已经加入仓库但未取得通过报告。

## 验收建议
1. 使用实际 Windows TTS 生成若干 WAV，确认两个位置都有音频：播放用缓存，以及独立库存。
2. 播放 ACK 后确认库存文件仍在。
3. 重置旧 Session 后确认库存文件仍在。
4. 重启 Audio Cache 和桌面工作台，确认库存仍可查询。
5. 模拟磁盘写入失败和不足空间，确认合成任务不虚报成功。
6. 观察 1000 段推理与连续播报时磁盘增长，规划库存配额与显式清理操作。
7. 运行 `python -m unittest tests.test_audio_library tests.test_generation_playback_split tests.test_live_session tests.test_text_studio`。

## 下一批工作
独立播放 Session / 选择库存、播放顺序和跨项目播放；然后改造库存容量回压并实现 6–12 小时预生成目标。
