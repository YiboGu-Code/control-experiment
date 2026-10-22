# Fixed Loop 开发与运行说明

第 4 步把五个角色接入同一个共享状态和执行账本，从原始 instruction 开始生成新的上游产物。入口为 `examples/fixed_loop_demo.py`，控制器为 `src/adaptive_game_generation/controllers/fixed_loop.py`。这条入口不导入历史角色示例。第 5 步 [Scheduler](scheduler.md) 已实现，两条入口共用运行装配。

入口默认使用 `.env` 中各角色的真实模型，以及 `ART_IMAGE_MODEL` 指定的真实生图模型。**图片默认通过 DashScope SDK 原生接口调用；其他角色继续使用 Chat Completions。** 只有显式传入 `--mock-images` 时才使用占位图；配置缺失或接口失败不会自动降级为 mock。003 诊断后已开启文本角色思考、流式输出，以及 Player/QA 的实际截图输入，详见 [模型配置](model_configuration.md)。所有发给模型的提示词、输入和角色输出使用英文，Benchmark instruction 原文不变。

## 顺序与待办

每轮严格按照 `Designer → Art Producer → Developer → Player → QA Reviewer` 前进。模型不能选择下一角色、调整单次预算或绕过权限。

| 角色 | 调用条件 | 跳过条件 |
| --- | --- | --- |
| Designer | 尚无设计，或有未关闭的指派问题 | 已有设计且没有相关问题 |
| Art Producer | 尚无清单、设计版本变化，或有指派问题 | 已与设计同步且没有相关问题 |
| Developer | 首次实现、设计/素材变化、构建未通过、未实现/验收失败需求、指派问题，或待装配采集轨迹 | 当前输入已集成，且没有上述工作 |
| Player | 首次检查当前游戏版本，或有试玩/证据问题 | 当前构建未通过；或已有当前版本报告且无相关问题 |
| QA Reviewer | 新游戏、新证据导致旧就绪判断失效，或有指派问题 | 当前就绪判断仍有效且没有相关问题 |

缺少设计或 Art 尚未处理当前设计时，依赖角色等待。若设计修改后 Art 明确返回 `no_op` 保留现有素材，也允许 Developer 继续，不强迫重新生图。缺少当前 Developer 工程时，Player/QA 等待。构建失败时 Player 跳过，QA 可以检查构建失败和缺失证据。跳过不调用模型，也不产生 `SchedulerDecision`。每个固定槽位的选择原因、待办 ID、前后状态版本和结果都写入 `checkpoint.json`。

单次调用使用共享执行器。现在执行器可以注入不同角色的 `Services` 和固定预算；旧的单一服务/预算接口仍兼容。服务、预算由宿主配置，模型请求不能覆盖。`controllers/work.py` 的 `WorkPolicy` 独立实现待办资格规则。Scheduler 直接复用这套规则、角色、预算、证据检查和结束条件，仅改变可执行角色的选择顺序。最终失败的角色调用只在下一个固定槽位重试，并将上一条安全错误说明带回角色输入。角色内部已有的有限纠错先在本次调用预算内完成：默认 `art-producer.v4` 会反馈 JSON、网格和素材来源等方案错误，保留搜索结果并要求完整重提；Fixed Loop 配置为 6 次文本额度、最多 3 次搜索，余下用于提交和纠错；独立角色默认仍为 4 次。提供商、权限、预算与图片服务失败不因此自动重试。

当前默认三轮是单任务开发联调的安全上限，不是已确定的正式对比协议。第 6 步仍需完整核对并冻结两组共同的外部预算和调用上限，并避免把提前撞上开发轮数上限的 Fixed Loop 与不限步的 Scheduler 直接比较。正式结果必须保留停止原因和未用预算，不能据本次单任务联调判断哪种范式更好。

若 Art 报 `invalid JSON proposal` 且 `image_calls=0`，先检查 `model_calls/art_producer/*/response.txt` 和 `response.json` 的 `finish_reason`，不要直接归因于生图接口。`fixed-loop-real-images-001` 中的图集网格冲突、未检索的 UI 路径和大量帧名导致回复截断，已在 [本次诊断与复跑说明](fixed_loop_real_images_001_diagnosis.md) 中记录。

## 轨迹如何进入最终项目

首次 Developer 实现时尚无真实轨迹，因此通常需要至少两轮才能形成带轨迹且经过重放的交付项目：

1. 第一轮 Developer 生成项目并运行 Godot 检查。
2. Player 在该版本上执行输入，保存实际采集轨迹、截图、帧日志和新进程重放结果。
3. 下一轮 Developer 的公共宿主逻辑读取当前游戏对应的 Player 报告，校验注册引用、内容哈希和实际执行轨迹，选取至多 2 条不重复的真实采集轨迹。新探索输入优先；每组存在两类输入时，优先各取一个普通入口和命名场景，再按采集顺序补充。此规则避免前两条菜单操作挤掉玩法场景，不代表轨迹质量已经通过。
4. 宿主将这些字节写入新工程的 `demo_outputs/demo_01.json` 等路径；模型仍不能编辑 `demo_outputs/`。报告记录来源游戏版本、Player 报告、原始轨迹引用和需求关联。模型只负责代码修复；若仅需装配，可以提交内容不变的 `project.godot`，并如实说明。
5. 新版本重新构建。Player 优先执行工程内全部轨迹，并在新版本上重新采集、执行两次新进程重放，再使用剩余试验预算探索。Fixed Loop 每次至多 5 条试验、5 次模型调用，打包最多 2 条，为后续探索保留至少 3 个名额；单条仍严格限制在 600 帧。新采集输入会形成下一轮 Developer 的装配待办。
6. QA 重新校验该版本的构建、原始日志、截图和轨迹匹配关系。

即使某条旧轨迹过去重放失败，也可以作为下一版修复后的试验输入；过去的通过或失败结论都不直接移植到新版本。装配后无法执行的命名场景、被修改的轨迹或再次分歧的重放都会留下失败证据。

这些能力由 `developer.v2` 和 `player.v2` 实现，当前 Fixed Loop 的 `developer.v3` 继承轨迹装配，并增加源码检查和重复错误摘要。Developer 每次最多 5 次文本调用；Art 为 6 次、最多 3 次搜索，Player 为 5 次，QA 为 4 次并允许一次引用纠错。总调用预算与固定顺序仍由控制器统一约束。新能力既可供 Fixed Loop 使用，也供 Scheduler 使用。既有单角色示例默认保留 v1。角色配置在一次运行内冻结。打包、源文件校验、构建和重放都在角色调用内计费，没有预算耗尽后额外执行游戏的隐含步骤。

## 结束与交付

| 原因 | 含义 |
| --- | --- |
| `completed` | 公共条件全部成立：当前构建通过、当前版本重放证据、全部需求经 QA 验证、当前就绪判断为真、无未解决的重大阻塞问题 |
| `iteration_limit` | 达到预设轮数，保留当前结果，不声称游戏验收通过 |
| `budget_exhausted` | 总预算或单次调用的资源边界阻止继续执行 |
| `timeout` | 运行期限到达 |
| `stalled` | 整轮只有跳过、无改动或依赖阻塞 |
| `failed` | 连续调用失败达到上限、消耗无法确认、隔离清理失败或宿主错误 |
| `cancelled` | 用户中断；已提交产物和调用账目保留 |

就绪条件与 `Executor.finish("completed")` 共用同一个判断。真实 QA 还会检查所有打包轨迹是否在当前版本重放通过，以及是否仍含 mock 美术。**mock 美术会使 final-art 门槛不通过。** 真实生图产物记录为 `is_mock=false`，但这个标记不代表图像质量、精灵帧布局或全部需求已经验收通过；仍须结合实际试玩和 QA 证据判断。

最终选择规则固定为 **最后一个已提交的游戏版本**，即使其构建失败，也不会悄悄换成较早的好版本。`delivery/game/` 是该版本的逐字节副本，导出前后校验树哈希，导出过程中不新增轨迹或运行游戏。导出仅用于存档，其宿主文件复制不计入角色工具预算；影响评测的装配和执行均已计入。

`summary.json` 现在额外给出 `final_build_status`、`game_builds` 和 `roles_not_invoked`，便于区分“已导出工程”和“构建通过、角色已执行”。若 Player 因构建失败被跳过，就没有新的真实试玩/重放证据；QA 角色 completed 也不代表游戏 ready。实际案例见 [002 运行诊断与复跑说明](fixed_loop_real_images_002_diagnosis.md)。

若只有一轮，或预算在下一轮装配前耗尽，交付副本可能尚无 `demo_outputs/`；汇总和 QA 会如实保留这个缺口。开发/试玩成功和游戏满足全部 instruction 是不同的结论。

## 可选的 Developer MCP 自检

`--godot-mcp` 启用 `developer.v4`，把真实场景检查、预览截图和看图模型反馈接入原公共执行器；默认路径仍为 v3。固定角色顺序不变，MCP 检查与视觉调用计入 Developer/总预算。Player 复用诊断提示，但正式采集和确定性重放保持独立；QA 无引擎执行权限。需额外构建 MCP 镜像，详见 [Godot MCP 安装、约束与结果目录](godot_mcp.md)。

已有镜像准备好后，真实模型与真实图片运行：

```sh
python examples/fixed_loop_demo.py --godot-mcp --output runs/fixed-loop-mcp-thinking-001 --max-cycles 3 --max-model-calls 64 --max-wall-seconds 43200
```

增加 `--mock-images` 可保留图片 mock。建议先使用 `--godot-mcp --preflight-only` 和新输出目录验证镜像与模型配置；预检不调用远端模型。更多 Developer 调用额度用于检查和反馈，不保证三轮能通过验收。正式调度对比需同时冻结两组的 MCP 配置和预算。

当前 `.env` 已采用更大的思考输出与输入额度：`<ROLE>_MAX_PROMPT_BYTES` 控制本地提示词字节上限，`FIXED_LOOP_ROLE_MAX_INPUT_TOKENS` 控制每次角色的累计输入预算，`FIXED_LOOP_MAX_INPUT_TOKENS` / `FIXED_LOOP_MAX_OUTPUT_TOKENS` 控制整次循环预算。角色输出额度由调用次数与 `MAX_OUTPUT_TOKENS` 自动汇总。完整数值、模型窗口与这些上限的区别，以及思考实验的对比方法见 [模型配置与切换记录](model_configuration.md)。所有最终生效的配置都会冻结到本次运行的 `experiment.json`。

## 在执行服务器运行

在项目根目录安装环境。使用支持该项目的 Python（3.10 及以上）、可用的 Docker，以及已配置好的 `.env`。真实模型配置沿用已有角色前缀，不需要修改脚本。

```sh
python -m pip install -e ".[live,test]"
docker build -t gamecraft-godot:4.5.1 configs/godot
docker build -t gamecraft-player:4.5.1 configs/player
```

镜像构建命令见相应 Dockerfile/角色文档；运行时不下载镜像，不回退到宿主执行生成代码。记录并使用镜像内容 ID，避免同名标签变更导致同一轮运行中引擎漂移。

### 生图配置

先更新依赖，确保安装 DashScope SDK（当前适配要求 `dashscope>=1.25.17,<2`）：

```sh
python -m pip install -e ".[live,test]"
```

已有 `.env` 包含阿里云兼容模式地址、密钥和图片模型时，无须修改入口。以下使用用户当前选择的 `wan2.7-image` 举例：

```dotenv
MODEL_GATEWAY_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
MODEL_GATEWAY_API_KEY=your-gateway-key
ART_IMAGE_MODEL=wan2.7-image
# Optional: dashscope is already the default.
ART_IMAGE_PROVIDER=dashscope
```

图片适配器使用 `dashscope.AioMultiModalConversation.call`，这是原生 `MultiModalConversation.call` 的可取消异步 SDK 形式，调用同步生图端点 `/api/v1/services/aigc/multimodal-generation/generation`。它不是 Chat Completions，也不创建需要轮询的后台任务。适用于当前 `wan2.7-image` 及支持该同步端点的 Qwen Image 模型。服务返回 `output.choices[].message.content[].image` 后，宿主下载原图，再进行已有 PNG 处理。

图片地址优先级为 `ART_IMAGE_BASE_URL` → `DASHSCOPE_BASE_HTTP_API_URL` → 从 `MODEL_GATEWAY_BASE_URL` 推导。推导时只把阿里云域名的 `/compatible-mode/v1` 改为 `/api/v1`，保留原地域/业务空间域名，**不改写文本地址或 `.env`**。北京、新加坡和带业务空间前缀的域名均保留各自的主机名。没有任何地址配置时使用北京原生地址；自定义或非阿里云 Chat 网关必须单独设置图片地址。

图片密钥优先级为 `ART_IMAGE_API_KEY` → `DASHSCOPE_API_KEY` → `MODEL_GATEWAY_API_KEY`。使用独立图片服务时：

```dotenv
ART_IMAGE_BASE_URL=https://dashscope.aliyuncs.com/api/v1
ART_IMAGE_API_KEY=your-image-key
```

地址必须是对应地域的 API 基础地址，以 `/api/v1` 结束，不要填写完整的 `/services/...` 路径。进程环境变量优先于 `.env`。SDK 每次调用显式传入 `base_address`、`api_key` 和 `request_timeout`，不修改 `dashscope.base_http_api_url` 或 `dashscope.api_key` 全局变量。预检会检查配置及 SDK 是否可导入，再允许派发文本角色。

| 可选配置 | 默认值 | 用途 |
| --- | --- | --- |
| `ART_IMAGE_TIMEOUT_SECONDS` | `600` | 每张图的整个请求与下载期限；仍受公共执行器剩余预算约束 |
| `ART_IMAGE_SIZE` | `1024*1024` | 原生 `宽*高`；也接受 `宽x高` 并转换，或模型支持的 `1K`/`2K`/`4K` 档位；不支持 `auto` |
| `ART_IMAGE_PROMPT_EXTEND` | 不发送 | 仅支持该参数的模型可设 `true`/`false`；当前 Wan 2.7 应保持不设置 |
| `ART_IMAGE_WATERMARK` | `false` | 原生水印参数 |
| `ART_IMAGE_SEED` | 不发送 | 可选 0–2147483647，保存于实验配置；不保证像素级重现 |
| `ART_IMAGE_TRANSPARENCY` | `chroma_key` | 原生模式使用纯洋红背景与宿主去背景，不发送 OpenAI 的 `background`/`output_format` |
| `ART_IMAGE_CHROMA_TOLERANCE` | `40` | 色键模式对 RGB `(255, 0, 255)` 每个通道的容差，允许 0–128 |
| `ART_IMAGE_RESAMPLE` | `nearest` | 原图缩放至 Art Producer 请求的精确尺寸，可设 `lanczos` |
| `ART_IMAGE_LICENSE` | 说明适用服务商条款、未经独立核验 | 可填写已确认的生成产物使用条款；不会自动声称 CC0 |

每个素材只提交一次 SDK 生图请求，固定 `n=1`、`stream=false`，不自动换模型、重试或额外调用修图接口。请求结构为 `model + input.messages + parameters`，英文提示放在消息的 `content` 中。只接受一张结果图；后台 task ID、多张图片或文本替代图片均视为失败。超时或取消会结束当前 SDK 协程，不使用可能在后台继续执行请求的线程包装。

原图可能是 PNG、JPEG 或 WebP，宿主保存原始字节后，转换并缩放为游戏需要的 PNG；不会把小尺寸精灵的 `32x32` 等规格直接当作模型必须支持的生成分辨率。整幅图按目标宽高缩放，可能影响比例，精灵表的帧布局与视觉质量仍需要检查。设置适合素材比例的生成尺寸可以减少变形。

默认色键模式用于没有原生透明通道的模型：提示词要求背景为 `#FF00FF`、主体不使用该颜色，宿主在缩放前移除匹配色。模型没有遵循背景要求时会报错，原图仍保存在调用目录供检查；不会只添加全不透明的 alpha 通道就宣称透明处理完成。边缘质量、色键误删及实际帧布局不由格式检查保证。

此前的 OpenAI Images 适配器仍可显式选择：`ART_IMAGE_PROVIDER=openai`。只有这种模式才接受 `ART_IMAGE_RESPONSE_FORMAT`、`ART_IMAGE_QUALITY`、`ART_IMAGE_TRANSPARENCY=native`，并调用 `/images/generations`。DashScope 模式若残留这些配置会在预检时报错，需注释或移除相应项；不会忽略后继续发送错误参数。文本角色不受这个图片开关影响。

接口依据：[阿里云 Wan 图像生成与编辑 API](https://help.aliyun.com/zh/model-studio/wan-image-generation-and-editing-api-reference)、[Qwen Image API](https://help.aliyun.com/zh/model-studio/qwen-image-api)。具体模型及地域的在线可用性仍由用户执行验证。

图片下载使用独立的无鉴权客户端，不转发模型网关的 Bearer 密钥。下载链接必须为 HTTPS，限制重定向、字节数和解码像素数；签名 URL 和网关凭据不会写进公开日志。生成服务运行在宿主，生成的游戏仍在断网容器中执行。

两个素材库接口保持可用。服务器上如已有库，可在 `.env` 设置：

```dotenv
ART_KENNEY_ROOT=/path/to/kenney
ART_OGA_ROOT=/path/to/opengameart
ART_LIBRARY_SNAPSHOT=my-frozen-library-snapshot
```

库路径必须与 Benchmark 和执行目录隔离。缺少库会明确报告不可用；任务允许的自生成路径使用上述图片服务。Art Producer 仍可选择库中的真实资源，instruction 对美术来源的规定保持不变。

先预检。预检校验配置、引擎镜像与素材库可用性，不调用模型、不访问生图接口，也不执行游戏；预检通过不代表在线接口已经可用。输出目录必须是新目录：

```sh
python examples/fixed_loop_demo.py --output runs/fixed-loop-preflight-server-001 --preflight-only
```

运行默认的真实 Benchmark `platformer-time-loop/instruction.md`：

```sh
python examples/fixed_loop_demo.py --output runs/fixed-loop-server-001 --max-cycles 3 --max-model-calls 32 --max-wall-seconds 10800
```

也可明确指定任务及 `.env`：

```sh
python examples/fixed_loop_demo.py --output runs/fixed-loop-server-002 --task-dir benchmark_resources/FreedomIntelligence-gamecraft-bench-a433475/tasks/platformer-time-loop --env-file .env --max-cycles 3 --max-model-calls 32
```

未安装为可编辑包时，PowerShell 可先执行 `$env:PYTHONPATH='src'`；Linux 可先执行 `export PYTHONPATH=src`。

默认总上限还包括文本输入 15,000,000 token、文本输出 1,000,000 token、生图 32 次和工具 20,000 次。真实生图消耗图片服务额度；失败请求也计入 `image_calls`，下载与本地处理计入工具时间。若图片 API 返回自己的 usage，该字段保留在原始响应中，不混作文本 token 账目或已确认费用。各角色单次上限、图片与文本模型参数、代码版本哈希和引擎 ID 写入 `experiment.json`。Art 单次时间上限考虑逐张生图耗时，但不能突破总运行期限。token 预留是保守上界，剩余额度不足以预留下一次完整请求时，会在派发前停止，即使已用额度尚未恰好达到上限。接口不隐式重试；调用内的协议修复和游戏修复由既有角色控制并计费。

进程退出码：`0` 表示预检成功或游戏满足内部完成条件；`2` 表示有结果但因轮数、预算、停滞等原因结束，尚未就绪；`1` 表示技术失败；`130` 表示用户中断。真实生图不保证一轮运行即可就绪。

仅需复现之前的图片占位图流程时，明确加上开关：

```sh
python examples/fixed_loop_demo.py --output runs/fixed-loop-mock-images-001 --mock-images
```

该开关只替换图片服务，文本模型和 Docker 试玩仍然真实运行；不是完全离线测试。

CLI 不覆盖旧目录，也不自动恢复付费运行。重复运行请换新目录。控制器内部支持依据权威账本核对已完成槽位，执行器支持恢复中断调用；手动通过 Python API 重开执行器时仍必须保留原配置、服务及模型捕获路径。异常恢复不会重放已计费调用，未知 token 消耗会阻止继续。正式的跨进程 CLI 恢复管理留待单独实现。

## 结果目录与审阅

```text
runs/fixed-loop-server-001/
  instruction.md
  experiment.json
  review.md
  summary.json
  state_final.json
  image_calls.json
  image_calls/<call>/             # 真实生图；显式 mock 模式只有上面的索引
    job.json                     # Art Producer 请求的尺寸、帧和透明要求
    request.json                 # 原生 SDK 对应的请求体 model/input/parameters，不含凭据或请求头
    response.json                # SDK 响应，保留 status_code/request_id/output/usage；凭据和图片 URL 脱敏
    source.png                   # 或 source.jpg/source.webp；模型返回的原始图像
    output.png                   # 最终交给 Art Producer 的 PNG
    metadata.json                # 状态、时间、模型、转换步骤和前后哈希
  model_calls/<role>/<call>/
    request.json
    response.json
    response.txt
    metadata.json
  model_call_index/<request_id>.json
  implementation_snapshot/        # 与实现哈希对应的 Python 源码，便于审阅
  workspace/public/                 # ArtifactRef.path 的根目录
  workspace/.private/state.json     # 权威状态
  workspace/.private/history/       # 每次账本更新
  workspace/.private/tools.jsonl
  workspace/.private/controller/
    checkpoint.json                # 调用和跳过原因
    calls/<request_id>/
      request.json
      state_before.json
      result.json
      state_after.json
  delivery/
    manifest.json
    game/                          # 当前已提交项目的原样副本
```

模型或宿主失败时可能没有完整 `response.json` 或 `output.png`。SDK 成功返回的错误响应保存在 `response.json`；SDK 传输/解析异常会记录安全错误类型。显式 OpenAI 图片模式遇到非 JSON 返回时另存脱敏的 `response.txt`。`metadata.json`、权威账本和 `failure.json`/`export_failure.json` 会保留已经发生的部分。正常结束及可捕获的中断都会写 `image_calls.json`；进程被强制终止时，可直接检查已落盘的逐次调用目录。`.env` 不复制到输出目录；隐藏 rubric、测试和参考解答不导入生成工作区。

你运行完后，保留整个 `runs/fixed-loop-server-001/`，告诉我路径即可。我可以从 `review.md` 和 `summary.json` 开始，定位具体轮次及模型输入/输出，核对失败构建、真实截图、输入轨迹和 QA 问题，而不需要重新跑模型。

## 测试边界

```sh
python -m pytest tests/test_dashscope_image.py tests/test_image_provider.py tests/test_fixed_loop_images.py tests/test_fixed_loop.py -q
python -m pytest -m "not live" -q
```

明确启用付费端到端测试（会调用真实文本与生图模型、创建新运行，不读取历史结果冒充执行）：

```powershell
$env:GAMECRAFT_FIXED_LOOP_LIVE_TESTS='1'
$env:GAMECRAFT_FIXED_LOOP_LIVE_OUTPUT='runs/fixed-loop-live-test-001'
python -m pytest tests/test_fixed_loop_live.py -q -s
```

该测试要求真实运行至少到达轨迹装配。模型失败或预算不足时断言可能失败，仍保留完整目录供分析，不会改用假输出让测试通过。

固定 Loop 的离线端到端测试直接读取 Benchmark 原始 instruction，使用五个真实角色实现和公共执行器；文本模型与渲染边界使用明确标注的测试夹具。这些测试用于验证交接、权限、版本和计费，不作为真实游戏质量结果。

本次 DashScope 适配测试实际执行已安装的原生 SDK，在 HTTP 发送边界拦截网络，核对原生地址、请求体、尺寸格式、密钥隔离与取消行为；同时回归图片处理、计费和 Chat 适配器。未访问用户网关。这些测试验证代码边界，不证明具体网关或模型已经在线可用。

真实入口始终调用 `.env` 文本模型及 Docker Godot；没有离线文本回复或假引擎的自动降级。历史 DeepSeek + mock 生图的验证结果见 [实测与回归记录](fixed_loop_review.md)：已验证真实轨迹装配及新版本重放，当时完整 QA 因调用超时未完成。历史目录保持原样，不代表本次真实生图配置的运行结果。
