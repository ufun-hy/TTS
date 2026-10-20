# Windows 单机化迁移｜main 业务保真与待批准偏差（2026-10-20）

## 基准与执行约束
- 依据《Windows 单机化重构：模块盘点与迁移计划》与《TTS迁移差距审计》。
- 来源基准 `main@35d92fb73efdea589423d4a9a1e4fc81c79a56ed`；Windows 工作分支 `codex/windows-offline-runtime-v1`。
- 主干业务测试原文不得改写或跳过；仅在获得明确批准后才能调整涉及架构偏差的测试导入路径及预期；Windows 差异应进入适配层。
- 不创建新的分支、worktree，不修改 main；此文档是**待审批**偏差清单，未经用户确认不得算作通过。

## 当前真实测试结果
- 原审计：`cecba99` 时 3/12 原版业务测试文件 PASS，9/12 FAIL。
- 现有另一次工作提交：`39d11bc` 时 5/12 PASS，7/12 FAIL，Actions: https://github.com/ufun-hy/TTS/actions/runs/39393879033
- 已 PASS 的原版文件：`test_context_variants`、`test_live_session_blocks`、`test_semantic_tts_blocks`、`test_context_live`、`test_first_round_preparation`。
- 未通过的原版文件：`test_prepared_live_session`、`test_live_session`、`test_prohibited_reviews`、`test_prohibited_speech`、`test_text_studio`、`test_tts_client`、`test_tts_gateway`。此处按文件计数，一个文件可有多项失败原因。
- Windows Installer 与 Patch 在 `39d11bc` 上 Actions 均 PASS；但这**不等价于** main 业务保真或 Windows 实机验证通过。

## 已核实的迁移状态
- `server/first_round_plan.py`、`server/prepared_live_session.py` 与主干 blob 相同；`server/first_round_preparation.py` 只在旧 `timeline.tts_client` 导入路径上有适配差异。
- `web/text-studio-variants.js`、`web/text-studio-reviews.js`、`web/text-studio-generation.js`、`web/text-studio-preparation.js` 与主干 blob 相同；原业务测试 `test_context_live.py`、`test_prohibited_reviews.py` 文件原样存在。
- 上述文件一致不代表浏览器入口和 Windows 直播控制全部连通，需保持测试验证。

## G1–G6 缺口与复核
| ID | 项目 | 现状 | 下一动作 |
| --- | --- | --- | --- |
| G1 | 首轮准备/边泛化边准备 | 后端已移入、原 `test_first_round_preparation` 通过；`test_prepared_live_session` 尚未通过 | 保留原版逻辑，查清 import 兼容及真实生命周期 |
| G2 | 整组版本直播 | 原 `test_context_live` 当前通过 | 继续 Windows 生成/播放分离与实机行为验收 |
| G3 | 跨单元违禁话术 | 原测试仍失败，包含浏览器编码、试听 HTTP 502 等问题 | 分开排除编码、协议和业务确认链缺陷 |
| G4 | 音频内容哈希 | 需以 `test_live_session` 对 `source_audio_sha256` 原断言复验 | 原测试不改，保留元数据契约 |
| G5 | 页面入口及原版 JS | 原版 4 个前端模块已经存在，但 `test_text_studio` 仍失败 | 按 main 交互保持原版，再做 Windows 挂接 |
| G6 | main 提交差异 | 当前分支仍相对 main behind 24；已有原样搬运和部分适配，不等于完整 merge | 先逐文件核对冲突与行为，禁止盲目覆盖 Windows 文件 |

## 待用户批准偏差草案（**全部未批准**）
| ID | 主干原测试预期 vs Windows 架构差异 | 当前处置 |
| --- | --- | --- |
| D1 | `timeline.tts_client` 改迁 `server.tts_client`，原测试 mock 旧路径失败 | 建议允许仅变更测试的导入/mock 目标路径，保留其他原测试断言 |
| D2 | 旧 `/speak` 返回 404，Windows 使用 `/synthesize` | 建议允许测试接口路径按 Windows 新协议适配，保留响应/授权断言 |
| D3 | Gateway 的限流 429 vs main 测试预期 200 | **待调查/待批准**，不更改测试，也不擅自修改限流行为 |
| D4 | 高水位回压导致主干 `test_high_buffer_does_not_pause_generation` 失败 | **待用户决策**是否保持高缓存持续生成；不能默认改变播放端保护 |
| D5 | 主干试听测试桩返回非 WAV，Windows 试听严格校验 WAV | **先调查**，区分测试替身与真实合成错误；未经确认不改断言 |

## 环境诊断
- 原测试里的 Node 子进程读取中文脚本输出，在 GitHub Windows runner 的 cp1252 locale 下触发 `UnicodeDecodeError`，掩盖实际规则比对。CI 已设 `PYTHONUTF8=1`、`PYTHONIOENCODING=utf-8`；这仅修复编码环境，**没有**改原测试文件。
- 单独增强 `scripts/audit_main_business.py` 的失败打印，保留完整原测试 artifact，用于诊断剩余真实失败；不新增通过条件或跳过条件。

## 完成标准
1. 每份 main 业务测试 PASS，或者仅涉及**用户明确批准**的偏差且在清单中有证据。
2. 先具备本地 Python 3.11 原测试执行证据，再视结果推送下一批代码；当前 GitHub Action 不能代替本地实机。
3. 不宣称 Windows 真机合成/WinMM 端到端已通过，除非有真实设备日志和验证记录。


## 2026-10-20 用户实测反馈与落实情况

用户独立复核原测试 12 文件与 main 原文一致，当前 5/12 结果复现；G2、G4 已确认恢复。

- **D3 改归类为真实回归，不再列为待批准偏差**：移除旧 `/speak` 时误将每分钟 30 次限流套在内部 `/synthesize`，不符合 main 的批量合成契约；已通过 `d973bec8` 删除 `/synthesize` 限流，保留本地地址、API Key、已知音色和请求体校验。真实批量直播效果待 Windows 实机复测。
- **D1 优先使用兼容别名，不修改原业务测试**：`23107f7` 将 `timeline.tts_client` 作为 `server.tts_client` 的模块别名，不复制核心逻辑；可解除旧测试 import/mock 阻塞并暴露更真实的失败。审计流程已配置在别名文件变化时重跑。
- **D4 回压策略仍需明确产品决策**：主干不按水位/ACK/客户端在线情况暂停准备或生成；Windows 仍有回压。建议目标为持续预生成，直到库存配额上限，再按容量暂停并向用户提示；此项暂未动任何回压业务代码、未调整测试预期。
- **D5 未批准**：main 历史测试使用 `/speak` 样式的 `{"success":true}` 测试响应，而 Windows `/synthesize` 返回 WAV 并要求 RIFF。需要继续分析模拟协议及断言，不能直接削弱 Windows 音频校验。
- **G5/G6 未完成**：原版页面完整入口和 main 24 个提交差异仍需文件级适配/审计；不直接覆盖 Windows 页面或播放器。

本轮修改 `d973bec8`、`23107f7`、`c0d8180`；原 12 份 main 测试未改动。新的完整保真审计：https://github.com/ufun-hy/TTS/actions/runs/39399381250

## 2026-10-20 首轮准备、持续直播及 D4 容量门控复修

### 执行环境与基线
- 工作分支：`codex/windows-offline-runtime-v1`，工作树：`/Users/ufun/code/2026/TTS-windows-offline-runtime-v1`；执行前 HEAD 为 `5808008`，工作区干净。
- 当前机器没有运行中的 TTS 服务，`AI_LIVE_STUDIO_DATA` 未设置；代码默认数据目录为当前工作树 `runtime/`。该目录含既有模型、日志和运行数据，本轮未清理或修改。
- 使用 macOS 上的 Python 3.11.15 执行本地测试；不是 Windows Python，也没有 CosyVoice3 / WinMM 真机验证。
- 修改前 12 份原版测试为 6/12 PASS。完整逐文件结果和日志在 `/tmp/tts-windows-main-business-baseline-20261020/summary.json` 及同目录各测试 `.log`。

### 本轮修复
- `build_live_manager()` 改为创建已有的 `SynthesisBlockLiveSessionManager`，支持完整首轮准备、已准备音频 handoff、连续候选轮次和准备状态返回；保留 `autoplay=False` 及独立播放控制。
- 恢复可暂停、可停止的 TTS 暂时性错误重试，并将 Gateway 单次请求设为 `retries=0`，避免客户端内部不可中断的等待。连续失败最多重试 5 次（最多 6 次尝试），之后将 Session 标为失败；首轮后台准备使用相同上限。
- 生成结果在停止后不会进入 Audio Cache；内容、sequence 和候选轮次在重试中保持不变。
- 用户已明确批准 D4 容量方案。生成不再因 ACK、客户端在线状态或缓冲水位暂停；官方管理器在入库前检查归档库存和实际磁盘余量，空间不足时显示已用量/上限并等待容量释放。停止可以立即打断等待；不自动删除库存。容量门控按 Audio Processor 的最慢速度估算 WAV 增长，并沿用现有 20 GiB 默认库存上限与 16 MiB 磁盘余量。
- `AudioLibrary.capacity()` 的磁盘用量结果最多缓存 1 秒，并在本地归档新增或显式删除后失效，避免生成过程中重复扫描库存目录。

### 12 份原版测试对照

| 原版测试 | 修改前 | 修改后 | 修改后失败原因 / 分类 |
| --- | --- | --- | --- |
| `test_context_variants.py` | PASS | PASS | — |
| `test_live_session_blocks.py` | PASS | PASS | — |
| `test_semantic_tts_blocks.py` | PASS | PASS | — |
| `test_context_live.py` | PASS | PASS | — |
| `test_first_round_preparation.py` | PASS | PASS | — |
| `test_prepared_live_session.py` | FAIL（8 failures、2 errors） | PASS | 恢复准备管理器接线、重试与首轮状态字段；首轮失败不再误作完成。 |
| `test_live_session.py` | FAIL（7 failures） | PASS | 恢复可中断重试、暂停/停止；D4 已按用户批准切换为容量门控。 |
| `test_prohibited_reviews.py` | FAIL | FAIL | D5：原测试桩返回 JSON，Windows 试听保持严格 WAV 校验；其中 3 项 errors。 |
| `test_prohibited_speech.py` | FAIL | FAIL | D5：原测试桩返回 JSON，Windows 试听保持严格 WAV 校验；1 项 error。 |
| `test_text_studio.py` | FAIL | FAIL | D5 非 WAV 测试桩导致 1 项 error；G5 页面检查仍缺少“准备 1 轮并开播”旧入口文案。 |
| `test_tts_client.py` | PASS | PASS | — |
| `test_tts_gateway.py` | FAIL | FAIL | D2：旧 `/speak` 路径返回 404，2 项 failures；未恢复 Mac 接口。 |

修改后共 8/12 文件 PASS，4/12 仍失败。审计日志在 `/tmp/tts-windows-main-business-after-20261020/summary.json` 及同目录各测试 `.log`。原版测试快照和断言没有修改或跳过；当前 Windows 专项测试中与获批 D4 旧策略冲突的断言已更新为容量等待验证。

### D2 / D4 / D5 处置
- **D2 待单独批准**：`/speak` 不存在是 Windows 协议差异；本轮保留 `/synthesize`。
- **D4 已批准并实施**：原 `LiveSession` / `SynthesisBlockLiveSession` 的高水位与断连等待已移除。库存满或剩余磁盘不足时，当前音频暂留在生成线程，状态显示容量原因；用户删除旧库存或释放磁盘后自动重试入库。客户端缓存空间没有独立配额字段，因此以服务端现有库存配额及文件系统余量为门控依据。
- D4 门控位于正式音频入库边界；首轮准备服务和 `PreparedLiveSession` 会先将有限的首轮 WAV 写入既有准备目录，20 GiB 归档配额不统计这些工作文件。入库前的文件系统余量检查会计入它们当前占用，但不会在首轮准备期间提前按归档配额暂停。
- **D5 仍未批准**：未改变原测试桩、未放宽 RIFF/WAV 校验。
- **G5 仍在范围外**：本轮没有重做页面入口和前端完整流程。

### 验证与限制
- 修改前和修改后均运行了 `scripts/audit_main_business.py` 的全部 12 份 `main@35d92fb` 原版测试。
- 按 `.github/workflows/windows-functional-regression.yml` 两组命令复跑：第一组 79 项通过，第二组 60 项通过。原始日志分别为 `/tmp/tts-windows-regression-1-20261020.log` 和 `/tmp/tts-windows-regression-2-20261020.log`。
- 本机环境只能验证 Python 逻辑，不能证明 Windows 音频播放、CosyVoice3 Vulkan 推理或真实磁盘表现。
- 容量门控由官方 `build_live_manager()` 创建的生产管理器启用；直接构造管理器的测试/调用方默认不启用该门控。
- 阶段 2 尚未完成：D2、D5 未获偏差批准，G5 页面入口仍待独立修复；不以本轮 8/12 结果宣布整体 PASS。


## 2026-10-20 `2c6244f` 提交审阅与修正

审阅结论：原提交存在阻断项，修正后才允许推送。按 Windows 单机单个生成任务、独立播放和长时间库存生成检查。

1. **P1 — 首轮音频写进源码目录**：生产 factory 改为 PreparedLiveSession 后，默认路径仍是安装目录/runtime/prepared-live；Program Files 无写权限时首轮失败。改为 AI_LIVE_STUDIO_DATA/prepared-live，显式测试目录仍可覆盖。
2. **P1 — 暂停期间仍可发送**：容量 HTTP 查询返回后没有重新检查暂停；查询中点击暂停会继续入库。合并各循环的交付步骤，并在容量查询后重新等待手动恢复。
3. **P2 — 停止时库存计数少一段**：入库成功后恰逢 stop，先退出再计数。现在只在确认交付成功后递增 sequence 和生成数量，即使 stop 已请求，也准确记录已落盘库存；停止前未开始交付的合成结果仍丢弃。
4. **P1 — 容量检查与落盘竞争导致失败**：预检查通过后，其他写入占用磁盘/库存，store 抛错会将片段标 failed。落盘处使用实时检查，容量不足返回 507，保留已处理 WAV 为 pending，重试同一 ID，不再次合成或处理 WAV；恢复后只归档一次。预估磁盘峰值计入队列、处理副本与归档。
5. **P2 — HTTP 缓存桥丢失音频身份元数据**：source_audio_sha256、round_position/round_total、looping/session_final 在服务端白名单被丢弃。现保留这些字段，并按实际收到的音频计算哈希。

生产边界回归：tests/test_windows_live_review.py。前三项在修正前均失败，日志 /tmp/tts-review-before-20261020.log；修正后连同实际 HTTP 507 恢复共 4 项通过，日志 /tmp/tts-review-boundaries-20261020.log。原版审计保持 8/12，日志 /tmp/tts-review-main-business-20261020/。测试已加入 Windows 3.11/3.13 workflow。D2/D5 尚不计为原版通过，后续独立适配。
