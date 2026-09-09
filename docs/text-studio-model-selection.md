# Text Studio 模型选择

在「泛化设置」中选择 Provider 与 Model。默认 Provider 为 Codex；Model 的「跟随默认配置」保留现有行为，不传模型覆盖参数，也不改全局 Codex 配置。旧项目没有 `model` 字段时同样跟随默认配置。

选择模型会自动保存到当前项目的 `model` 字段，刷新和重新打开项目时恢复。泛化全部段落和重生成单段均传递所选模型；批量执行时 Provider/Model 控件不可修改。模型选择不会清除已有候选；需要比较其他模型时使用「重生成本段」。JSON 导出也记录当前模型配置。

列表由当前执行的 Codex CLI 的 `app-server` 提供：初始化后读取 `config/read` 和 `model/list`，处理分页，仅显示支持文本的可见模型。页面同时显示默认配置实际指向的模型 ID；点击「刷新模型列表」重新读取。模型目录表示当前 CLI 提供的选项，最终是否可调用仍由账号权限、CLI 版本和服务端状态决定。列表读取超时为 20 秒，不会自动发起模型生成。

列表读取失败、默认模型不在列表中、项目保存的模型已不在列表中时，页面显示说明。不会静默切换到其他模型；仍保留项目的选择。实际调用失败会显示所选模型和 provider 的原始错误原因。

`GET /api/models?provider=codex` 返回 `models`、`default_model`、`selection_supported`、`source` 和 `error`。`POST /api/generalize` 可选字段 `model` 为空或缺失时保持旧命令不变；非空时以 argv 形式传给 `codex exec --model MODEL`，覆盖命令中原有的模型选项，其余参数保持不变。不会经过 shell 拼接执行。

原生 `codex exec` 支持模型选择。已有 ChatGPT 自定义命令和非原生 Codex wrapper 没有标准模型列表或参数契约，保留默认调用，界面提示不支持选择，后端拒绝忽略非空模型。原生命令的 config/profile/cwd 参数会用于发现；OSS 等独立配置模式暂不提供匹配的目录，给出明确提示。未选择模型时，这些自定义命令照旧运行。

职责安排：

- `server/text_studio_models.py`：模型发现、参数校验与 Codex 命令构造。
- `server/text_studio.py`：HTTP 接口、泛化调用与项目持久化。
- `web/text-studio.html`：选择控件、加载提示及项目恢复。
- `tests/test_text_studio_models.py`：默认兼容、命令覆盖、分页发现、失败说明与模型持久化测试。

诊断日志的 `requested_model` 表示用户选择，`model` 表示 Codex 实际输出的模型。原始请求日志保留实际执行命令；两者可对照验证。运行数据在 `runtime/text-studio/`，不改变文本切分、Prompt、候选生成或 TTS 流程。

验证命令：`python3 -m unittest discover -s tests -p 'test_text_studio*.py'`。

接口依据：[Codex App Server 模型列表](https://developers.openai.com/codex/app-server#list-models-modellist)。CLI 参数另以本机 `codex exec --help` 验证。

## Gemini CLI

Provider 新增「本地 Gemini CLI」。安装/更新官方稳定版：`npm install -g @google/gemini-cli@latest`；本次安装版本为 0.59.0。首次使用在终端运行 `gemini`，完成 Google 登录或按官方方式配置认证，然后回到网页刷新模型列表。

Gemini 模型列表通过本机 `gemini --acp` 的 `initialize` / `session/new` 读取当前账号的 `availableModels` 和 `currentModelId`，不会使用硬编码型号冒充可用模型。未认证时显示 CLI 的错误与登录提示；现有项目的模型选择仍保留。列表刷新创建一个无 prompt 的 ACP 会话，不执行泛化。

默认命令为 `gemini --output-format json`（非 TTY，通过 stdin 传入原 Prompt），可用 `TTS_TEXT_STUDIO_GEMINI_CMD` 覆盖。明确选择时传 `--model`，不选择时保留 Gemini 的自动路由和默认配置。JSON 外层 `response` 才是模型文本，解析后进入原有结果校验；完整 stdout/stderr 仍保留。日志 `requested_model` 是选择的 ID/别名，`models_used` 来自 CLI 的 `stats.models`，可能包含自动路由或辅助模型，不能视为单一最终回答模型。

认证错误不会被当作空 candidates 或 JSON schema 错误。原生 Gemini 支持选择，非原生 wrapper 不推断参数协议。未完成认证前，只能验证认证失败路径、模型持久化和模拟返回解析，不能声称真实生成通过。

参考：[Headless JSON 返回](https://geminicli.com/docs/cli/headless/)、[模型选择](https://geminicli.com/docs/cli/model/)，ACP 字段按已安装 0.59.0 的接口实现核对。

## Antigravity（agy · Gemini）

使用 Antigravity 账号时，Provider 选择「Antigravity（agy · Gemini）」，而非独立 Gemini CLI。两套 CLI 的认证独立；无需为 agy 再配置 Gemini API Key。

模型列表实时调用 `agy models`，读取 tab 分隔的模型 ID 和显示名称，展示 Antigravity 账号返回的全部可用模型（包括 Gemini、Claude 和 GPT-OSS）。默认模型从 `~/.gemini/antigravity-cli/settings.json` 的 `model` 字段或自定义命令参数读取，不改动全局设置。项目存储 `provider: agy` 和对应模型 slug，不把独立 Gemini 的 `flash` 等别名迁移成 agy 模型。

通过 `TTS_TEXT_STUDIO_AGY_CMD` 可配置原生 agy 命令。显式选择以 `--model` 覆盖，JSON 返回从 `response` 字段解析。对于 `SUCCESS` 但空返回且存在 `denied_actions` 的情况，明确报告无头模式权限拒绝，不视为生成成功，不自动增加全局权限。agy JSON 未提供实际模型字段时日志标为 `unknown`，所选模型可在 `requested_model` 和命令中核对。

本次检测 agy 为 1.1.27；`agy update` 返回 `update already in progress`，已存在后台 updater，未中断它或删除更新锁，不能宣称升级已完成。

agy 1.1.27 的本次复现实测：`--print -` 未正确接收 stdin 文本，改用 `--print PROMPT` 后同稿返回正确的 3 个候选。服务端在执行前将命令模板的 `--print -` 替换为实际 Prompt 参数，以 argv 执行（不经过 shell），诊断请求记录实际命令。Prompt 内容未改动。默认 `--mode plan` 不自动批准工具操作。
