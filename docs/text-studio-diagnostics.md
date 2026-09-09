# Text Studio 泛化诊断

诊断入口为 `server/text_studio.py` 的 `/api/generalize`。前端额外传递项目 ID 和原稿中从 0 开始的段落索引，不改变 Prompt、模型、切分、批量大小或结果保存逻辑。

每次实际 provider 调用使用独立 run ID，避免批次重试覆盖原始证据：

- `runtime/text-studio/logs/<run_id>-batch-NNN.json`：项目、段落范围及精确索引、输入规模、时间、状态、原始错误、CLI 退出码和模型。
- `runtime/text-studio/debug/request/<run_id>/batch-NNN-request.json`：完整 Prompt、命令、候选数及超时配置。
- `runtime/text-studio/debug/response/<run_id>/batch-NNN-response.txt`：完整 stdout，解析之前保留。
- 同目录 `batch-NNN-stderr.txt`：完整 stderr，包括 Codex 运行头和错误。超时则保留捕获到的部分输出。

`batch-NNN` 根据首段在原稿中的索引按 8 段编号；恢复时若待处理段落不连续，以 `paragraph_indexes` 为准。运行期间状态为 `running`，结束后为 `success` 或 `failed`。进程意外退出可能留下 `running`，不等于仍在执行。日志中的模型从实际 stderr 运行头提取；运行头缺失时标为 `unknown`。请求快照是执行前状态，模型可能仍为 `unknown`。

`input_chars` 为原稿字符数，`prompt_chars` 包含提示词与 JSON 包装。`estimated_tokens` 以 Prompt 字符数粗估（1 token/字符），不是精确 tokenizer 计数，不能直接据此判定超长。

日志含直播稿原文和原始 provider 输出，属于运行数据，不放入源码目录。不自动清理，删除需明确授权。

修改后需重新启动 Text Studio 服务以加载后端诊断代码；已有运行进程不会热加载。启动方式仍为 `bash scripts/text-studio-start.sh`。此次诊断保留原 8770 服务，临时 8771 实例仅用于复现，结束后停止。原服务下次正常重启后启用日志。

运行验证：`python3 -m unittest discover -s tests -p test_text_studio.py`。新增诊断测试覆盖成功、schema 错误及超时原始输出保留，并保留其临时诊断目录。

2026-09-09 诊断报告位于 `runtime/text-studio/diagnostics/report.yaml`，环境证据在同目录 `environment.json`。结论：Codex CLI 0.149.0 调用 gpt-6-astra 被上游以版本不兼容拒绝，第 1 批和同稿单段均失败。未执行升级或泛化逻辑修复。
