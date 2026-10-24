# Developer 阶段、上下文与局部修改

Fixed Loop 和 Scheduler 通过同一 `role_setup`、Developer 与公共执行器使用这三项优化。正式入口默认启用，单角色旧示例默认保持原协议。此次没有修改 `.env`、模型名称、思考开关、最大输出 token 或验收标准；没有执行在线模型、生图或真实引擎测试。

## 思考强度与质量保护

| 阶段 | 宿主判定 | 实际模型参数 |
| --- | --- | --- |
| `deep` | 每次角色调用的首次实现；玩法、架构、类型推断、运行时、多文件、重复错误；未知原因或升级请求 | 完整继承 `.env` 中原有思考配置 |
| `local_repair` | 首次出现、定位于一个现有文件、日志为 `Parse Error: Expected "…"` 或 `Expected '…'` 的明确语法错误 | 默认 `reasoning_effort=medium`，最多修改 2 个已有文件，替换旧文和新文或完整写入合计不超过 8192 字节 |
| `finalize` | 当前构建通过、当前项目的所有已有 MCP 预览通过、视觉反馈有效；无视觉发现，或预览额度已耗尽、只需报告剩余问题 | 默认 `reasoning_effort=low`，只能请求资料、检查或结束；继续改代码必须升级。宿主保留未解决的视觉错误，不能因此宣告完成 |

较轻阶段也保留原 `thinking` / `enable_thinking` 和输出额度。仅当网关已配置 `reasoning_effort` 时才调整该字段；只配置思考开关的接口完整继承原配置，避免凭空添加不支持的参数。没有阶段接口的旧 provider 也安全回退到原参数。每次调用记录实际应用的参数，不能只看阶段名称推断思考强度已经改变。

模型可返回 `request_deep_reasoning=true` 主动升级。校验失败、局部修改超范围会自动升级，直到下一份补丁被接受且构建通过后才重新判定阶段。重复引擎错误由历史报告与本次构建共同计数。复杂问题不会仅因“代码改动小”而降档。过期预览或失败的场景检查仍不能进入收尾阶段。网关如拒绝某个强度，保存原始失败并按原有失败规则停止，不自动重试或换模型。

## 按需读取

宿主仍读取和校验完整的公开工程与产物。优化的是发送给模型的内容：

- 保留 Benchmark 原始英文 instruction、全部需求条目、当前问题、交付契约、实际集成素材及归一化帧信息。初次无工程时提供完整 Designer 文档。
- 完整源码默认自动选入至多 48 KiB；优先错误文件和工程入口，并在该额度内保留先前自动选入的小模块。每个文件提供完整内容的 SHA256、字节数与函数索引。
- 历史报告、大段构建输出、附件正文、旧检查、非当前必需的设计文档提供资源索引。最近构建状态、错误片段、重复计数和视觉发现仍直接提供。省略不代表该功能或证据不存在。
- 通过 `status=context` 获取完整资源或行区间。资源来自已校验的公开内存快照，只允许索引 ID，不解析任意磁盘路径或 URL，不读取隐藏评分、参考答案或 `.env`。

```json
{
  "status": "context",
  "summary": "Read the current interaction code before changing replay behavior",
  "reads": [
    {"resource_id": "source/scripts/main.gd", "start_line": 120, "end_line": 220},
    {"resource_id": "input/design"}
  ],
  "request_deep_reasoning": true
}
```

行号从 1 开始，两端包含；省略两个行号读取全文。响应带**完整资源**的摘要，即便只读取片段也可绑定正确版本。一次最多 8 项，默认每次角色执行最多 3 次新增资料请求。显式读取结果累积保留，整个工作集最多 512 KiB；每轮都从当前已验证快照刷新正文与摘要，修改一个文件不会清空已读取的其他文件。重复读取已完整载入的资源不再占用新增资料额度，但模型调用仍计费、计入预算。

可通过 `drop_resources: ["source/scripts/unused.gd"]` 释放不用的上下文；这不会删除工程文件。新增读取导致累计超限时，整次请求被拒绝且保留原工作集。已有文件变大、被删除或行区间失效时，宿主按预算保留仍有效的最近资源，并在 `evicted_resources` 中明确告知移除原因；不得将已移除的文件当作已完整读取。`retained_bytes` 和 `retained_byte_limit` 可用于下一次读取决策。单独申请深度思考不受资料额度耗尽阻塞，仍受模型轮数与总预算约束。

每次读取决策也算一次真实模型调用；宿主读取结果通过下一轮请求传入。正式入口为读取预留 3 轮空间：v3 主调用上限从 5 到 8；v4 从 8 到 11，另有最多 3 次视觉调用。所有决策仍共享这些轮数、角色与全局模型/token/时间/工具预算，读取不获得免费额度。无需读取时不会强制消耗多余轮数。到达预算而未完成时保留真实限制，不伪造完成。

## 局部代码修改

模型可在 `DeveloperProposal.edits` 中返回按原文替换的编辑。这里仅展示新增字段；实际响应仍需完整的状态、变更说明、需求、操作、场景及剩余工作。

```json
{
  "edits": [{
    "path": "scripts/main.gd",
    "expected_sha256": "<SHA256 from the current source catalog>",
    "replacements": [{
      "old_text": "func _ready() -> void\n",
      "new_text": "func _ready() -> void:\n"
    }]
  }]
}
```

宿主先校验整批编辑：文件存在、摘要相符、每段旧文唯一匹配（包括重叠出现的情况）、匹配范围互不交叠；所有匹配都基于同一份修改前原文。任一项失败都不应用这一批候选编辑，再把原因交给模型。不存在模糊替换或让模型运行补丁脚本的路径。

新文件或确有必要的整体重写仍用 `files`。重写已有文件前必须在本轮上下文读取过该文件全文，防止只看片段便覆盖其余实现。同一路径不能同时写、改、删。编辑后继续检查单文件/工程总大小、空字节、受保护路径、素材引用和静态规则，再运行原有导入、普通入口、场景启动及 MCP 检查。该校验保证候选批次通过后才开始写入；操作系统或预算异常仍按现有暂存/提交机制处理，不发布半完成批次。

Player 的独立输入采集、确定性重放和 QA 验收保持原路径。构建通过或 `finish` 都不等于玩法验收。mock 美术仍阻止最终美术就绪。

## 开关与兼容

以下是可选配置，缺省已经启用优化；无需为了启用它们更换 `.env`。

```dotenv
CODE_DEVELOPER_OPTIMIZE_EXECUTION=true
CODE_DEVELOPER_LOCAL_REASONING_EFFORT=medium
CODE_DEVELOPER_FINALIZE_REASONING_EFFORT=low
```

两个强度可取 `inherit`、`low`、`medium`、`high`。若网关仅支持当前高强度，可设为 `inherit`，仍保留按需读取和局部修改。`CODE_DEVELOPER_OPTIMIZE_EXECUTION=false` 恢复旧的上下文传递、原思考参数和主调用轮数。角色与网关配置写入实验记录，同一运行内冻结；请用新输出目录，不要沿用旧 checkpoint。

## 用户在线运行

沿用已安装的镜像与 `.env`。以下两条命令使用相同角色配置、15 次角色执行上限、64 次总模型调用和时间预算；Scheduler 自身调用也计入总数。默认使用原始 `platformer-time-loop/instruction.md`，默认真实图片；两组都加 `--mock-images` 可对照图片 mock。MCP 未安装时两组同时去掉 `--godot-mcp`。

```powershell
$env:PYTHONPATH='src'
python examples/fixed_loop_demo.py --godot-mcp --output runs/fixed-loop-developer-opt-001 --max-cycles 3 --max-role-calls 15 --max-model-calls 64 --max-wall-seconds 43200
python examples/scheduler_demo.py --godot-mcp --output runs/scheduler-developer-opt-001 --max-role-calls 15 --max-decisions 30 --max-model-calls 64 --max-wall-seconds 43200
```

可以分别加 `--preflight-only` 并使用另外的新目录检查配置，不调用远端模型。不要只凭运行速度评价优化：比较 QA 遗留问题、需求覆盖、Player 重放、最终画面，再看模型时间、上下文量和重试次数。已有 opt-001 真实运行的分析及下一次运行命令见 [运行诊断](fixed_loop_developer_opt_001_diagnosis.md)。目前不能声称优化已经提速或提高最终交付质量。

## 完整输入输出与离线复现

真实运行会保存：

- `experiment.json`：冻结的角色、网关、预算和代码标识。
- `model_calls/developer/NNN/request.json`、`response.json`、`metadata.json`：SDK 实际请求、完整返回（包括服务返回的思考内容）、有效阶段参数、usage 和时间。流式响应沿用原有逐块保存。
- `workspace/public/artifacts/developer/<invocation_key>/optimization/`：完整已校验输入与初始源码、每轮实际 prompt 和原始输出。目录键由宿主生成，具体路径以状态中的产物引用为准。
- `developer_report.json` 的 `optimization_calls`：阶段及理由、有效参数、请求/回复字节量、模型耗时、完整载入的文件与局部编辑数。`full_context_prompt_bytes` 是同一状态旧式完整上下文的序列化字节量，仅作结构比较，不是实测 token、费用或旧版本延迟。
- 原有构建、MCP、Player、QA、账目与交付文件不变。失败调用仍可在原有工作区暂存/失败记录与 provider 捕获中诊断，未提交报告不代表没有消耗调用。

离线审阅材料见 [本次审阅入口](../runs/developer-optimization-review-001/review.md)。使用真实 Benchmark instruction，实际执行两种控制器、角色适配器和 SDK；外部文本、视觉、引擎与重放输出均为明确标注的测试夹具，图片为 mock。它验证分阶段参数、读取、准确编辑、预算和验收边界，**不证明真实模型更快或游戏质量提高**。

复现时指定一个新的材料根目录（测试默认只用临时目录）：

```powershell
$env:PYTHONPATH='src'
$env:DEVELOPER_OPTIMIZATION_REVIEW_ROOT='runs/developer-optimization-review-new'
python -m pytest tests/test_developer_optimization.py tests/test_developer_profiles.py tests/test_developer_optimization_chain.py -q
Remove-Item Env:DEVELOPER_OPTIMIZATION_REVIEW_ROOT
```

已有真实请求的上下文体积对照也可独立复现，不调用模型：

```powershell
python examples/developer_context_review.py --source runs/fixed-loop-permissions-002 --output runs/developer-context-review-new
```
