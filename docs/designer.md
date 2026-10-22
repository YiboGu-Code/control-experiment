# Designer 开发说明

第 3 步先实现 Designer。该角色把公开 instruction 转成需求与设计规格；通过同一个 `Executor` 服务于固定 Loop 和 Scheduler。本文件记录 Designer 步骤；后续已实现 [Art Producer](art_producer.md) 和 [Developer](developer.md)，Player、QA Reviewer 和调度策略尚待开发。

实现位于 `src/adaptive_game_generation/roles/designer/`，统一使用已有 Python 包，不在旧的预留目录中维护第二份实现。

模型协议统一使用英文：角色提示词、请求目标、完成条件、环境说明、反馈、模型回复以及设计 JSON/Markdown 均使用英文。Benchmark 原始 instruction 不翻译、不改写；`source_excerpt` 保留原文标点与换行。调用端应提供英文上下文，本角色不会通过额外模型调用自动翻译任务。本文中文说明仅面向项目开发者。

## 1. 调用与职责

```python
from adaptive_game_generation.roles import Designer
from adaptive_game_generation.runtime import Executor, Services

# workspace 和 task 由 Workspace.prepare 导入公开资料后得到。
# provider 是宿主注入、遵守 TextProvider 协议的文本模型适配器。
executor = Executor.create(
    workspace, task,
    run_id="experiment-001", controller="fixed_loop",
    budget=budget, services=Services(text=provider),
)
request = executor.request(
    "designer",
    "Create the initial English game specification from the public instruction",
    ("Ground requirements in source excerpts", "Cover gameplay, content, assets, and acceptance plans"),
)
result = await executor.execute(request, Designer())
```

正式运行已提供 `OpenAICompatibleTextProvider`，位于 `src/adaptive_game_generation/providers/openai_compatible.py`。它使用异步 SDK 接入 `.env` 中指定的兼容网关，遵守 `TextProvider` 的输入上界、真实用量、取消与超时协议。Designer 本身仍只使用 `RoleTools.generate_text`，不读取凭据、不直接请求网络。原 `LLM/openai/client.py` 的同步接口保留；新适配器沿用其环境变量命名和禁止自动重试的约定，没有用后台线程包装不可取消的同步请求。

每次 Designer 调用至多请求一次模型；不隐式修复 JSON、不自动重试服务失败。下一次是否再次调用仍由控制器决定，消耗保留在共同账目中。原始输入大小、模型输出上限和工具耗时仍受公共执行器约束。

Designer 不能修改游戏、素材、问题单、证据、需求进度或验收结论。角色返回 `completed` 只代表本次设计提交完成，不代表游戏已实现或通过 QA。

## 2. 输入上下文

`RoleContext` 来自执行器锁定的共享状态快照。模型看到的输入经过显式选择：

| 内容 | 规则 |
| --- | --- |
| 原始任务 | 完整 instruction、公开环境约束、任务标识与资源引用 |
| 当前请求 | 目标、完成条件、目标事项、状态版本、固定单次限制与产物引用 |
| 需求 | 全部需求和当前进度，便于保持编号、依据及依赖一致 |
| 已有设计 | 上一版完整结构化规格；旧 Markdown 设计则完整提供正文 |
| 反馈问题 | 有目标事项时选择指定问题及关联需求的问题；无目标事项时选择未关闭且建议 Designer 处理或属于设计/需求缺口的问题 |
| 公开文件 | `input/` 下的文件清单，包括已导入的初始工作区和资源；不会自动遍历 Benchmark 源目录 |
| 显式附件 | 请求引用的文档、报告、日志和配置读取正文；图片、项目目录等只提供引用 |

模型上下文不包含控制器类型、调度历史、账本或隐藏评测资料。文件必须经角色工具读取。图像的视觉理解和资源目录的自动语义检索不在本角色当前能力内；资源清单、环境约束及必要文字附件应由共同的调用端提供。

上下文超出 `DesignerConfig.max_prompt_bytes` 会在模型请求前失败，不静默截断 instruction 或旧需求。两组实验应固定同一输入构造规则、Designer 配置、提示词与模型服务。后续若引入摘要或分块，需要单独版本化并对两组同时应用。

`designer.v3` 在完整 instruction 之外提供原文逐行列表 `source_lines`，帮助模型直接摘录，避免把换行错误替换为空格。列表不替代完整原文，也不放宽后续精确子串校验。历史 `v2` 产物仍可读取，显式选择 `v2` 时保持原提示词构造；同一设计历史仍须使用原来的完整配置。升级验证在新运行目录进行，见 [恢复验证记录](deepseek_restore_review.md)。

## 3. 模型输出与校验

模型返回 `DesignerProposal` JSON，不能直接返回可写共享状态的任意补丁：

| 字段 | 含义 |
| --- | --- |
| `status` | `completed`、`no_op` 或 `blocked` |
| `summary` | 本次完成情况与变更原因 |
| `requirements` | 新增或修订的需求规格：编号、描述、连续原文依据、验收条件、依赖；没有进度与验证字段 |
| `sections` | 新增或修订章节，每章包含正文与关联需求编号 |
| `assumptions` | 明确要求之外的假设；`null` 或省略表示保留，`[]` 表示清空 |
| `blockers` | 仅在 `blocked` 时使用，依赖编号必须属于已有需求或问题 |

首次设计必须包含至少一项需求及八个章节：核心玩法、操作约定、状态转换、内容配置、视觉与 UI、素材需求、实现约定、验收与试玩方案。每项需求必须至少被一个章节覆盖。章节正文允许 Markdown，完整输出须为 JSON 对象。

提交前会拒绝不合法 JSON、重复 JSON 键、未知字段、虚构原文依据、未知依赖、依赖环、缺少章节、未知章节关联需求以及模型越权声明。错误信息不回显模型原文；无效回复会保留模型消耗，但不发布设计或改变共享状态。可以用 `DesignerProposal.model_json_schema()` 和 `DesignDocument.model_json_schema()` 获取输出与产物 schema。

自动检查保证结构和引用有效，不能证明模型在语义上提取了所有需求，或保证某个同源改写没有弱化数量、功能等要求。提示词明确要求保留这些约束，后续 QA 仍须对照原始 instruction 审查完整性；本步骤没有把 Designer 的结果当作官方验收依据。

## 4. 增量修订与产物

需求编号由首次模型输出分配，后续复用。已有编号的 `source_excerpt` 不可替换，不提供删除操作；未提交的需求原样保留。完全相同的描述及原文依据也不能换一个新编号重复插入。现有实现/验证状态由程序保留，模型不能提供这些字段。

后续调用只需返回发生变化的章节和需求。程序将补丁与旧设计合并，生成完整的新版本；未改章节、未改需求及未显式替换的假设继续保留。每次有效修订保存变更摘要、实际改变的需求与章节编号、提供给角色的反馈问题编号。反馈编号仅表示上下文来源，不表示问题已经解决或关闭。

执行器会在设计改变后使旧需求验证结果失效，保留历史证据。Designer 不能凭新的设计文字继续沿用旧验收结论。需求仍未实现的，不会因此变成已实现。

每次有效提交生成两个只属于该调用命名空间的产物：

| 文件 | 用途 |
| --- | --- |
| `design.json` | 完整 `DesignDocument`，包含需求规格、八个章节、假设、版本与配置；作为 `DesignSpec.document_ref` |
| `design.md` | 从同一结构化文档生成的可读版本，含原文依据与逐项验收条件 |

产物带 SHA-256，文件名、引用、创建者与版本由程序生成，模型不能指定写入位置。所有输出通过执行器校验并提交，历史文件保留。设计文档不存需求实现状态，进度以 `SharedState.requirements` 为准。

已有 Markdown 格式的步骤 1/2 示例设计可作为迁移输入：模型需补齐八个结构化章节，旧正文和需求完整保留在输入中。已有 JSON 文档若不符合本角色 schema，则明确失败，不能当作不存在而重建。

`no_op` 不产生文件或新设计版本，仍记录模型消耗。首次没有设计时禁止 `no_op`；与现有规格完全一致的有效补丁也归为 `no_op`。`blocked` 只登记本次阻塞，不写半成品、不自动结束整个运行。当前角色不使用 `partial`。

## 5. 配置

| 配置 | 默认值 |
| --- | --- |
| 提示词版本 | `designer.v3`（英文，保留 `v2` 兼容读取与显式调用） |
| 模型输出上限 | 8,192 tokens |
| 完整提示词上限 | 512 KiB（UTF-8 字节） |
| 模型回复上限 | 256 KiB（UTF-8 字节） |

配置保存在设计 JSON 中，后续调用与已保存配置不一致会失败。公共执行器的总预算、单次预算和文件上限始终优先；角色不会提高调用预算以满足自己的输出上限。公平比较时应在运行开始前冻结整个服务配置，并保持两组一致。

此次提示词与语言约定升级为 `designer.v2`。不要在原 `designer.v1` 运行中混用新提示词；请创建新运行。旧实验产物不自动翻译或重写。

## 6. 真实模型集成测试与审阅

已生成 [Designer 真实输入输出审阅入口](examples/designer_review/review.md)。**真实集成测试已通过**：2026-10-19 使用用户更新的 DeepSeek API 和 `deepseek-flash`，在 `runs/designer-live-008` 中一次调用生成并提交 37 条需求和 8 个设计章节，状态版本为 1，`finish_reason=stop`。完整英文提示词、原始回复、设计 JSON/Markdown 和运行账目均已保存。建议按“instruction → 实际 API 请求 → 模型回复 → 完整设计 → 角色结果与账目”的顺序检查。旧的模拟审阅材料仅保留在 `tests/fixtures/designer_mock_review/`。

验证结果：真实集成测试 1 项通过；离线回归 146 项通过、3 项跳过（默认关闭的真实调用测试、当前 Windows 用户无法创建符号链接、未配置 Docker 集成）。成功调用耗时约 92 秒，供应商报告输入 3,685 tokens、completion 21,909 tokens，其中 reasoning 14,749 tokens；账目与响应一致。

此前旧网关的 5 次失败仍保留。换到 DeepSeek 后，第 006 次在 8,192 上限内只生成了推理，第 007 次在 32,768 上限下生成了部分正文，两次均以 `length` 结束且未提交设计。显式设置 `reasoning_effort=low` 后，第 008 次完成。没有手工补全 JSON，也没有自动重试。完整尝试记录见审阅入口；旧网关的未知用量与 DeepSeek 实测用量分开记录。

此次通过的是实际模型到执行器提交的集成链路，不是设计语义或游戏可玩性的验收。[单独的审阅意见](examples/designer_review/reviewer_notes.md) 标出了四循环计数、Godot 启动参数的单位、30 秒玩法与 20 秒 demo 时长之间的三处问题，留待后续 Designer 修订与 QA 处理；原始模型输出和设计产物保持不变。

任务使用仓库内 GameCraft-Bench 快照的真实 `platformer-time-loop`（Time Loop）：30 秒循环、幽灵协作、四章共 24 个关卡，以及公开交付与 demo 轨迹协议。原始文件为 `benchmark_resources/FreedomIntelligence-gamecraft-bench-a433475/tasks/platformer-time-loop/instruction.md`，示例通过公共导入器直接读取该任务目录，不读取 `tests/`、`solution/` 或正式评分。

`examples/designer_case/source.json` 固定来源路径、快照和 instruction 的 SHA-256，运行前核验；文件变化或缺失会报错，不回退到自造任务。审阅目录的 `instruction.md` 与原文件逐字节一致，实际模型输入保留全部原文。`examples/designer_demo.py` 现在只提供真实模型路径，不读取预设回复，不手工修补模型 JSON，也不隐式重试失败请求。需求数量由实际模型响应决定。

安装可选的真实服务依赖并运行：

```sh
python -m pip install -e ".[test,live]"
python examples/designer_demo.py --output ./runs/designer-live
```

输出目录须不存在；默认显式读取项目根 `.env`，也可用 `--env-file` 选择文件。进程环境变量优先于文件，不修改全局环境。必要配置是 `DESIGNER_MODEL`、`MODEL_GATEWAY_API_KEY` 和 `MODEL_GATEWAY_BASE_URL`。不会自动选择或替换配置中的模型。当前保留用户设置的 DeepSeek API 和 `deepseek-flash`，本次增加 `DESIGNER_MAX_OUTPUT_TOKENS=32768`、`DESIGNER_REASONING_EFFORT=low`；已有的 600 秒超时保持不变。JSON 模式开启、流式模式关闭。所有实际参数保存在各次 `provider_config.json`，其他模型仍使用适配器默认值或调用端显式配置。

可选配置：

| 环境变量 | 默认值 | 含义 |
| --- | --- | --- |
| `DESIGNER_TIMEOUT_SECONDS` | `240` | 单次模型调用超时；执行器还会应用剩余预算 |
| `DESIGNER_MAX_OUTPUT_TOKENS` | `8192` | 同时约束角色请求、供应商参数及执行器输出预算 |
| `DESIGNER_TOKEN_PARAMETER` | `max_tokens` | 兼容网关的上限字段，也可显式设为 `max_completion_tokens` |
| `DESIGNER_JSON_MODE` | `true` | 发送 `response_format={"type":"json_object"}`；不支持时需显式改配置，不自动重试 |
| `DESIGNER_STREAM` | `false` | 显式启用流式响应，发送 `stream_options.include_usage=true`，保存每个返回片段 |
| `DESIGNER_REASONING_EFFORT` | 不发送 | 显式发送 `reasoning_effort`；例如 DeepSeek 的 `low`。可选值由适配器校验，但具体支持范围仍以供应商为准；不设置时沿用供应商默认行为 |
| `DESIGNER_INPUT_TOKEN_MARGIN` | `1024` | 完整 messages 的 UTF-8 字节数之外预留的 token 余量 |

输入 token 预留采用保守字节上界，包含真实 system/user 消息，不冒充模型分词结果；若网关额外注入提示词，可能需要增大余量。收到回复后，按 `prompt_tokens`、`completion_tokens` 和缓存 token 实际结算；缓存 token 不重复相加，`reasoning_tokens` 也是 completion 用量的子集，不另行重复计费。供应商未返回可用 usage 时保留未对账预留量，不编造零消耗。字段语义参考 [Chat Completions 官方接口](https://developers.openai.com/api/reference/resources/chat/subresources/completions/methods/create)，兼容网关是否支持某个可选参数以实际响应为准。

DeepSeek 的思考模式可能在输出设计 JSON 前消耗大量 token；[官方思考模式说明](https://api-docs.deepseek.com/guides/thinking_mode/) 提供了 `reasoning_effort` 配置。真实测试会记录 `finish_reason` 和供应商报告的 `reasoning_tokens`；`finish_reason=length` 不算完整测试通过。推理强度和输出上限都会进入公开服务配置和配置标识，两种调度范式应使用相同设置。改变这些配置后须创建新运行，不在旧运行中悄悄放宽预算。

运行目录保留 `workspace/`，其中 `.private/model_calls/001/` 保存请求、响应、时间和状态，不提供给角色或沙箱。SDK 使用 `max_retries=0`，异步取消传播至网络请求。连接建立最多等待 10 秒，完整调用仍受执行器期限约束。错误日志只保留分类、传输异常类型与 HTTP 状态码，不复制可能含凭据的错误正文。无使用量的连接失败不等同于确定的零费用。

审阅文件包括：

| 文件 | 内容 |
| --- | --- |
| `api_request.json` | 实际 API 请求体，包括 model、system/user messages、token 上限与 JSON 模式 |
| `model_prompt.json` | user 消息中的完整 Designer 协议、输入和输出 schema |
| `api_response.json` | SDK 返回的响应字段，包括模型标识、choices、finish_reason、usage 等；不保存请求头 |
| `response_chunks.jsonl` | 流式模式下保存的原始 SDK 片段，包含额外供应商字段；此时 `api_response.json` 是完成后的消息与用量聚合视图 |
| `model_response.txt` | 模型原文，不修补结构或内容；合法 JSON 时另保存同内容 `.json` |
| `provider_config.json` | 可公开的配置；不包含密钥和网关地址 |
| `role_result.json`、`state_after.json` | 校验后的角色返回值和执行器账目 |
| `summary.json` | 真实用量、验证结果与失败原因 |
| `design.json`、`design.md` | 只有设计成功提交后才存在的产物 |

完整响应捕获保持模型文本；若网关意外回显已知凭据或地址，会先脱敏。流式内容仅按原序拼接，不修复 JSON；缺少结束标记或有效 usage 时失败，已收到的片段仍保留。连接失败、超时或模型输出校验失败仍保留请求与运行状态，不用 mock 替代，也不伪造产物。复制到文档目录的审阅文件不含完整 workspace，原始运行目录可继续检查。

`tests/test_designer_live.py` 会调用真实模型。设置 `GAMECRAFT_LIVE_TESTS=1` 后执行 `python -m pytest -q -s tests/test_designer_live.py`；可设置 `GAMECRAFT_LIVE_OUTPUT` 指定新的持久化输出目录。不设置开关时默认测试套件明确跳过此项，避免无意产生服务费用。直接运行 `designer_demo.py` 始终调用真实模型，无需此开关。

离线单元测试仍使用测试替身验证权限、预算、错误响应和局部修订；它们不能替代真实集成测试。`tests/test_provider.py` 使用受控 HTTP 传输检查 SDK 参数、取消和计费映射，不声称这些响应来自真实服务。此示例未挂载素材库、截图工具或 Godot；这些限制以英文提供给角色，输出不能声称已经检索素材、实现或试玩游戏。

测试还覆盖：原始 instruction 的来源与校验值、示例输入输出无中文回归、导出产物摘要一致、两种控制器的相同输入、局部反馈修订、需求追加与编号保留、旧验证失效、重复提交、预算中断后的原子性、服务失败计费、上下文限制和越权输出拒绝。其他角色在这些测试中仅用最小受控函数准备状态，不代表已经实现它们的角色能力。
