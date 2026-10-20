# 上下文整组泛化与连续合成

2026-09-23，直接在 `main` 实施，基线 `a1b4440`。没有创建 branch/worktree、提交或推送，没有重启现有生产服务。分析依据见 [初始方案](context-variant-synthesis-plan.md)。

## Current Flow

旧项目继续使用原有路径。Text Studio 新增“复制为整组试验项目”：保存原项目后创建副本，按原稿建立上下文组，点击泛化逐组生成。新模式为主动选择，历史候选不自动迁移成整组版本。

新项目链路：编辑单元 → 上下文组 → 多套完整 Variant → 每轮按组选一个版本 → 恢复连续文本 → 禁止播报句过滤 → 自然标点分块 → 现有 CosyVoice/Gateway、Audio Cache、Windows。

## Root Cause

- 原泛化每批最多 8 个单元，但没有“同编号候选共同生成”的契约；单段重生成进一步破坏关联。
- 后端独立随机每段；当前页面实际只传每段当前选中稿，所以从页面启动一般重复同一稿。新模式提交全部完整组版本，后端按组选择。
- 旧路径先 200 字硬切，再按编辑单元加入换行并合并；新组路径绕过这两步。
- 原禁止播报过滤会压缩候选数组，不能拿过滤后的下标绑定版本。新路径不改变候选槽位，选定整组后对连续文本过滤。

## New Generalization Structure

`server/context_variants.py` 负责分组、提示词、严格校验、候选映射和整组选择。`server/semantic_tts_blocks.py` 负责文本连续化、过滤后的来源映射和自然分块。Provider 的执行与诊断沿用 Text Studio，播放/队列继续使用现有实现。

新增 API：

- `POST /api/context/groups`：以完整顺序建立稳定组。
- `POST /api/context/generalize`：一次生成一个组的全部版本；不完整输出直接拒绝。
- `POST /api/context/polish`：事实修正后，向模型提供一整套版本，只允许润色指定成员；非目标单元被改写则拒绝结果，保留确定性修正。
- `/api/live/start` 在 `segments` 之外携带 `context_groups` 时进入整组模式；旧请求继续兼容。

网页保留单句编辑、搜索、风险定位、事实检查。候选按钮切换整个组；重生成产生可审阅预览，点击应用才替换整组，不直接覆盖旧编辑。生成期间内容变化则拒绝过期结果。风险扫描和禁止播报定位覆盖所有候选，精确编辑器按对应 Variant 定位和保存。

## Variant Data Structure

schema v2 在原 `paragraphs[].candidates` 外增加 `context_groups`：

```json
{
  "id": "g0001",
  "revision": 1,
  "paragraph_ids": ["p0001", "p0002"],
  "source_fingerprint": "sha256-of-ordered-source-inputs",
  "selected_variant_id": "g0001-r1-v1",
  "variants": [
    {"id": "g0001-r1-v1", "candidate_index": 0},
    {"id": "g0001-r1-v2", "candidate_index": 1}
  ]
}
```

候选文字仍只有一份；组内同一槽位来自同一套生成结果。模型返回 `variants[].segments[{id,text}]`，每套必须按顺序覆盖所有成员一次。缺失、重复、额外 ID、空候选、错版本数均拒绝。组覆盖、槽位与来源 fingerprint 在 Live 启动前校验；输入变化需重新生成组。项目可保存待重新生成的状态。

保存、恢复、备份 JSON 和 Live 请求都携带组信息。旧编辑器试图在保存时丢弃已有组信息会报错，不静默降级。每轮选择记录可通过 `/api/live/status` 的 `variant_selection` 查看，避免重复整轮时更换完整组。

## Context Grouping Strategy

顺序扫描原稿，利用句末强标点、来源段落和已有事实/承接规则。约 120–240 字处优先自然收束；通常数个编辑单元，短附和句允许超过 6 个单元。累计超过约 360 字或 30 个成员时结束组，但不拆开一个编辑单元；单个长单元允许独立成组。每次生成带前后相邻单元的只读上下文。

组先建立再生成；续作只处理未完成组，不重新打包稀疏单元。每个组完整校验和保存，失败保留已完成组。多组的同编号版本不是一次全文联合生成，不声称其具有全文级协同保证。

## Semantic TTS Block Strategy

- 中文相邻单元直接按原有标点拼接，不新增编辑边界换行。ASCII 字母/数字单元之间保留分词空格；原文本显式空白保留。
- 先恢复连续文本并过滤完整禁止播报句，支持跨编辑单元匹配；保留段 ID、组 ID、Variant ID 和块内来源区间。
- 目标 180 字，默认硬上限 200 字。优先强标点，其次逗号/顿号/空格；可选择 180 字之后、200 字以内的自然结束点。
- 避免数字内部逗号和引号/括号内弱停顿；句末闭合引号留在前块。无安全边界的超长文本才硬切，块标记 `hard_cut`。
- 动态时间占位符不会被分开，仍在请求前展开并再次检查上限。现有三个占位符的展开结果比占位符短；重试复用同一文本。
- 如果某套版本过滤后丢失完整成员，Live 启动会指出组和 Variant，要求先修改；不会压缩候选下标或混入另一版本。

旧项目维持旧分块策略，便于对照与兼容；新组模式在普通 Live 和实际 SynthesisBlock 两入口均使用新分块。

## A/B Listening Result

**真实文本生成与音频生成已完成；尚未收到人工听感评分，不能宣称 C 已明显接近 A。**

使用真实项目“女装风衣9.23”，音色 `speaker_c`。新文本经现有 Codex Provider 的 `gpt-5.6-terra` 生成，Provider 日志确认模型；直接通过现有 Gateway `/synthesize` 合成，不修改模型/backend/音色设置，不进入 Live/Windows 缓存队列。旧 B 使用项目已有候选，因此对照仍包含旧/新生成批次差异。

短片段 `p0003–p0009`，原稿 134 字，1 个上下文组、3 套 Variant。每份均为单次合成：

| 版本 | 字数 | 音频时长 |
| --- | ---: | ---: |
| A 原稿 | 134 | 26.88 s |
| B1 独立候选 + 旧块 | 150 | 26.36 s |
| C1 整组 + 自然块 | 129 | 20.68 s |
| B2 | 157 | 31.04 s |
| C2 | 132 | 25.52 s |
| B3 | 141 | 27.00 s |
| C3 | 126 | 23.72 s |
| B-ui 页面当前选中稿 | 146 | 25.84 s |

长片段 `p0003–p0032`，30 个单元，分为 5 个上下文组，各生成 3 套 Variant。已合成 A/B1/C1；其余版本已保留文本计划，可按需合成：

| 版本 | 每块字数 | 拼接时长 |
| --- | --- | ---: |
| A 原稿自然块 | 186 / 184 / 178 / 176 | 139.36 s |
| B1 旧方案 | 173 / 157 / 168 / 162 / 86 | 132.92 s |
| C1 新方案 | 192 / 138 / 176 / 172 | 139.60 s |

长稿 A 受 Gateway 上限约束，是原稿自然分块，不是全文一次合成。长稿 C 块间边界均为强标点，无硬切；这些客观指标不代表听感评分。

本机产物（不入 Git）：

- 短稿：`runtime/ab/context-variants/20260923-183503-eb704e/`
- 长稿：`runtime/ab/context-variants/20260923-184519-ed02cc/`
- 两目录均有 `manifest.json`、原始输入、模型请求/结果、单块 WAV、连续 WAV、`listen.html` 和 `listening-key.json`。
- 试听页用随机编号隐藏方案标签，支持下载评分记录；音频通过 ffmpeg 转为 24 kHz 单声道 PCM16 后原样拼接，无裁尾、淡化或额外静音。已验证 11 份连续 WAV 格式、时长和非空帧。
- Gateway 的随机种子未由接口固定；相同文本可能命中既有 TTS Result Cache，WAV 响应不提供命中标识。未删除或绕过缓存，未把音频复用视为独立采样。

复用实验脚本：

```sh
python3 scripts/context-variant-ab.py --project runtime/text-studio/projects/PROJECT_ID/project.json --start 3 --units 7
python3 scripts/context-variant-ab.py --synthesize runtime/ab/context-variants/RUN_ID/manifest.json
# 仅合成长稿选定版本：
python3 scripts/context-variant-ab.py --synthesize runtime/ab/context-variants/RUN_ID/manifest.json --labels A-original B1-independent C1-context
```

第一条调用项目 Provider 生成文本并落盘；第二条才读取现有 TTS 凭证进行合成。脚本不会启动直播或自动播放。重跑已完成案例会依据 manifest 校验文本/音色身份并复用文件，不删除已有产物。

## Changed Files

- 新增 `server/context_variants.py`、`server/semantic_tts_blocks.py`：组与分块业务逻辑。
- 修改 `server/text_studio.py`：组 API、schema v2 保存及 Live 请求。
- 修改 `server/live_session.py`、`server/live_session_blocks.py`：新组选择、分块和状态追踪。
- 新增 `web/text-studio-variants.js`；修改 `web/text-studio.html`、`web/text-studio-extension.js`、`web/text-studio-search.js`：编辑、保存、事实修正、风险精确定位。
- 新增 `scripts/context-variant-ab.py`：真实对照实验。
- 新增 `tests/test_context_variants.py`、`tests/test_semantic_tts_blocks.py`、`tests/test_context_live.py`、`tests/context_variants_browser_checks.js`。
- 新增本说明与初始分析方案，更新 README。

用户已有的两份 playback diagnosis 文档改动保持原样。未修改 Windows buffering/rebuffering、300/180 秒背压、claim lease、播放器、Gateway 429 修复、CosyVoice 模型/backend 或音色克隆。

## Tests

- `python3 -m unittest discover -s tests`：215 项，3 项按条件跳过，其余通过（Windows WinMM 实机测试及 opt-in launchd 集成不在本次执行）。
- Node 搜索与风险测试通过；页面内脚本和新增/修改 JS 语法检查通过。
- 新增核心测试覆盖严格映射、完整组选择、来源失效、过滤不重排、跨单元禁止句、自然边界、引号/数字/动态占位符、200 字上限、两个 Live manager、保存恢复、HTTP 生成及润色非目标保护。
- ego-browser 在独立 `127.0.0.1:18773` 和独立数据目录执行 15 项流程检查通过，含原项目保留、整组切换、单句编辑、保存恢复、完整 Live 请求、两轮组选择、隐藏候选风险编辑、重生成预览/应用。该 UI 检查使用 Provider/TTS 替身；真实 Provider/TTS 证据来自上述独立 A/B/C 实验。
- 截图及 UI 验证记录留在 `runtime/ab/context-variants/browser-check/`。没有删除临时文件或测试产物。

## Remaining Risks

1. 听感验收仍未完成；短 C1 时长比 A/B 短，需听是否有语速或信息节奏差异，不能仅按字数/块数判好。
2. 标点与事实关联是启发式，不能证明组间衔接、信息覆盖或事实正确。无标点超长文本仍可能硬切，人工删改也可能削弱连贯性。
3. 部分禁止句删减可改变承接，页面风险检查会显示；完全丢失成员的版本则拒绝 Live。跨组接缝在选定全文上最终再过滤。
4. 原稿/事实修正改变组输入后，需重生成受影响组；历史候选无法无损转换成共同生成的 Variant。
5. 本轮未验证 Windows 实机播放、未做跨平台音频设备验收。播放器和传输协议未改，现有自动回归通过。
6. 现有运行服务未重启，不能把文件已修改等同于生产进程已加载新后端。当前新增路径是显式试验模式。

## Recommended Next Step

先试听短稿 A/B/C，重点比较句间承接、重新起调、自然停顿和直播感，再听长稿块边界。确认 C 达到目标后，安排 Text Studio 服务加载新代码并刷新页面，在现有项目点“复制为整组试验项目”进行实际编辑及 Windows 试听。不自动覆盖已有项目，也不依据自动测试宣布听感验收完成。
