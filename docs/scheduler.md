# Scheduler 开发与运行说明

正式入口与 Fixed Loop 共用 [素材规范化与切图](asset_normalization.md)：Art 发布明确布局和逐帧 PNG，Developer 检查原图集误用，未解决布局保留为公开待办。新配置请使用新的运行目录；原有模型和生图调用方式不变。

两条入口也共用[素材库优先、质量反馈迭代与 Designer 引用修正](library_first_and_grounding_repair.md)。搜索、引用修正和失败图片重做全部计入原有共享账本；没有为 Scheduler 单独放宽权限、质量门槛或总预算。

[人物精灵处理与质量检查](sprite_quality.md) 同样共享：保留原生像素、无损切图与独立显示尺寸、可靠紫红底去除、可疑源图隔离。rejected 和 unresolved 都保留为 Art 待办，Developer/QA 不将其视为已完成美术。

本文对应 `role_design` 第五步。`AdaptiveScheduler` 在已有五个角色、共享状态和执行器上，每次选择一个可执行角色及其目标事项。它是控制器，不是第六个工作角色，不新增游戏制作或验收权限。实现位于 `src/adaptive_game_generation/controllers/scheduler.py`，英文模型协议位于同目录的 `scheduler_policy.py`。

本阶段没有调用在线模型，也没有使用用户尚未补交的 Fixed Loop 运行结果。已有真实模型入口可供用户自行执行。离线测试中的模型回复和引擎观察明确为夹具，不能用于评价模型生成质量或比较两种范式的优劣。

## 与 Fixed Loop 共用的部分

Developer 默认使用同一套[阶段思考、按需上下文与局部修改](developer_optimization.md)。主调用上限 v3 为 8、v4 为 11（均包含读取决策），v4 另有最多 3 次视觉调用；全部计入相同的公共账目。Scheduler 不能覆盖 Developer 阶段参数或绕过其校验，首次实现和复杂问题保留原思考配置。

总运行期限同样由共享入口决定：默认 12 小时，可通过 `RUN_MAX_WALL_SECONDS` 或优先级更高的 `--max-wall-seconds` 设置。期限、素材透明处理、Developer 源素材视觉输入及未解决视觉错误的保留规则均与 Fixed Loop 相同。对比实验请显式冻结一致的预算；详见 [permissions-002 诊断](fixed_loop_permissions_002_diagnosis.md)。

两条入口共用 `examples/fixed_loop_demo.py` 的运行装配；`examples/scheduler_demo.py` 仅选择自适应控制器，不复制另一套角色配置。以下条件相同：

| 内容 | 共同实现 |
| --- | --- |
| 角色与模型 | 同一组 Designer、Art Producer、Developer、Player、QA 类与 `.env` 角色配置 |
| 状态与上下文 | 同一个 SharedState、角色输入构造、产物引用和状态提交检查 |
| 可执行条件 | `WorkPolicy`；初始化、上游变更、当前工程、问题分派和轨迹装配条件一致 |
| 角色请求 | `controllers/common.py` 的共同目标、完成条件、交接附件和上次失败反馈 |
| 工具 | 同一权限策略、素材库、DashScope 生图、可选 Godot MCP 与视觉辅助服务 |
| 试玩与验收 | Player 独立采集及确定性重放，QA 验收；MCP 预览不能代替这些证据 |
| 预算与交付 | 同一执行器计费、公共就绪谓词、最新提交版本选择和逐字节导出 |

Scheduler 另行读取 `SCHEDULER_*`，只为选择工作服务。此次没有修改 `.env`，因此调度模型的思考、流式、输出上限和超时以其中已有的 `SCHEDULER_ENABLE_THINKING`、`SCHEDULER_STREAM` 等值为准，不自动继承 QA 或 Developer。五个工作角色的大预算思考配置保持原样。

两种入口的 `experiment.json` 都保存 `comparison_conditions_sha256`。它覆盖原始任务、角色模型与配置、角色预算、总预算、引擎、素材库、图片服务、MCP、实现版本和角色调用上限，排除控制策略及专属调度模型。相同指纹帮助核对共同条件，不代表随机输出相同，也不能代替第六步的完整公平性审计。Fixed Loop 的轮数和 Scheduler 的决策上限分别保留，正式比较仍需冻结停止协议。

## 一次调度的输入与输出

宿主先应用共同的完成、预算和调用上限检查，再用 WorkPolicy 构建候选清单。无可执行工作时直接停止，不为得到“没有工作”的答案消耗模型调用。只有一个角色可执行时也记录一次模型决策，不把这部分推理隐藏为免费步骤。

Scheduler 输入包含原始英文 instruction、公共状态（需求、设计、资产、游戏版本、证据、问题和就绪判断）、全部角色的可执行条件及固定调用额度、总用量及剩余额度、最近 20 条角色结果/调度事件，以及明确的输出 schema。历史截取数量可见；公共状态本体不静默截断。完整提示词超过配置的字节上限会拒绝派发，并保存诊断。该适配器不向模型开放任意文件读取或工具选择；执行器本身仍保留其受限只读能力。

模型必须返回英文 JSON，例如首次初始化时：

```json
{
  "state_revision": 0,
  "selected_role": "designer",
  "work_item_ids": [],
  "objective": "Extract grounded requirements and an implementable design from the public instruction.",
  "completion_criteria": [
    "Preserve exact instruction excerpts for the requirements.",
    "Describe gameplay, controls, visual needs and observable acceptance criteria."
  ],
  "rationale": "A grounded design is required before art production and implementation can proceed."
}
```

这是协议示例，不是线上模型结果。字段校验包括：当前状态版本、角色确实可执行、事项属于该候选的待办集合且不重复、目标和完成条件非空。候选有待办时至少选择一项；初始化等没有具体待办编号的工作使用空列表。未知字段（包括模型、预算或 `ready`）一律拒绝。模型不能通过返回空角色或“完成”绕过宿主就绪检查。

通过校验后，选择结果经 `Executor.decide()` 写入账本。共同请求构造器保留角色固有目标和完成条件，再补充 Scheduler 的具体目标与条件；不会用调度模型的文字替换角色职责。目标、条件和理由持久化到 `SchedulerDecision`，因此恢复时不需要再次推理。

## 预算、失败与恢复

每次决策最多一次文本模型调用，失败和非法 JSON 同样计费。没有隐藏的免费纠错或回退到固定顺序。连续两次决策失败停止；中间有效决策会重置该计数。任一角色连续两次执行失败也停止，与 Fixed Loop 的规则一致。用量无法确认时保留预留成本并停止，不把未知用量记成零后继续运行。

默认允许 15 次工作角色执行和 30 次调度尝试。两条运行入口都支持 `--max-role-calls`，角色内部的多次模型调用不等于多次角色执行。`--max-model-calls` 是两者共用的总模型调用上限：包含工作角色、Developer 视觉辅助以及 Scheduler，自适应组不会额外获得免费模型额度。

为了保持两组一致，累计 token 预算暂时沿用已有的 `FIXED_LOOP_MAX_INPUT_TOKENS`、`FIXED_LOOP_MAX_OUTPUT_TOKENS` 和 `FIXED_LOOP_ROLE_MAX_INPUT_TOKENS` 名称，**它们同时作用于两种控制器**。单次调度额外读取：

| 设置 | 未配置时默认 | 作用 |
| --- | ---: | --- |
| `SCHEDULER_MAX_PROMPT_BYTES` | 1,048,576 | 本地完整提示词 UTF-8 字节上限 |
| `SCHEDULER_MAX_INPUT_TOKENS` | 2,097,152 | 单次调度输入预留/结算上限 |
| `SCHEDULER_MAX_OUTPUT_TOKENS` | 8,192（网关通用默认） | 单次输出上限；当前模板明确设置 4,096 |
| `SCHEDULER_TIMEOUT_SECONDS` | 240 | 提供商请求超时，仍受运行剩余期限约束 |

Scheduler 的额外决策预算不扩展总预算。输入仍采用完整请求字节数做保守预留、按实际 usage 结算。无进展角色不会无限重试：当前所有可执行角色都已连续返回 `no_op`/`blocked`，或连续五次角色执行没有适用变更时，停止为 `stalled`。角色明确失败适用失败规则，不伪装成无进展。

控制器持有独立锁，执行器继续串行提交。恢复先以账本对齐调度和角色结果：已付费的决策直接复用；已完成的角色不再次执行；已预留但中断的调用由 Executor 保留成本并补写失败。恢复必须使用相同运行配置。用户入口仍要求新输出目录，不自动恢复旧实验；恢复机制通过 `Executor.open(...)` 与控制器 API 提供并经离线测试验证。

## 运行

在项目根目录使用现有 Python 环境、依赖和 Docker 镜像。首次启用 MCP 的准备步骤沿用 [Godot MCP](godot_mcp.md)。无需为 Scheduler 构建新的镜像，也不需要修改各角色调用方式。

先进行本地预检，不调用在线模型或生图：

```sh
python examples/scheduler_demo.py --godot-mcp --preflight-only --output runs/scheduler-preflight-001 --max-role-calls 15 --max-decisions 30 --max-model-calls 64 --max-wall-seconds 43200
```

真实模型、真实 DashScope 图片和真实 Godot/MCP：

```sh
python examples/scheduler_demo.py --godot-mcp --output runs/scheduler-real-images-001 --max-role-calls 15 --max-decisions 30 --max-model-calls 64 --max-wall-seconds 43200
```

增加 `--mock-images` 可保留图片 mock；去掉 `--godot-mcp` 使用与 Fixed Loop 相同的 Developer v3。进行对比时两组必须使用相同开关。运行默认使用 Benchmark 快照的 `platformer-time-loop/instruction.md` 原文；可通过 `--task-dir` 指定另一份真实 Benchmark 任务。

相同共同预算的 Fixed Loop 示例：

```sh
python examples/fixed_loop_demo.py --godot-mcp --output runs/fixed-loop-comparison-001 --max-cycles 3 --max-role-calls 15 --max-model-calls 64 --max-wall-seconds 43200
```

这些仍是开发命令。三轮与最多十五次自适应调用具有不同的搜索轨迹和停止机会，不能仅凭这一个任务宣称 Scheduler 更好。

## 完整审阅目录

| 路径 | 内容 |
| --- | --- |
| `experiment.json` | 共同条件指纹、角色配置、调度模型、固定预算和实现快照信息 |
| `model_calls/scheduler/NNN/` | 真实提供商请求、响应、流式块和 usage（在线运行时） |
| `model_call_index/schedule-NNN.json` | 调度决策到提供商调用的对应关系 |
| `workspace/.private/controller/decisions/schedule-NNN/` | 完整调度输入、候选、原始输出、校验后的选择、账本结果和前后状态 |
| `workspace/.private/controller/calls/schedule-NNN-ROLE/` | 实际角色请求、结果和完整前后状态 |
| `workspace/.private/controller/checkpoint.json` | 选择顺序、理由、待办、执行状态和失败 |
| `state_final.json` | 全部共享状态和唯一权威账本 |
| `summary.json` | 停止原因、就绪状态、总消耗，以及独立列出的 Scheduler/worker 消耗 |
| `delivery/game/` | 最后一个已提交游戏版本的逐字节副本 |
| `review.md` | 输入输出与产物导航 |

尚未真正发出的调用不会伪造提供商请求或响应文件。最终导出仍不额外修改代码、装配轨迹或执行游戏。完整目录应保留，便于后续读取结果分析。

## 离线验证

`tests/test_scheduler.py` 使用真实 `platformer-time-loop/instruction.md`，同时明确标记脚本模型、模拟引擎和 mock 图片。覆盖可变角色顺序、轨迹装配后重放、mock 美术阻止交付、错误选择/状态/事项/预算注入拒绝、共享计费、无进展停止、取消和恢复。另有通过真实 Chat 适配器但使用离线 HTTP 响应的服务路由测试，以及两条入口的共同配置指纹测试。

本次审阅材料位于 `runs/scheduler-development-001/`。其中 `offline-review/` 保存调度和全部角色的夹具模型输入输出、状态、工具与决策日志及工程副本；本地预检位于 `preflight-scheduler/` 和 `preflight-fixed/`。线上兼容性和游戏质量留给用户真实运行验证。第六步的系统公平性审计和第七步的多任务正式实验不包含在本次第五步实现内。

遇到 Developer 导入权限错误时，参考 [两种控制器的共同故障诊断](developer_permissions_diagnosis.md)。公共执行器现在会将无法写入输出目录识别为宿主故障并终止运行，Scheduler 不会继续选择 Developer 去修改游戏代码解决权限。模型返回的 `partial` 与当前版本构建是否通过是不同维度，不能直接以 `partial` 次数比较两种范式的游戏质量。
