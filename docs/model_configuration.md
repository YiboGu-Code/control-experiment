# 模型配置与切换记录

实际启用的模型以运行时 `.env` 和进程环境变量为准。2026-10-21 在 003 诊断配置上继续扩大输出、输入长度限制和预算；下面保留的 DeepSeek 恢复记录属于历史，不作为当前开关状态。本次没有在线模型调用。

## 当前大预算思考配置

| 角色 | 模型 | 单次输出上限（tokens） | 本地提示词上限（UTF-8） | 思考 / 流式 | 实际截图输入 |
| --- | --- | ---: | ---: | --- | --- |
| Designer | `qwen3.8-max` | 65,536 | 1 MiB | 开 / 开 | 关 |
| Art Planner | `qwen3.8-flash` | 65,536 | 1 MiB | 开 / 开 | 关 |
| Developer | `deepseek-v4-pro-0813` | 131,072 | 4 MiB | 开 / 开，`reasoning_effort=high` | 关 |
| Player | `qwen3.8-flash` | 32,768 | 1.5 MiB | 开 / 开 | 开 |
| QA | `qwen3.8-max` | 65,536 | 2 MiB | 开 / 开 | 开 |

各文本请求超时为 1,800 秒，仍受角色与整次运行期限约束。保留模型、凭据、地址、生图设置和注释掉的历史配置。此次修改前五个角色已经全部设置 `ENABLE_THINKING=true`、`STREAM=true`；此次将各角色的输出上限和本地提示词上限扩大为原来的两倍，不是首次开启思考。003 的 20 次回复全为 `finish_reason=stop`，最大单次输出 13,645 token；提高上限主要为思考和更完整的审查留空间，不能保证游戏质量提高。

**上下文窗口、输出额度和本地输入限制是三个不同的概念。** 模型本身的窗口由提供商决定，不能用 `max_tokens` 扩大。当前 [Qwen3.8 Max](https://help.aliyun.com/zh/model-studio/qwen3-8-max)、[Qwen3.8 Flash](https://help.aliyun.com/zh/model-studio/qwen3-8-flash) 和 [DeepSeek V4 Pro](https://help.aliyun.com/zh/model-studio/deepseek-v4-pro) 的官方说明支持百万级上下文；输入、输出和思考的组合仍须满足具体服务限制。本项目不发送虚构的 `context_window` 参数，也不为填满窗口而重复内容。`<ROLE>_MAX_PROMPT_BYTES` 只放宽角色原先的本地长度检查；单位是字节，不是 tokens，图片另外计入输入预留。超过本地上限会明确报错，不静默裁剪。`MAX_OUTPUT_TOKENS` 经现有 `max_tokens` 参数发送到网关。

Fixed Loop 新增以下宿主配置，均写入 `experiment.json`，供后续复查。未配置时保留原默认值；进程变量覆盖 `.env`。本地提示词设置用于 Fixed Loop，`CODE_DEVELOPER_MAX_PROMPT_BYTES` 也用于 Developer MCP 单角色在线示例，其余旧单角色入口仍沿用各自的本地输入限制。

| 配置 | 当前 `.env` | 未配置时默认 | 含义 |
| --- | ---: | ---: | --- |
| `FIXED_LOOP_ROLE_MAX_INPUT_TOKENS` | 40,000,000 | 6,000,000 | 每次角色执行的累计输入预算 |
| `FIXED_LOOP_MAX_INPUT_TOKENS` | 128,000,000 | 15,000,000 | 整次循环的累计输入预算 |
| `FIXED_LOOP_MAX_OUTPUT_TOKENS` | 8,000,000 | 1,000,000 | 整次循环的累计输出预算 |

这些是执行器的硬上限，不是预付用量或模型上下文长度。输入调度仍先用完整请求字节数及图片额度做保守预留，再用提供商 usage 结算；角色输出预算仍按允许的调用次数和单次额度计算。MCP 工具权限、隐藏资料隔离和确定性重放约束没有变化。

使用新目录运行完整实验（镜像已按 Godot MCP 文档准备）：

```sh
python examples/fixed_loop_demo.py --godot-mcp --output runs/fixed-loop-mcp-thinking-001 --max-cycles 3 --max-model-calls 64 --max-wall-seconds 43200
```

此命令使用真实文本模型与真实 DashScope 图片；增加 `--mock-images` 则保持图片 mock。`--max-wall-seconds 43200` 是整次运行的 12 小时上限，不代表必然运行这么久。不传该参数仍使用旧默认 10,800 秒。预检可增加 `--preflight-only` 并使用另外一个新目录，不会调用在线模型。

完整请求、响应、提供商返回的思考内容和 token 用量继续保存在 `model_calls/`，最终游戏在 `delivery/game/`，总览在 `summary.json` 和 `review.md`。本次修改前的脱敏配置与离线验证记录保存在 `runs/thinking-context-config-001/`。与上次比较时应称为“大预算思考配置”的效果比较；若要单独检验思考的作用，应在相同代码、模型、MCP、输入和预算条件下，仅切换五个角色的 `ENABLE_THINKING`（Developer 视觉工具继承 QA），再分别保存运行目录。

`PLAYER_VISION=true`、`QA_VISION=true` 让 Fixed Loop 各取至多 8 张当前版本的真实采集截图，通过原有 Chat 接口的 `image_url` 内容块发送 PNG/JPEG/WebP 字节。图片按场景轮流选取、去重、校验哈希，只能读取公共工作区，计入 token 预留及实际账目。无图时保持原来的文本请求形状；关闭开关会恢复文本审查，不能再批准视觉验收。Developer 通过 Player/QA 的问题和具体描述获得反馈。

阿里云文档说明了 [思考开关与流式输出](https://www.alibabacloud.com/help/en/model-studio/deep-thinking)、[DeepSeek 思考强度](https://www.alibabacloud.com/help/en/model-studio/deepseek-api) 和 [Token Plan 视觉输入](https://help.aliyun.com/en/model-studio/add-vision-skill)。当前网关的在线兼容性和实际效果仍由下一次用户运行验证。流式原始块保存在 `response_chunks.jsonl`；汇总的 `response.json` 保留 `reasoning_content` 与 usage，角色仅解析最终 `content` 的 JSON。

`examples/fixed_loop_demo.py` 默认读取 `ART_IMAGE_MODEL`，通过 **DashScope SDK 原生图片接口** 调用，其他角色仍使用原来的 Chat Completions 适配器。默认 `ART_IMAGE_PROVIDER=dashscope`；可通过 `ART_IMAGE_BASE_URL` / `ART_IMAGE_API_KEY` 独立配置图片服务，也支持 `DASHSCOPE_BASE_HTTP_API_URL` / `DASHSCOPE_API_KEY`。未设置图片地址时，会将阿里云共享地址的 `/compatible-mode/v1` 推导为同地域的 `/api/v1`，不改写共享地址。可选参数、接口格式和运行命令见 [Fixed Loop 开发与运行说明](fixed_loop.md#生图配置)。只有显式 `--mock-images` 才恢复占位图；既有单角色示例仍保留各自的 mock 配置。

## 可选 Developer MCP 视觉工具

`--godot-mcp` 不修改上表的主模型。Developer 的看图工具默认复用 `QA_*` 配置，发送实际 MCP 预览 PNG；可用 `DEVELOPER_VISUAL_MODEL` 覆盖该工具的模型名，`DEVELOPER_VISUAL_MAX_OUTPUT_TOKENS` 设置输出上限（当前 `.env` 为 32,768，未配置时为 8,192）。地址、凭据、超时、思考和流式设置仍来自 QA 网关配置，QA 本身不变。该工具至多调用 3 次，与 Developer 至多 8 次主调用共用角色和总预算。真实图片继续走 DashScope 原生接口。详见 [Godot MCP](godot_mcp.md)。

此前 MCP 接入开发没有更改 `.env`，没有发起在线模型调用；已验证配置预检、离线边界测试和真实本地 MCP/Godot 修复链路。后者的模型回复明确为脚本夹具，不能作为线上质量结果。本次按用户要求另行修改了上表对应的 `.env` 设置。

## DeepSeek 恢复时的配置（历史记录）

| 角色或服务 | 模型 | 状态 |
| --- | --- | --- |
| Designer | `deepseek-flash` | 已有角色，真实文本模型 |
| Art Producer 规划 | `deepseek-flash` | 已有角色，真实文本模型 |
| Developer | `deepseek-flash` | 已有角色，真实文本模型 |
| Player | `deepseek-flash` | 已有角色，真实文本模型 |
| QA Reviewer | `deepseek-flash` | 已有角色，真实文本模型 |
| SVG 模型预留 | `deepseek-flash` | 仅配置预留 |
| Scheduler 模型预留 | `deepseek-flash` | 仅配置预留 |
| 当时的图片生成模型预留 | `qwen-image-3.0-pro` | 当时示例注入 `MockImageProvider`，历史记录没有真实生图 |

网关凭据和地址从 `.env` 中原有的 DeepSeek 历史条目恢复，不写入本文或公开产物。Designer、Art Planner 恢复原来的 `REASONING_EFFORT=low`；Developer、Player、QA 恢复原来的 `THINKING=disabled`。原有角色输出上限与超时也已恢复。

恢复配置时的角色集成测试独立导入已有上游产物作为测试前提。后续已实现第 4 步 [固定 Loop](fixed_loop.md)：该入口从原始 instruction 连续运行五个角色，最初使用 DeepSeek 和 mock 生图，现已支持真实生图。第 5 步 [Scheduler](scheduler.md) 已实现，独立读取 `SCHEDULER_*`，其推理消耗计入同一总预算。

## 阿里云模型分配方案

| 角色或服务 | 预留模型 | 分配依据 |
| --- | --- | --- |
| Designer | `qwen3.8-max` | 需求分解与整体设计优先考虑能力 |
| Art Producer 规划 | `qwen3.8-flash` | 素材清单与来源规划兼顾调用成本 |
| Developer | `deepseek-v4-pro-0813` | 编程任务采用固定版本，方便记录实验条件 |
| Player | `qwen3.8-flash` | 反复生成短操作计划，兼顾延迟与成本 |
| QA Reviewer | `qwen3.8-max` | 长证据审查与需求逐项验收优先考虑能力 |
| SVG 模型预留 | `qwen3.8-flash` | 与素材规划保持一致 |
| Scheduler 控制器 | `qwen3.7-plus` | 由 `SCHEDULER_*` 独立配置，仅自适应入口启用，计入共同预算 |
| 图片生成 | `qwen-image-3.0-pro`（原方案） | Fixed Loop 已支持 DashScope 原生接口；当前用户配置为 `wan2.7-image`，实际值以 `.env` 为准 |

这是面向职责的起始配置，不是经过 GameCraft-Bench 对比得出的最优组合。最初采用非思考 JSON 模式，当前已按上表调整。两组正式实验必须固定相同的角色模型、提示词、视觉能力、参数和预算，不自动切换模型。

无凭据配置模板见 [`configs/models.token-plan.env.example`](../configs/models.token-plan.env.example)。凭据与地址由用户在 `.env` 中维护，不写入文档或产物。

选型与接口参考：[阿里云文本生成模型说明](https://help.aliyun.com/zh/model-studio/text-generation-model)、[DeepSeek 调用说明](https://help.aliyun.com/zh/model-studio/deepseek-api)、[思考模式参数](https://help.aliyun.com/zh/model-studio/deep-thinking)。这些文档说明模型定位及接口；本项目效果仍需实测。

## 参数兼容

公共适配器现在支持两个互斥的思考开关：

- 原 DeepSeek 接口：`<ROLE>_THINKING=enabled|disabled`，请求中为 `thinking: {"type": ...}`。
- 阿里云接口：`<ROLE>_ENABLE_THINKING=true|false`，请求中为布尔值 `enable_thinking`。

未配置时不发送对应字段，不改变旧配置标识。同一角色同时设置两个开关会在发请求前报配置错误。进程环境变量依然覆盖 `.env`，切换时应同时检查旧进程变量。

切换文本服务时需要一起核对共享网关凭据、地址和全部角色参数，不能只改模型名。图片服务默认走 DashScope 原生接口；切换到非阿里云的文本网关时，应单独配置图片地址和密钥。只有显式设置 `ART_IMAGE_PROVIDER=openai` 时才使用此前的 `/images/generations` 图片协议，该开关不影响任何文本角色。

## 可复现的短请求检查

`examples/model_config_smoke.py` 使用仓库内真实 `platformer-time-loop/instruction.md`，校验原文件 SHA-256。每个配置发一次英文 JSON 请求，保留请求、原始响应、实际 token 用量和检查结果。默认每次输出上限 1,024 tokens、最长 90 秒，不自动重试，不调用图片或控制器。

```powershell
$env:PYTHONPATH = 'src'
python examples/model_config_smoke.py --live --roles DESIGNER ART_PLANNER CODE_DEVELOPER PLAYER QA --output runs/model-check-new
```

输出目录必须是新目录，避免覆盖失败记录。这只验证短请求的接口和输出结构，不能替代完整角色集成测试，也没有验证模型在配置的最大输出额度下的行为。

阿里云的已尝试记录在 `runs/model-config-token-plan-001/`：7 次请求均在连接阶段超时，没有模型响应，不能据此判断模型质量。按用户要求不再尝试访问该端点。

恢复 DeepSeek 后的短请求记录在 `runs/model-config-deepseek-restored-001/`：5 次均收到模型响应，4 次通过检查；Developer 返回的 `source_excerpt` 不是原 instruction 的连续原文，因此被校验拒绝。该失败原样保留，没有用 mock 回复替换。

五个角色的完整真实调用与回归验证记录见 [`deepseek_restore_review.md`](deepseek_restore_review.md)。
