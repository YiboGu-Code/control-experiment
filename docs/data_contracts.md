# 共享数据结构开发说明

本文对应 `role_design.md` 开发顺序中的第 1 步：共享状态、产物引用、角色输入输出与运行日志。实现位于 `src/adaptive_game_generation/models/`。

## 1. 实现范围

本次实现的是可校验、可序列化的数据契约，以及只操作内存值的辅助方法。它不绑定具体模型、Godot 安装位置、操作系统、数据库或智能体框架。

- 已实现：类型定义、字段与关联关系校验、JSON 往返转换、JSON Schema 导出、日志消耗汇总、调用对应关系校验。
- 第 2 步已提供配套执行层：工具执行器、权限与文件隔离、预算拦截、状态提交与并发控制、持久化存储，见 [执行框架开发说明](runtime.md)。数据模型本身仍不承担这些执行职责。
- 第 3 步已实现 [Designer 的英文提示词与工作流程](designer.md)；后续已完成其余角色、真实试玩、[固定 Loop](fixed_loop.md) 和 [Scheduler](scheduler.md)。

数据模型不会读取 `.env`，不会导入现有 `LLM` 模块，不会发起网络请求或自动启动游戏。

## 2. 包与依赖

项目使用标准 `src` 包布局，包名为 `adaptive_game_generation`。支持 Python 3.10 及以上；数据结构依赖 Pydantic 2，测试使用 pytest。依赖范围声明在根目录 `pyproject.toml` 中，不要求采用特定环境管理工具。

在自行选择的 Python 环境中安装：

```sh
python -m pip install -e ".[test]"
```

不需要测试依赖时使用 `python -m pip install -e .`。这里安装的是本次共享数据结构包；现有 `LLM/` 模块的模型 SDK 不属于此步骤的依赖。

所有公共模型从同一入口导入：

```python
from adaptive_game_generation.models import (
    SharedState, TaskSpec, RunLedger, ResourceLimits,
    RoleRequest, RoleResult, ArtifactRef,
)
```

## 3. 共享状态

`SharedState` 是一次运行的完整数据快照：

| 字段 | 类型 | 含义 |
| --- | --- | --- |
| `schema_version` | 固定字符串 `1.0` | 数据结构版本 |
| `task` | `TaskSpec` | 原始任务、资源引用、环境约束、Benchmark 快照标识 |
| `ledger` | `RunLedger` | 本次运行身份、控制方式、预算配置与事件记录 |
| `state_revision` | 非负整数 | 共享状态版本，与日志中最后一次提交一致 |
| `artifacts` | `tuple[ArtifactRef, ...]` | 已登记的产物引用，包括历史版本 |
| `requirements` | `tuple[Requirement, ...]` | 需求、原文依据、验收条件、依赖与验证结果 |
| `design` | `DesignSpec` 或空 | 设计文档引用、版本、假设、变更说明 |
| `assets` | `tuple[AssetRecord, ...]` | 当前素材清单、来源、许可、接入说明 |
| `game_revisions` | `tuple[GameRevision, ...]` | 按父版本先于子版本排列的游戏版本历史 |
| `current_game_revision_id` | 标识或空 | 当前正在工作的游戏版本 |
| `evidence` | `tuple[EvidenceRecord, ...]` | 与具体游戏版本绑定的试玩、重放、构建或观察证据 |
| `issues` | `tuple[IssueRecord, ...]` | 问题、严重程度、建议处理角色、证据与处理状态 |
| `readiness` | `ReadinessReview` 或空 | QA 对当前游戏版本的内部交付建议 |

最小初始化示例：

```python
from pathlib import Path

task_path = Path("benchmark_resources/FreedomIntelligence-gamecraft-bench-a433475/tasks/platformer-time-loop")
state = SharedState(
    task=TaskSpec(task_id=task_path.name, instruction=(task_path / "instruction.md").read_text(encoding="utf-8")),
    ledger=RunLedger(
        run_id="run-001",
        task_id=task_path.name,
        controller="fixed_loop",
        budget=ResourceLimits(max_model_calls=30, max_output_tokens=50000),
    ),
)
```

初始状态允许没有需求、素材、游戏和证据。首次 Designer 请求也允许 `work_item_ids=()`，用于生成初始需求。

### 不可变值与校验

模型默认冻结，集合使用 tuple；JSON 中相应表示为数组。这样原始 instruction、证据和历史记录不会因为共享可变列表而被意外改动。

使用 `revised(...)` 创建经校验的新值：

```python
updated_requirement = requirement.revised(status="implemented_unverified", verification=None)
```

不要用 Pydantic 的 `model_construct()` 或 `model_copy(update=...)` 接收外部输入，它们会绕过全部或部分校验。外部 JSON 必须通过 `model_validate_json()`，字典通过 `model_validate()` 进入。

`revised()` 仅表示值的重新构造，不代表已授权提交、自动递增状态版本或自动保存历史。更新同一次运行的原始任务、应用角色权限、把提交与日志原子保存等规则，仍需由下一步执行器和存储层落实。

## 4. 需求与证据

`Requirement.source_excerpt` 必须是原始 instruction 的非空原文片段。新增玩法假设应放入 `DesignSpec.assumptions`，不能伪装成原始要求。需求依赖必须引用存在的需求，且不得成环。

验证状态包括：

- `not_implemented`：尚未实现。
- `implemented_unverified`：已实现，尚未验证。
- `verified_pass`：有当前游戏版本的证据支持通过结论。
- `verified_fail`：有当前游戏版本的证据支持失败结论。

两种 verified 状态必须包含 `Verification`，其中记录游戏版本、证据编号、理由与审查时间。其他状态必须清空 `verification`。

证据记录中的 `requirement_ids` 表示覆盖的需求。需求验收所引用的每条证据必须覆盖该需求，并属于当前游戏版本。证据 `outcome=not_run` 表示没有完成独立的二值检查，例如仅记录观察；它本身不是通过结论。

当游戏版本变化时，旧证据可以继续保留，但不能直接支撑新版本的验收。第一版采用保守规则：切换版本后，已有 verified 需求需要回到待验证状态并清空当前结论，`readiness` 也需要清空或刷新。模型会拒绝过期结论，不会自动替执行器完成这些更新。先前的验收结论可保留在历史状态快照与角色结果日志中。

构建检查结果必须有同一游戏版本、同一检查结果的 `build_check` 证据；重放证据必须包含输入轨迹引用。模型只验证记录的一致性，无法凭元数据证明文件存在或游戏真的通过检查。

`ReadinessReview` 是 QA 建议，不等同于完整交付门槛或正式分数。启动成功、最终轨迹可重放、预算等停止规则由后续控制器与执行器统一落实。

## 5. 产物引用

`ArtifactRef` 包含：

- `artifact_id` 与 `version`：共同构成唯一引用键。
- `kind`：文档、素材、游戏项目、输入轨迹、截图、视频、日志、报告、配置或资源库。
- `path`：以运行工作区为逻辑根的规范化相对路径，统一使用 `/`。
- `created_by`：工作角色或 `system`；它是来源元数据，不是授权凭证。
- `media_type`：可选媒体类型。
- `sha256`：可选内容摘要；摘要计算和文件校验由执行层负责。

引用示例：

```python
ref = ArtifactRef(
    artifact_id="jump-trace",
    version=1,
    kind="input_trace",
    path="game/v1/demo_outputs/jump.json",
    created_by="player",
    media_type="application/json",
)
```

绝对路径、盘符、反斜杠、`..`、空路径片段等会被拒绝。每个产物版本应使用独立路径，例如 `game/v1` 与 `game/v2`，避免覆盖历史证据。需要共用某个文件时复用同一引用，而非创建指向相同路径的另一条登记。

状态中被设计、素材、游戏或证据使用的引用，必须与 `artifacts` 中对应登记完全一致。失败或尚未提交的角色结果可以暂存新产物引用；执行器接受产物后再将它们登记进共享状态。

这些是逻辑路径规则，不实现文件访问权限、符号链接隔离、路径实际解析或内容完整性检查。外部提供的只读资源如何映射到逻辑工作区，由下一步执行器决定。

## 6. 角色请求与结果

`RoleRequest` 除设计文档中的工作字段外，还包含 `request_id`、`run_id` 和 `task_id`，用于关联日志与运行。`state_revision` 表示这次调用所依据的共享状态版本；角色枚举中不包含 Scheduler。

`RoleResult` 复述 `request_id`、`role` 和 `state_revision`，并提供状态、摘要、产物、证据引用、阻塞原因和结构化错误。`evidence_refs` 存储证据记录的 ID，不是文件路径。

```python
state.validate_request(request)
result.validate_for(request)
```

前者检查请求是否属于当前运行、任务与状态，及所引用事项和产物是否存在；后者检查返回结果是否对应原请求。权限和预算检查不包含在这两个方法中。

`ProposedUpdates` 使用有类型的字段，支持需求、设计、素材、游戏版本、证据、问题和就绪建议。集合字段表示按 ID 新增或替换的建议；空集合、空对象字段表示没有对应更新。执行器接受游戏新版本时需要另行清除过期验证结果。更新建议不提供任意 JSON patch，也不提供改写原始任务、预算或运行日志的入口。

角色结果状态的区别：

| 状态 | 含义与约束 |
| --- | --- |
| `completed` | 本次角色目标已完成，不代表游戏需求已通过验收 |
| `partial` | 仅完成部分工作，可附带错误或阻塞事项 |
| `blocked` | 必须说明阻塞原因；不代表整个运行终止 |
| `no_op` | 没有新增产物或状态更新 |
| `failed` | 必须包含结构化错误；已有部分产物如何处理由执行器决定 |

角色返回的更新建议不会自动应用，也不会自动获得修改相应状态的权限。

## 7. 运行日志与资源口径

`RunLedger` 包含运行身份、控制器类型、可选配置快照引用、总预算、开始时间和事件序列。每个 `LedgerEvent` 包含事件 ID、从 1 开始的连续序号、带时区的时间及有类型的内容。

支持以下事件：

| 事件 | 用途 |
| --- | --- |
| `role_call_started` | 记录完整请求，建立调用身份 |
| `role_call_finished` | 记录完整结果与执行器采集的实际消耗 |
| `scheduler_decision` | 记录 Scheduler 的决策、理由、状态版本及实际消耗，也支持决策失败 |
| `state_committed` | 记录共享状态版本递增，可关联产生该提交的请求 |
| `run_ended` | 记录终止原因、最终选择的游戏版本及未解决问题 |

典型顺序：

```text
角色调用开始 → 角色调用结束 → 接受更新并提交状态 → 下一次调用
```

第一版按串行运行校验：同一时刻最多有一个进行中的角色调用。结果必须对应尚未结束的请求，每次结果和提交不可重复；时间不能倒退；终止后不能再追加事件。日志从状态版本 0 开始，`state_committed` 每次只能递增 1。框架导入检查点可使用不关联请求的提交事件。

```python
new_ledger = ledger.append(event)  # 产生新的已校验日志值，不写入磁盘
usage = new_ledger.usage
```

资源记录包含模型调用数、输入 token、缓存输入 token、输出 token、图像调用数、工具调用数、工具秒数和调用耗时：

- `input_tokens` 已包含缓存部分，`cached_input_tokens` 是其中的子集，不能再次相加。
- `total_tokens = input_tokens + output_tokens`。
- Scheduler 消耗与角色消耗都进入汇总；失败调用同样记录。
- 缺少完整 token 用量时设 `token_usage_complete=False`。仅有部分报告时数值可以是已知小计；执行层遇到无法计量的失败或中断时，会保守保留调用前预留值。这些数值不能当作完整实测消耗，有限 token 预算下执行层会阻止继续执行，直至完成对账。
- 每次角色或 Scheduler 调用的 usage 包含其内部工具消耗，只汇总一次，避免重复计数。
- 汇总的 `wall_seconds` 是已结束调用的耗时之和，不包含调用间空闲时间；整个运行的墙钟超时需要执行器另外依据运行开始时间控制。
- `ResourceLimits` 中 `None` 表示该维度未指定上限，`0` 表示零额度。

日志记录允许实际消耗超出预算，以保留真实情况；是否允许继续执行由下一步预算约束实现决定。正在进行的调用尚未进入完成事件的小计，也不应被解释为没有消耗。正常终止前应把中断调用记录为 partial/failed 并附带已知消耗；进程硬崩溃恢复与未完成调用对账属于后续执行与存储层。

## 8. JSON、Schema 与示例

保存和恢复：

```python
from pathlib import Path

path = Path("state.json")
path.write_text(state.model_dump_json(indent=2), encoding="utf-8")
restored = SharedState.model_validate_json(path.read_text(encoding="utf-8"))
```

结构版本目前为 `1.0`；未知版本、未知字段会被拒绝。未来更改字段语义或不兼容结构时，应显式升级并提供迁移，不静默丢弃字段。

安装包后可执行：

```sh
python examples/contracts_demo.py --output ./example_output
python -m adaptive_game_generation.schema --output ./schema_output
python -m pytest -q
```

示例读取快照内真实 `platformer-time-loop/instruction.md`，选取其中跳跃能力说明一种数据契约用法，并生成英文共享状态、QA 请求和 QA 结果三个 JSON 文件。它不是完整任务设计；所有游戏、素材和证据引用都是演示数据，示例不会创建实际游戏或验证这些文件。

Schema 导出包含 ArtifactRef、SharedState、RoleRequest、RoleResult、LedgerEvent 和 RunLedger。JSON Schema 适合接口文档和字段级校验，但无法表达全部跨记录规则；实际接收数据时仍应通过 Python 模型校验。

测试覆盖完整快照与 Unicode 往返、冻结与版本规则、引用一致性、证据失效、需求依赖环、请求结果对应、终止记录和消耗汇总。测试不调用外部模型或游戏引擎。

Pydantic 接口参考：[模型与校验](https://docs.pydantic.dev/latest/concepts/models/)、[序列化](https://docs.pydantic.dev/latest/concepts/serialization/)。
