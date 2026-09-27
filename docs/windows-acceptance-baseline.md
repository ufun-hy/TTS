# Windows 验收固定基线

固定时间：2026-09-27，Asia/Shanghai。

这份记录冻结本轮 Windows 单机验收的起点。`main` 下面 7 个提交按当前顺序记录；Windows 单机工作目录开始修改前的 HEAD 是 `d7a24c3d19bacb7fc4ad4d899f78cad6f4920440`。后续提交不改写这些对象，验收报告需注明实际测试的提交。

## main 的 7 个提交

| 顺序 | 提交 | 说明 |
| ---: | --- | --- |
| 1 | `d8bf8a7d5aeb4c02b176ca19705438ff4972b89f` | docs: record branch consolidation, validation and Windows follow-up scope |
| 2 | `5ae665d924ba8437eb1778a56635d149580416b4` | fix(windows): reduce cached playback switch latency |
| 3 | `ebd96996b8f2e42690bdfb51aba66c750ebfcc07` | docs(playback): preserve dated switch-latency evidence and isolated probe |
| 4 | `1c5ea97e1d44c0763f63838909ba0bd7be7f0e76` | feat(studio): preserve reviewed candidates and reusable first-round preparation |
| 5 | `9a3bb186e518ccb007ad04785cf2e5ebe113e007` | fix(studio): preserve replacement across all candidate texts |
| 6 | `e65e4da05a040cec20e30d095ff49cb42d09cacd` | feat(live): decouple generation from Windows buffer watermarks |
| 7 | `1ca906f5cd83e96bc604e7b88c833e301f82f562` | chore(mac): preserve engine compatibility profile and voice registrations |

## Windows 分支起点

| 对象 | 固定值 |
| --- | --- |
| 分支 | `codex/windows-offline-runtime-v1` |
| HEAD | `d7a24c3d19bacb7fc4ad4d899f78cad6f4920440` |
| 工作目录 | `/Users/ufun/code/2026/TTS-windows-offline-runtime-v1` |
| 起点状态 | 跟踪、未跟踪和 ignored 检查均干净 |
| 远端 | `origin/codex/windows-offline-runtime-v1` 指向同一 HEAD |

## 验收顺序

先验收 Windows 单机的“开播 → 暂停 → 继续 → 停止 → 换稿再开播”会话生命周期和严格会话绑定，再验收已从 `codex/windows-playback-gap` 整合的播放调度优化。首轮准备、全部候选随机播报和更大范围的功能迁移必须建立在这两项通过后。macOS 上的模拟测试不能替代 Windows WinMM 真机结果。
