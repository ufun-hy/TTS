# Windows 音频库存容量管理 V1 — 实施与验收

## 本轮修改
- `AudioLibrary.capacity()` 扫描已保存 WAV 与 metadata.json，提供全量归档计数、实际文件字节占用、上限及剩余额度；不同于首页只展示最近 100 条。
- 默认容量上限 **20 GiB**；可使用服务进程环境变量 `AI_LIVE_STUDIO_LIBRARY_MAX_GIB` 配置（0.01–100000 GiB）。这是容量限制，不是磁盘可用空间；配置需在 Audio Cache 启动前设定。暂未增加可视化修改上限。
- 写入归档前检查当前容量与可用磁盘空间（至少保留 16 MiB 作为安全余量）。不足时本次归档失败，不自动删除任何库存，也不把条目标记为 ready。此检测尚不能取代 Windows 文件系统实际的磁盘写入错误处理。
- 增加手动删除 1–100 条库存 API；必须明确指定音频 ID，不提供定时、按日期或自动清理。
- 删除前拒绝操作当前回放任务所选音频、以及 ready / pending / processing 的播放缓存和正在生成条目引用的归档。
- Text Studio 增加真实总量和已用配额显示、勾选后确认永久删除。
- 新增 `tests/test_audio_library_capacity.py` 覆盖总量、达到上限、主动删除、非法批量删除、播放引用等基础边界。

## 接口
- `GET /audio/library/capacity`：Audio Cache 内部接口（沿用已有 Bearer 校验）。
- `POST /audio/library/delete`，body：`{"item_ids":["abc1"]}`。
- Text Studio 代理：`GET /api/audio/library/capacity`、`POST /api/audio/library/delete`。

## 安全边界与后续
- **本阶段不会清理已下载到 Windows 客户端的二级缓存，也不会修改播放器的 ACK/播放逻辑。**
- 此版本不提供项目级分组、保留天数、全量分页或自动清理；当前 `session_id` 仅是已有来源标记，不能等同于产品项目 ID。
- 并发查询需要扫描本地库存目录；大规模库存可能带来额外磁盘 I/O，后续应引入持续维护的索引与容量计数。现有配额检查是单进程内的串行检查。
- 容量管理只是为 6–12 小时离线预生成做准备；**生成回压阈值尚未修改**。
- 已写入 GitHub，但未取得 Windows 实机测试、CI 或端到端测试成功证据。

## 建议验收
1. 验证界面统计的是完整库存而不是最近 100 条。
2. 用很小的 `AI_LIVE_STUDIO_LIBRARY_MAX_GIB` 测试超限保护，并确认旧 WAV 未删除。
3. 模拟真实磁盘剩余空间不足，确认失败状态准确。
4. 播放正在引用某个 WAV 时尝试删除，必须被拒绝；停止任务后再次确认清理。
5. 删除非活动条目需用户确认，清理后容量统计立即下降。
6. 检查 Session reset、播放 ACK、Audio Cache 重启都不会误清理归档。
7. 运行：`python -m unittest tests.test_audio_library_capacity tests.test_audio_library tests.test_library_replay tests.test_replay_journal tests.test_generation_playback_split`，然后进行完整 Windows TTS/WinMM 联合验收。
