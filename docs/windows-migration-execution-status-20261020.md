# Windows 单机化迁移计划：执行状态与阶段 0 基线交接

更新：2026-10-20
目标分支：`codex/windows-offline-runtime-v1`
原始任务：`Windows 单机化重构：模块盘点与迁移计划.md`

## 当前执行状态

本任务按原始文档定义的顺序执行，不以此前桌面壳、音频库存等旁路功能的提交代替阶段 1–5 的架构迁移。

| 阶段 | 本次判定 | 证据 / 下一道关 |
|---|---|---|
| 阶段 0：测试安全网 | **进行中（未验收）** | 修复 `tests/test_wav_compat.py` 对 Path.stat 参数的兼容；新增 Windows Python 3.11/3.13 回归矩阵；尚无测试运行结果 |
| 阶段 1：砍 Mac/局域网遗留 | 未开始删除 | 阶段 0 通过后再逐模块删除；保留 `audio_cache` 直到阶段 4 |
| 阶段 2：移植 main 六个业务模块 | 尚未执行 | 必须逐项迁移并跑对应测试，不能直接覆盖 Windows 交叉修改 |
| 阶段 3：安全修复 | 尚未执行 | Origin/Host/Content-Type，TTS gateway 异常及认证，离线补丁签名 |
| 阶段 4：单宿主进程化 | 尚未执行 | 新 Host、Windows Job Object、GPU 调度、进程内队列；需实机验证 |
| 阶段 5：文档/分支收尾 | 尚未执行 | 所有阶段通过后才能进行 |

## 已提交的阶段 0 修改

- `tests/test_wav_compat.py`：测试中的 monkey-patch `changing_stat(path, *args, **kwargs)`，传递 Python 3.13 新增或扩展的 stat 关键字参数，避免误报类型错误。
- `.github/workflows/windows-functional-regression.yml`：Windows Python 3.11 / 3.13 矩阵；覆盖本阶段已有桌面、库存、回放测试，并加入 `tests.test_wav_compat`。
- 本轮读取 GitHub 最新提交检查时，GitHub 返回 combined statuses 为空、关联 PR 工作流运行列表为空。**这不是通过，也不能证明工作流执行失败**；需要在 GitHub Actions 页核对 workflow 是否实际触发，且提供 run/job 日志。

## 阶段 0 未完成条件

1. GitHub Actions 真实运行并得到 Python 3.11 / 3.13 两套测试结论。
2. 完整既有 Windows Runtime 测试套件结果与失败清单。
3. Windows 实机安装包/本地环境的功能烟测。GPU/WinMM 不要求在云端 runner 假装通过。
4. 记录稳定基线 Commit 及可复现的运行环境。
5. 若测试失败，仅修复确认属于兼容和回归的问题，不跳过测试宣布 PASS。

## 严格顺序与安全约束

阶段 0 通过前，不大规模删除旧 Mac/网络模块，不迁移运行时入口或更换 Audio Cache，更不修改 main；避免将功能回归和大规模迁移交织在一起。

后续阶段 1 删除前须逐条核对导入、脚本执行入口和打包清单。尤其 `recording_transcript/windows_worker.py` 是动态子进程入口不可删除，`audio_cache/processing.py` 的音频变速/增益处理必须保留。

## 结论

**真实进度：原始计划阶段 0 已开始落实，但尚未通过完成门槛。** 之前的桌面壳与库存功能只代表另外一批代码改造，不能视为“阶段 4 单宿主已完成”。
