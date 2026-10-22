# 公共执行器、权限、预算与环境隔离

本文对应 `role_design.md` 的第 2 步。实现位于 `src/adaptive_game_generation/runtime/`，基于已有数据契约，为固定 Loop 和 Scheduler 提供同一执行入口。

第 4 步扩展：`RuntimeConfig.role_limits_by_role` 与 `Executor(..., services_by_role=...)` 支持宿主按角色注入不同模型和固定预算；未配置的角色沿用旧的默认服务/预算。请求仍必须匹配宿主分配的限制，Scheduler 的资源边界单独保留。控制器和 `finish()` 复用 `completion_ready()`；账本增加 `iteration_limit` 和 `stalled` 两种明确的停止原因。完整运行及配置冻结见 [Fixed Loop](fixed_loop.md)。

可选 Godot MCP 服务已接入同一执行器：`Services.godot_mcp` 提供隔离检查，`Services.vision` 提供 Developer 看图工具。`RoleTools.inspect_godot` 校验角色、工程路径/哈希、回执、截图与输出限制，并为每次检查预留 7 次 MCP 工具调用；`generate_text(use_vision=True)` 共用模型预算。Developer/Player 可用检查权限，QA 只能读诊断。部署与调用边界见 [Godot MCP](godot_mcp.md)。

## 1. 本次实现

- `Executor.execute()`：校验请求、调用角色适配器、关闭本次工具能力、校验产物与更新建议、提交状态、记录实际或保守预留的消耗。
- `Executor.decide()`：使用相同预算与工具边界包装 Scheduler 的真实模型决策，并记录其调用成本；不包含调度策略本身。
- `RoleTools`：受控文件读写、目录检索、复制、内容摘要和 Developer 暂存文件删除、文本模型、图像生成、隔离进程执行，以及后续 Art Producer 接入的只读素材库检索与导入。
- `RolePolicy`：角色可用工具、可写产物类型、状态字段及验收权限。
- `BudgetMeter`：总预算与单次预算、调用前预留、调用后结算、截止时间检查。
- `Workspace`：公开输入导入、角色暂存区、通过校验后的产物发布。
- `StateStore`：单写者锁、原子状态文件、历史快照、工具审计和中断恢复账目。
- `DockerSandbox`：可选的 Linux 容器执行后端；没有配置后端时，拒绝执行生成代码。

本步骤不实现五个角色的智能行为、真实游戏试玩策略或两种控制器的调度算法。它们通过适配器接入，不需要重新实现权限或预算逻辑。

## 2. 信任边界

角色适配器、模型服务适配器、沙箱后端和宿主配置属于可信框架代码。角色提示词、模型返回的工具参数、代码和产物属于需要约束的输入。

角色适配器是异步 Python 函数，只应通过 `context.tools` 暴露能力给模型。**不要把模型生成的 Python 当作适配器在宿主进程中执行，也不要绕过工具接口直接读宿主文件或请求模型服务。** 该 API 不声称能够隔离一个恶意的宿主 Python 插件。

通用文件工具只访问公开工作区与本次调用暂存区。Art Producer 的素材库工具另通过宿主注入的 `AssetLibraryProvider` 读取明确配置的只读素材目录，模型不能设置宿主路径；库文件导入后仍在独立暂存区接受校验与发布。生成游戏代码必须由隔离后端执行；内置后端不提供直接在宿主执行 shell、Python 或 Godot 的回退路径。运行目录应由可信框架独占，不与不受信任的宿主写入进程共享。

## 3. 生成工作区

```text
run_directory/
  public/                         # 角色可读；容器只读挂载
    input/
      instruction.md
      runtime_config.json
      starter/                    # 仅来自 task/workspace，可选
      resources/<name>/           # 显式声明的公开资源，可选
    artifacts/<role>/<call-key>/   # 通过校验后发布的产物
  .staging/<call-key>/             # 每次调用独立的输出暂存区
  .private/                       # 不提供给角色工具或容器
    state.json
    history/<event-count>.json
    inflight.json
    tools.jsonl
    writer.lock
    quarantine/                   # 中断时发现的未提交发布内容
```

`Workspace.prepare()` 仅导入指定任务的 `instruction.md`、可选 `workspace/` 和显式声明的 `PublicResource`。它不会复制整个 Benchmark、任务根目录、`tests/`、`solution/` 或评测脚本。

导入目录内出现 `.env*`、`.git`、`tests`、`solution`、`rubric.json` 等私有路径时拒绝导入。该检查是补充措施，公开输入白名单才是边界；宿主必须确保显式传入的资源本身确实是公开内容，文件名检查不能识别任意改名的秘密内容。

生成工作区与任务来源目录不得互相包含。实际读取会拒绝路径穿越、绝对路径、盘符、反斜杠、符号链接、Windows 重解析点、硬链接和特殊文件。产物文件名与引用也检查大小写冲突，避免在不同文件系统中指向同一文件。

## 4. 权限矩阵

所有工作角色可读取公开输入与已发布产物，也可调用统一的文本模型服务。每次调用只能在自己的暂存区写入；即使同一角色再次调用，也不能覆盖历史产物。

| 角色 | 可发布的产物 | 可提出的状态更新 | 额外工具 |
| --- | --- | --- | --- |
| Designer | 设计文档、报告 | 需求规格、设计 | 无 |
| Art Producer | 素材、文档、报告 | 素材清单 | 图像生成、只读素材库检索与导入 |
| Developer | 游戏项目、日志、报告 | 需求实现状态、新游戏版本、构建证据 | 隔离进程执行、当次暂存文件删除 |
| Player | 操作轨迹、截图、视频、日志、报告 | 试玩与重放证据 | 隔离进程执行 |
| QA Reviewer | 报告 | 需求验证状态、问题单、交付建议 | 无 |
| Scheduler | 无 | 无；仅返回下一步决策 | 只读工具与文本模型 |

附加规则：

- 只有 Designer 能创建需求；其他角色不能改写需求描述、原文依据、验收条件或依赖关系。
- Designer 不能声明实现或验收通过，Developer 不能给自己的实现验收。
- Player 不能修改游戏；需要运行时可把只读项目复制到容器临时目录，正式游戏版本保持不变。
- Developer 仅提交构建检查证据；Player 提交试玩、重放和观察证据。
- `fingerprint()` 复用提交时的内容摘要算法，计入读取预算；`remove_output()` 仅允许 Developer 删除当次暂存区的安全路径，不能删除公开输入、历史工程或其他调用产物。
- 关闭问题需要当前游戏版本的证据。
- 原始任务、预算和运行日志没有角色更新入口。
- 结构校验无法判断自然语言验收条件是否被弱化，QA 仍需依据原始 instruction 审查。

## 5. 调用与提交

```python
from pathlib import Path
from adaptive_game_generation.models import ResourceLimits
from adaptive_game_generation.runtime import Executor, Workspace

workspace, task = Workspace.prepare(
    Path("runs/task-001"),
    Path("benchmark/tasks/task-001"),
    task_id="task-001",
    benchmark_snapshot="your-fixed-snapshot",
)
executor = Executor.create(
    workspace, task,
    run_id="run-001",
    controller="fixed_loop",
    budget=ResourceLimits(
        max_model_calls=30,
        max_input_tokens=300000,
        max_output_tokens=50000,
        max_tool_calls=500,
        max_wall_seconds=1800.0,
    ),
)
request = executor.request(
    "designer", "Create the English game specification", ("Ground every requirement in the instruction",)
)
# response = await executor.execute(request, designer_adapter)
```

适配器收到 `RoleContext(request, state, tools)`。通过 `tools.write_text("design.md", text)` 写出的文件会得到带本次调用命名空间的逻辑路径；使用它构造 `ArtifactRef`，再放进 `RoleResult.artifact_refs`。

返回值的请求编号、角色、状态版本必须匹配。只有 completed/partial 结果可以发布变更。产物必须真实存在、类型正确、属于本次输出范围；有摘要时会核对摘要。全部输出文件必须被声明的文件或项目目录覆盖，不能暗中发布额外文件。更新引用必须已登记或由本次声明。

通过校验后，暂存区整体移动到公开产物目录，再原子保存新状态。提交失败时不接受数据更新，暂存内容不提供给下一角色。发布后、保存前发生中断的孤立目录，会在恢复时移入私有隔离区。

新游戏版本只能基于当前版本追加。原有游戏、证据与产物版本不可覆盖。新游戏或需求规格变更会使现有验收结论回到待验证状态；历史证据与结果日志继续保留。

目前未实现用于官方提交的最终目录组装。Player 的轨迹保存在其独立命名空间，后续交付适配器应把选定版本与轨迹复制成 Benchmark 要求的游戏目录，不能改写历史游戏产物。

## 6. 预算与计费口径

`RuntimeConfig.role_limits` 是两种范式共用的单次角色上限；请求必须使用该配置，不能由控制器或角色临时扩大。Scheduler 使用固定的 `scheduler_limits`，其消耗同样计入运行总预算。配置会保存，恢复时拒绝传入不同配置。

每次工具操作：

1. 检查角色权限、调用生命周期和剩余时间。
2. 检查总预算与单次预算，并在私有账目中持久化预留额度。
3. 调用工具、服务或隔离后端。
4. 按返回用量及实际耗时结算；成功、异常、超时和取消都会记录。

文本模型适配器必须提供完整请求的输入 token 上界，并遵守传入的输出上限和超时。成功时用实际用量替换预留值；缓存 token 是输入的子集，不重复计数。服务返回的实际用量超过预留上界时，保留实际数值并拒绝接受本次结果。

服务异常或进程中断，可能无法知道实际 token 用量。这时保守保留已预留的 token 数，并设 `token_usage_complete=False`。这些数字是预算占用估计，不能当作实测 token 或账单金额。只要存在有限 token 上限，未知用量会阻止后续执行，避免用失败请求绕过预算。自动向供应商查询与对账尚未实现。

所有文件、模型、图像与沙箱操作都计入工具调用数和工具耗时；图像生成另计 `image_calls`，文本模型另计 `model_calls`。单次调用墙钟时间包括角色计算与工具等待，总运行墙钟时间还包含调用间空闲时间。

`None` 表示没有指定该维度上限；`0` 表示零额度。调用预算用尽会返回失败原因，是否换角色或结束由控制器决定。不能因为某次角色达到自己的上限，就无条件结束整个实验。

异步超时要求可信适配器遵守取消协议；不得在事件循环中执行长时间阻塞工作或吞掉取消信号。生成代码的强制终止由沙箱后端负责。文件输出大小在读写、复制和提交时检查；沙箱 `/output` 大小在进程结束后检查，这不是宿主磁盘的实时硬配额。

## 7. 服务与沙箱配置

`Services` 接收 `TextProvider`、`ImageProvider` 和 `SandboxBackend`，并为 [Art Producer](art_producer.md) 新增 `libraries: AssetLibraryProvider` 与宿主声明的 `image_info: ImageServiceInfo`。执行层本身不绑定 LLM SDK；已提供可选的 `OpenAICompatibleTextProvider`，通过异步 SDK 将真实用量映射到 `TextReply`，显式加载宿主配置且关闭自动重试。服务身份与完整实验配置应固定，并通过 `service_configuration_id` 对应宿主记录；框架无法验证一个可信适配器内部实际使用了哪个模型。[Designer 角色与真实调用示例](designer.md) 已基于此接口实现；其模型调用与文件写入均经同一工具入口执行。素材库工具仅允许 Art Producer 调用，并计入工具预算；mock 图片调用同样计入 `image_calls`，但服务身份和产物明确标记为 mock，不能当作真实生图费用。

默认 `DisabledSandbox` 拒绝运行任何生成程序。可显式配置：

```python
from adaptive_game_generation.runtime import DockerSandbox, Services

services = Services(
    sandbox=DockerSandbox(image="your-preloaded-game-runtime@sha256:...")
)
```

这里的镜像是宿主可信配置，须预先具备所需 Godot、渲染和重放工具，且不能包含评测秘密或凭据。优先使用固定摘要。代码不自动安装 Docker、不启动守护进程，也不自动拉取镜像。

内置后端采用无网络、只读根文件系统、非 root 用户、删除 capabilities、禁止新增权限、CPU/内存/PID 限制；只挂载公开工作区和本次输出目录，不挂载 Benchmark 源目录、私有控制区、宿主用户目录或 Docker socket。宿主 API 凭据不传入容器。[Docker 运行参数](https://docs.docker.com/reference/cli/docker/container/run/)、[资源约束](https://docs.docker.com/engine/containers/resource_constraints/)。

容器中 `/workspace` 只读，`/output` 为当前角色输出，`/tmp` 为有容量限制的临时空间。需要导入缓存或存档的引擎应使用临时副本。容器用户必须有权写入配置的输出目录；宿主自身保持对运行目录的独占控制。

运行结束、输出过大、超时或取消时，后端会清理 Docker 客户端并按唯一名称删除容器。无法确认清理成功会禁用该后端并终止当前运行，不能继续假定进程已经停止。框架进程硬崩溃后，持久化账目可以恢复；容器守护进程中的遗留容器仍需宿主运维清理，本步骤不提供跨进程沙箱进程发现服务。

## 8. 持久化、恢复与结束

状态以 UTF-8 JSON 原子替换；每次保存还保留历史快照。单写者锁在 Windows 使用系统文件锁，在其他平台使用 `flock`。不同进程或同一进程的并发执行器不会同时修改同一次运行。

Windows 索引器或文件读取者可能短暂阻止账目文件的原子替换。仅对 Windows 错误 5/32/33 最多尝试 6 次原子替换，总等待不超过 0.31 秒；不重新执行工具或模型请求。持续权限错误仍失败，原文件保留。

目录产物的哈希按逐层、忽略大小写的相对路径排序后累计原始路径与文件 SHA-256，避免 `Path` 在 Windows/Linux 上的默认排序差异。路径大小写冲突仍由发布校验拒绝。此排序保留已有 Windows 运行产物的摘要；从旧版本 Linux 混合大小写目录迁移时，应重新发布并记录引用迁移，不能直接把不匹配的旧摘要当作有效。

`Executor.open(workspace, services=...)` 会恢复未结束的角色或 Scheduler 调用：保留已知及预留成本，补写 interrupted 结果，并隔离未提交产物。恢复不会自动重试外部模型调用，也不会把缺失账目当作零费用。

工具审计位于 `.private/tools.jsonl`，包含调用身份、工具名、结果、错误代码和用量快照。它不保存模型提示词或宿主异常全文，以免把凭据或私有路径带入角色反馈。

`Executor.finish("completed", summary)` 使用共同交付门槛：当前版本有通过的构建与重放证据、需求全部验证通过、QA 就绪、无未关闭的严重阻塞问题。其他终止原因会记录当前游戏版本及未解决问题。它只检查内部记录，不替代外部官方评测。

## 9. 本地示例与测试

在安装本项目后的任意支持的 Python 环境中执行：

```sh
python examples/runtime_demo.py --output ./runtime_demo_output
python -m pytest -q
```

示例直接导入快照内真实 `platformer-time-loop/instruction.md`，运行一次只处理跳跃需求的受控文件事务，生成英文文档、需求状态和运行日志。它是第 2 步的最小执行器检查，不是完整 Designer 能力测试；完整角色示例见 [Designer 说明](designer.md)。示例不请求模型服务、不执行生成代码，任务源目录的隐藏资料不导入公开工作区。

普通测试使用合成任务与可信假服务，覆盖权限、隐藏资料导入隔离、预算、异常计费、取消、恢复、发布回滚和多角色状态衔接。模拟的构建与试玩记录只用于测试执行框架，不代表真实游戏验证。

容器集成测试需要运行中的 Docker 和已预置、包含 `python` 的 Linux 镜像。通过环境变量 `GAMECRAFT_SANDBOX_IMAGE` 显式指定镜像，再运行 `python -m pytest -q -k real_container`。没有条件时该项明确跳过；Windows 用户没有符号链接创建权限时，对应真实链接测试也会跳过。

第 5 步扩展：`DecisionProposal` 和 `SchedulerDecision` 新增可选 `objective` 与 `completion_criteria`，旧记录缺省仍能读取。自适应控制器的新协议要求二者非空，并将其连同公共角色目标一起传给工作角色。日志同时保存原始模型响应和完整决策输入。详见 [Scheduler](scheduler.md)。
