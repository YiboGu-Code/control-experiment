# Developer 开发说明

正式入口使用 [素材规范化与切图契约](asset_normalization.md)：从 integrated_assets 装配已切好的 PNG，不再加载 Art 原图；源码和交付检查拦截图集误用，MCP 可查看联系表，未解决布局保留为美术依赖。

Developer 位于 `src/adaptive_game_generation/roles/developer/`，沿用共享状态、公共执行器、权限、预算、版本化产物和审计机制。固定 Loop 与 Scheduler 调用同一个角色实现。

正式入口现默认启用 [阶段思考、按需上下文与局部修改](developer_optimization.md)：首次实现和复杂修复继承原深度思考；明确的局部语法修复和结束决策使用独立强度；支持带摘要与原文校验的 `edits` 和公开资料读取。以下旧版调用数以该文档的新配置为准。

现已提供可选 `developer.v4`：`--godot-mcp` 开启真实 MCP 场景检查、隔离预览、看图工具反馈和后续补丁修复。正式入口开启优化后，最多 11 次主模型决策（包含为读取增加的 3 轮）、3 次检查/看图调用；全部计入原有总预算。关闭优化恢复 8 次主决策。预览截图只用于开发诊断，Player 仍独立采集和重放，QA 仍按正式证据验收。安装、完整输入输出、运行命令与本地测试见 [Godot MCP 开发与运行说明](godot_mcp.md)。

003 后续修复：全工程构建证据关联当前全部需求，修改范围仍单独记录，构建不会升级为玩法验收。轨迹装配固定优先兼顾普通入口和命名场景；协议要求为长于单条 trace 的机制实现确定性边界场景，连接真正的转换/实体交互逻辑，并在素材布局改变后调整裁切与缩放。两个真实引擎诊断确认了旧游戏“回卷不生成幽灵”和 `Sprite2D.animation` 错误；原始游戏保持不变，见 [003 诊断](fixed_loop_real_images_003_diagnosis.md)。

第 4 步新增 `developer.v2`，由公共宿主逻辑装配已采集的 Player 轨迹，模型仍不能编写或覆盖 `demo_outputs/`；装配后的新版本必须重新构建、试玩和重放。当前 Fixed Loop 默认使用继承这些能力的 `developer.v3`，原始配置每次最多 5 次文本调用，开启执行优化后为 8 次。既有单角色示例继续使用 v1，详见 [固定 Loop 说明](fixed_loop.md)。

本次示例继续使用 Benchmark 原始 `platformer-time-loop/instruction.md`，导入已保存的真实 Designer 与 Art Producer 规划，保留 Art 的 12 张 mock 图片。Developer 使用 `.env` 的真实文本模型，构建检查使用真实隔离 Godot，不用假日志代替。完整记录见 [Developer 审阅入口](examples/developer_review/review.md)。

## 输入与边界

| 输入 | 处理 |
| --- | --- |
| 原始任务 | 完整保留英文 instruction，仍为最终需求依据 |
| Designer 规格 | 校验注册引用与文件哈希；首次实现全文提供，优化后的修复阶段可按资源索引请求全文 |
| Art 清单及素材 | 校验当前共享资产、来源引用与字节哈希，按清单目标路径接入 |
| 当前工程 | 宿主读取公开、受限类型的完整源文件，按历史项目哈希校验并创建独立新版本；优化后的模型输入按需载入 |
| 问题与证据 | 提供被选中或分配给 Developer 的问题、相关证据记录和指定文字附件 |
| 内部检查日志 | 有限轮次内反馈实际导入、启动错误，驱动模型修复 |

模型输入、提示词、生成代码、UI 文案、说明与日志结构均为英文。主模型的图片附件只提供引用，标注没有视觉检查；v4 的独立看图工具收到实际 PNG，主模型收到其明确标注的文字解释。隐藏评分标准、参考解答、模型密钥、控制器策略和账目不会加入角色输入。

Developer 负责游戏工程、逻辑、场景、交互、UI、关卡内容和确定性场景入口。Art 文件和许可证由框架接入，模型不能覆写，也没有生图或素材库工具权限。Developer 不生成 Player 的试玩轨迹，不提交试玩或验收证据，不关闭问题，不将需求标记为 `verified_pass` 或 `verified_fail`。v4 可以注册 MCP 诊断截图和日志，这些产物不升级为 Player 证据。

## v3：源文件检查与重复错误反馈

`fixed-loop-real-images-002` 暴露出“导入返回 0，但场景只加载一部分”和重复修复未改变根因的问题。v3 增加：

- 应用模型补丁之前，对完整候选工程做有限的静态检查：`.tscn/.tres` 顶层混入方法调用、未加引号的字符串资源 ID、`project.godot` 中以 JSON 字典冒充输入事件对象。错误通过 `proposal_feedback` 返回，带文件和行号；补丁不落盘，不消耗引擎调用，纠错仍消耗共享模型额度。
- 这些检查只覆盖明确的错误模式，不替代 Godot 解析器。扫描跳过字符串、注释和嵌套属性值。关于场景切换后使用 `get_tree()` 的发现只是建议，因为常驻 autoload 与将被销毁的场景节点具有不同生命周期。
- 每次提示增加 `repair_brief`：最近失败的错误、此前相同错误出现的尝试数、对应源文件片段及静态发现。也读取上一轮已注册 Developer 报告，使跨固定轮次的修复能看到重复错误。优化开启后完整工程和原始日志通过资源索引按需读取；关闭优化时仍全部发送。
- 引擎输出中的“父节点路径消失”警告也判定失败；普通无关警告不因此一概失败。保留原先对退出码为 0 的脚本错误的检查。
- 英文提示补充声明式资源格式、场景切换生命周期、实际 `InputEventKey` 对象、可见角色素材和实际关卡内容要求。不能靠降低验收条件、屏蔽错误或声称修好了来通过。

v3 最初将每次 Developer 模型调用从 3 次提高到 5 次；当前正式入口开启执行优化后为 8 次，源码生成、资料读取、协议修正和构建修复共用这一额度，仍受全局 `--max-model-calls`、token 和时间预算约束。`.env` 的 Developer 输出上限继续传入角色，本次失败回复均正常结束，没有输出截断，因此未修改其 token 配置。v1/v2 不启用新增静态检查及重复错误摘要；一次运行内版本与配置冻结，未来 Scheduler 对比需使用相同配置。

源码与日志夹具见 `tests/fixtures/developer_real_images_002/`。`tests/test_developer_diagnostics.py` 测试原始错误、纠错、预算和兼容行为；`tests/test_developer_godot_regressions.py` 可选择执行真实本地 Docker/Godot 对照，无文本模型或生图调用：

```powershell
$env:GAMECRAFT_GODOT_REGRESSION_TESTS = '1'
$env:GAMECRAFT_GODOT_REGRESSION_OUTPUT = 'runs/developer-godot-regression-new'
python -m pytest tests/test_developer_godot_regressions.py -q
```

输出目录应为新目录。默认使用已安装的 `gamecraft-godot:4.5.1` 镜像，可用 `GAMECRAFT_GODOT_REGRESSION_IMAGE` 指定另一个可信的本地镜像；不拉取镜像。测试项目只用于复现错误，不是修好的 Benchmark 游戏。详见 [002 运行诊断](fixed_loop_real_images_002_diagnosis.md)。

## 增量实现与版本

模型返回 `DeveloperProposal`，其中 `files` 为新增或修改文件的完整 UTF-8 内容，`edits` 为校验原文件摘要后执行的唯一原文替换，`delete_paths` 为删除的已有源文件；未提及文件保留。还必须提供变更摘要、影响需求、实现声明、操作说明、完整的命名场景列表和剩余工作。优化开启时还支持 `status=context`，详见上述优化协议。

工程必须保留根目录 `project.godot` 与 `Main.tscn`，并设置 `run/main_scene="res://Main.tscn"`。允许 GDScript、Godot 文本场景/资源、shader、JSON、配置与文档；禁止路径越界、大小写冲突、文件/目录重叠、原生可执行文件、插件、隐藏路径以及模型改写 `assets/`、`licenses/`、`demo_outputs/`。

角色先校验整个补丁，再复制上个工程版本、接入当前 Art 资产并应用文件变更。初次调用没有工程时不能 `no_op`；已有暂存修复不能以 `no_op` 或 `blocked` 中途放弃。没有必需 Designer 输入时直接阻塞，不花费模型调用。无效 JSON、重复键、结构字段错误会在同一调用上限内回传给模型纠正，保留原回复与校验原因；校验通过前不写入工程。宿主不会修补或自动补全模型回复。路径越界、未知需求 ID、越权或预算错误直接拒绝。协议纠错耗尽时不发布暂存变更。

版本报告保存固定 `DeveloperConfig`，后续调用不能悄悄改变同一历史的角色配置。每次成功发布创建新的 `GameRevision` 和项目目录，父版本为当前游戏版本，旧工程不会原地修改。已有版本的内容哈希不一致时拒绝继续。

## 真实导入、启动与有界修复

每轮代码补丁后固定执行以下检查，具体命令由宿主代码构造，模型不能提交任意 shell 命令：

```text
godot --headless --version
godot --headless --path /output/game --import
godot --headless --path /output/game --quit-after 5
godot --headless --path /output/game --fixed-fps 60 --quit-after 60 -- --scenario <id>
```

默认检查前两个声明的场景，每项最多 90 秒；单角色示例最多 3 次模型调用，Fixed Loop v3 配置为 5 次，初次实现与协议纠错或日志驱动修复共用额度。固定命令、场景数量、迭代上限和预算在两组实验间保持一致。每次检查保存参数、真实退出码、stdout、stderr 及状态；遇到失败立即停止本轮后续检查，把诊断交给下一轮模型。

退出码为 0 但日志含 `SCRIPT ERROR:`、`ERROR:` 或 `Parse Error:` 也会失败。`--quit-after` 是迭代次数，不能当作秒数；所有引擎参数位于 `--` 之前，命名场景参数在其后。依据：[Godot 4.5 命令行文档](https://docs.godotengine.org/en/4.5/tutorials/editor/command_line_tutorial.html)。

检查还会检测生成游戏是否修改源文件或受保护 Art 字节。引擎生成的 `.uid` 文件允许保留，`.godot` 缓存在发布前删除。发布物是 Godot 源工程；首次打开应由编辑器导入资源。命令行运行新副本时，先执行 `godot --headless --path game --import`，再执行 `godot --path game`；单独启动一个未导入的新副本不属于本次通过的检查序列。删除能力仅限 Developer 本次暂存区，拒绝链接、路径越界以及访问已发布历史。

状态含义：

| 情况 | 游戏构建状态及发布行为 |
| --- | --- |
| 最终检查全部通过 | `build_status=passed`，提交真实 `build_check` 证据；模型实现声明最多更新为 `implemented_unverified` |
| 修复次数用尽仍失败 | 发布可检查的工程及完整失败日志，`build_status=failed`，不新增需求实现进度声明 |
| 没有可用隔离引擎 | `build_status=not_run`，保存代码与未运行原因，不伪造成功记录或构建证据 |
| 模型/预算/提交校验失败 | 不发布此次暂存变更，保留公共执行器中的失败账目 |
| 容器清理无法确认 | 沿用执行器的终止保护，不继续运行后续角色 |

含 mock 美术、模型报告剩余实现工作或构建未通过时，角色返回 `partial`。即使构建通过，也只证明有限导入和启动检查完成；尚不能证明玩法、画面、全部关卡或轨迹重放正确。原始 instruction 的最终完整交付目标没有因此降低。

## 产物

```text
artifacts/developer/<invocation>/
  game/
    project.godot
    Main.tscn
    scripts/ ...
    assets/ ...
    licenses/ ...             # Present when an imported asset has a license file.
  build_log.json
  developer_report.json
  developer_handoff.md
```

`developer_report.json` 记录工程版本、上下游引用、增删文件、影响需求、模型实现声明、素材版本、mock 标记、场景、操作说明、逐轮检查、协议拒绝原因及其模型轮次、剩余验证事项。运行后的共享状态包含新工程引用及真实基础检查证据，正式验收仍属于 QA。

`/output/game` 是隔离容器内部的暂存挂载位置，最终部署目录仍是 instruction 的 `/workspace/game`。工程使用 `res://` 读取资源、`user://` 存储进度，不将本机路径写进游戏。目录映射不会改写 instruction。

Developer v3/v4 保留默认 `.godot` 缓存位置；提交前检查拒绝 `config/project_data_dir_name` 和 `config/use_hidden_project_data_directory` 覆盖项。框架仅将 `.godot` 视为可删除的引擎缓存，改成可见目录会让缓存混入源码完整性检查。导入目录的写权限由公共沙箱负责，不能由模型通过换缓存路径解决。

Developer v4 的成功 `finish` 是本次调用最新的剩余工作声明：当前版本必须已构建通过、有当前版本的有效 MCP 预览和视觉反馈，再用 `finish.remaining_work` 替换此前补丁中的剩余工作。已经完成的预览不会因为早期补丁为 `partial` 而永久阻止 `completed`；仍有剩余工作、mock 美术或未完成的当前自检则仍为 `partial`。Developer 的 `completed` 不代表 QA 就绪，Player 的前置条件检查当前版本的构建结果，而不是强制要求 Developer 返回 `completed`。

Fixed Loop/Scheduler 权限故障的日志、修复和离线复查见 [运行诊断](developer_permissions_diagnosis.md)。

## 模型与运行环境配置

沿用项目安装方式：

```text
python -m pip install -e ".[test,live]"
```

Developer 的独立模型前缀为 `CODE_DEVELOPER`，与已有 Designer、Art 配置互不覆盖：

```dotenv
CODE_DEVELOPER_MODEL=deepseek-flash
CODE_DEVELOPER_TIMEOUT_SECONDS=600
CODE_DEVELOPER_MAX_OUTPUT_TOKENS=65536
CODE_DEVELOPER_REASONING_EFFORT=low
CODE_DEVELOPER_THINKING=disabled
DEVELOPER_SANDBOX_IMAGE=gamecraft-godot:4.5.1
```

API 凭据仍取 `MODEL_GATEWAY_API_KEY`、`MODEL_GATEWAY_BASE_URL`；进程环境覆盖 `.env`。也支持该前缀下的 `TOKEN_PARAMETER`、`JSON_MODE`、`STREAM` 和 `INPUT_TOKEN_MARGIN`。文本适配器关闭自动 API 重试，实际 token 使用量进入共同账目。

可选 `THINKING=enabled|disabled` 使用 DeepSeek 的 `thinking.type` 扩展，默认不发送，其他兼容网关无需设置。本次示例两次思考模式输出分别在 32,768、65,536 token 上限截断，因此明确切换为非思考模式后重测；未把截断代码当作成功结果。设置和请求均完整记录，两组对比实验应使用同一配置。依据：[DeepSeek Thinking Mode](https://api-docs.deepseek.com/guides/thinking_mode/)。

本次提供一个可选的测试镜像配方：

```text
docker build -t gamecraft-godot:4.5.1 configs/godot
```

镜像使用[官方 Godot 4.5.1 发布版](https://github.com/godotengine/godot/releases/tag/4.5.1-stable)。镜像构建时下载依赖，**角色执行时容器关闭网络**，使用非 root 用户、只读根文件系统、CPU/内存/PID 限额，仅挂载公开输入及当次暂存目录。`.env`、隐藏 Benchmark 和 Docker socket 不挂载，模型凭据不传入容器。

真实示例把用户指定的本地镜像解析成不可变 image ID，写入 `sandbox_config.json` 与 `RuntimeConfig.service_configuration_id`；运行时不自动拉取镜像。服务器可配置自有预装 Godot 的镜像，或注入实现 `SandboxBackend` 协议的隔离后端；无需限定 Windows 或某个本地 Godot 路径。不会回退到宿主直接运行模型代码。

接口接线：

```python
from adaptive_game_generation.providers import GatewayConfig
from adaptive_game_generation.roles import Developer, DeveloperConfig
from adaptive_game_generation.runtime import DockerSandbox, Services

model_config = GatewayConfig.from_env(env_file, prefix="CODE_DEVELOPER")
services = Services(text=text_provider, sandbox=DockerSandbox(image=preloaded_image_id))
# Inject services when creating or reopening the common Executor.
role = Developer(DeveloperConfig(max_output_tokens=model_config.max_output_tokens))
request = executor.request(
    "developer",
    "Implement or repair the public game specification and integrate the supplied Art package",
    ("Preserve source requirements", "Run isolated basic checks and record remaining work"),
)
result = await executor.execute(request, role)
```

新增公共工具 `fingerprint()` 与执行器提交校验使用相同摘要算法；`remove_output()` 仅限 Developer 的当次暂存区。读写、复制、删除、模型、容器和检查后的资源校验均进入共同工具预算与审计。

## 测试与复现

```text
python -m pytest -q
```

离线测试用小型源文件和明确命名的脚本化服务验证权限、校验、增量提交和失败行为；不会将这些夹具作为真实生成示例。真正的端到端测试独立启用：

```powershell
$env:GAMECRAFT_DEVELOPER_LIVE_TESTS='1'
$env:GAMECRAFT_DEVELOPER_LIVE_OUTPUT='runs/developer-live-new'
python -m pytest -q -s tests/test_developer_live.py
```

也可在已安装项目的环境运行：

```text
python examples/developer_demo.py --output runs/developer-live-new
```

输出目录必须为新目录。示例校验原始 instruction、上游设计和素材哈希，经执行器导入历史真实产物；只调整新工作区的逻辑引用，不伪造新的 Designer/Art 模型调用。本次 Developer 不进行任何图片生成，复用的图片明确保持 mock 身份。

审阅目录包含原始 instruction、上游输入与来源、完整实际 API 请求/原始回复、每轮修复日志、可打开的 Godot 项目、角色结果、前后状态、工具审计和用量汇总。测试没有读取隐藏评测资料，也没有运行正式 Benchmark 评分器。

## 本次真实测试结果

成功记录为 `runs/developer-live-006`：真实 DeepSeek `deepseek-flash` 调用 2 次，第一轮状态字段与剩余工作不一致，第二轮由模型修正；第一轮未进入工程。随后真实 Godot 4.5.1 的版本、导入、普通启动、`near_rewind` 和 `near_victory` 启动五项检查均通过，未触发引擎错误修复。引擎错误驱动修复另由离线边界测试覆盖。

发布 `game-0001`，含 16 个模型源文件、12 张逐字节校验的 mock 图片和引擎生成的资源引用文件。角色状态为 `partial`，24 项模型实现声明最多标记 `implemented_unverified`，没有 QA 验收或试玩证据。成功运行报告输入 98,652 token（缓存 75,264），输出 35,071 token，图片调用 0 次。

最终模型版本、非秘密配置、各轮原始输入输出、前后状态、审计和构建日志见 [完整审阅入口](examples/developer_review/review.md)。早期截断、协议错误和一次模型调用前的文件权限错误也完整保留，不能只用成功运行的用量代表本次开发总消耗。

静态阅读生成工程发现同章关卡布局重复、命名场景初始化和部分交互仍有待修复/验证，详见 [示例审阅说明](examples/developer_review/reviewer_notes.md)。示例证明 Developer 执行与检查链路可用，不代表这个游戏已达到原始 instruction 的完整交付要求；下一角色仍为 Player。

最终验证：全量 `python -m pytest -q` 为 **238 passed, 5 skipped**；其中 Developer 离线用例 34 项。单独开启的真实 Developer 集成测试为 **1 passed**。默认跳过项为三个付费真实角色测试、当前 Windows 无权创建的符号链接测试，以及需要显式 Python 容器镜像的旧沙箱测试；真实 Developer 的 Godot 容器检查已在独立 live 测试中执行。
