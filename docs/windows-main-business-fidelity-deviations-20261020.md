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
