# Art Producer 开发说明

正式 Loop / Scheduler 入口及独立 Art 示例已启用 [素材规范化与切图](asset_normalization.md)。光栅素材需要显式 layout，原图保留作来源证据，宿主发布逐帧 PNG 和联系表。底层默认配置仍兼容旧清单；正式入口增加了 [库优先和本轮质量反馈修复](library_first_and_grounding_repair.md)。

[人物精灵质量规则](sprite_quality.md) 会对生成图和素材库人物帧执行共同检查；疑似地面、文字、面板或复杂背景会被隔离。正式入口将发现和原生成请求回送本轮规划模型，最多再修复 2 轮，仅处理失败素材；仍失败才保留为 partial 交给后续调用。生图提示词区分单张精灵、动画序列、环境与 UI。

Art Producer 已接入公共执行器，为固定 Loop 与 Scheduler 使用同一套角色、提示词、权限和预算逻辑。实现位于 `src/adaptive_game_generation/roles/art_producer/`。

当前正式入口配置 `max_model_turns=8`、`max_search_turns=3`、`max_quality_repair_rounds=2`。搜索、提交、字段纠错和质量反馈共享 8 次文本调用，所有图片尝试计入原有总预算。底层角色默认仍为 4 次规划、0 次质量反馈修复，以兼容历史单轮使用；独立运行示例已显式采用正式配置。003 的旧修复背景见 [003 诊断](fixed_loop_real_images_003_diagnosis.md)。

角色最初开发时使用真实 GameCraft-Bench `platformer-time-loop/instruction.md` 和此前真实 Designer 调用的设计产物作为输入，Art 规划调用 `.env` 中的真实文本模型，只有图片生成使用 mock；历史完整输入输出见 [审阅记录](examples/art_producer_review/review.md)。本次库优先和反馈修复的验证使用保存的真实数据与离线夹具，没有新的在线调用。原始 instruction 保持不变。

## 职责和产物

角色读取原始 instruction、需求、Designer 文档、现有资产清单、指定问题和公开附件。文本模型通过有限轮次检索素材并提交结构化计划；执行层校验整个计划后，才导入文件或调用生图服务。

新调用默认使用 `art-producer.v4`，增加以下协议修复能力：

- 检索、提交和纠错共用 `max_model_turns`（默认 4），每次文本调用仍通过公共执行器计入预算。错误反馈包含字段位置、网格/尺寸约束和素材来源检查结果；已有检索结果保留。模型必须返回完整替代方案，宿主不会猜测、补齐或静默删除错误字段。
- 下一次提示包含 `proposal_rejections` 和上次无效回复的至多 8 KiB 摘录；超长枚举不会全部复制回提示。原始回复完整保留在模型调用目录。成功发布的 `asset_manifest.json` 也记录纠错原因；耗尽次数时，运行错误保留具体校验原因。
- 规则网格仍要求尺寸整除、逐格唯一帧名且最多 64 格。正式入口遇到大型或不规则库图集，应使用实际返回的 catalog 帧名或有依据的 regions；布局未知时声明 unresolved，保留为不可直接显示的源图。不能用单格声明把整张图集当成人物，也不能凭文件名编造动画或按钮状态。优先继续检索独立图片。
- 素材来源必须与实际检索结果匹配，库文件和许可证原文保持不变。整个方案通过校验后才执行导入/生图。预算、权限、提供商或生图失败不会触发这套方案纠错；角色调用最终失败后，仍遵循控制器原有规则。

显式指定 v2/v3 时保留原先的方案校验失败即退出行为。新旧角色配置在各自运行中冻结，不在已有清单上切换版本。修复背景和原始证据见 [真实图片运行诊断](fixed_loop_real_images_001_diagnosis.md)。

输出包含：

- `assets/`：所选素材的原始字节，或生图服务返回的 PNG。
- `licenses/`：素材库所选文件的许可证原文文本，不由模型编造。
- `asset_manifest.json`：完整版本清单，记录来源、文件引用和哈希、尺寸、透明通道、逐格帧名、过滤方式、建议接入路径、需求引用、影响场景、许可证与生图请求。
- `art_handoff.md`：供 Developer 审阅的风格与接入说明。

开启本轮质量修复后，原图、许可证、逐帧 PNG 和预览按 `art_attempts/001/`、`002/`、`003/` 分开保存，避免覆盖失败图片或让引用摘要失效。清单的 `quality_attempts` 保存全部候选和质量结果，`search_observations` 保存实际查库记录；最终 `assets` 只指向每个素材的最终候选。Developer 游戏内的稳定目标路径仍为 `assets/normalized/<asset_id>/<frame>.png`，不会打包失败原图。

实际产物均在执行器的 Art Producer 独立命名空间内发布；`assets/...` 是交给 Developer 的建议项目路径。Art Producer 不修改 Godot 游戏工程，不生成脚本、场景、验收证据或需求进度，不关闭 QA 问题。

模型输入、协议、回复和产物内容使用英文。不会自动翻译原始 instruction 或许可证；宿主应提供英文目标和反馈。二进制图片附件只提供引用，并标记 `visually_inspected=false`，没有宣称文本模型看过图片。

## 模型协议

当前协议为 `art-producer.v4`，历史 `v2/v3` 产物仍可读取。输出 schema 显式标注 `target_path` 必须以 `assets/` 开头；原有目录权限检查保持严格。底层默认 4 次、正式入口 8 次文本规划，角色内部检索不涉及调度其他角色：

1. `action=search`：每轮最多检索两个素材库，传入库名、关键词和分页 offset。下一轮获得真实检索结果及许可证。
2. `action=submit`：返回 `completed`、`no_op` 或 `blocked`，提交资产增量计划或明确阻塞原因。
3. 执行层校验 JSON、字段、需求 ID、候选来源、目标路径、尺寸、帧网格、数量和生图可用性。重复 JSON 键、未知字段和非有限数值均拒绝。不自动修复回复或重试 API。
4. 使用受限工具导入/生成，检查文件格式、尺寸和哈希，提交 `ProposedUpdates.assets`；仍由公共执行器原子发布并记账。

初次调用不能 `no_op`。缺少 Designer 规格时直接 `blocked`，不花费模型调用。增量修订复用资产 ID、增加产物版本，保留未修改资产；新资产不能占用已有资产的目标路径。角色配置随清单保存，同一资产历史中不可悄悄更换配置。

只要当前完整清单中仍存在 mock 图片，或归一化结果为 rejected/unresolved，角色结果就是 **`partial`**。这表示管线可以提交可检查的候选产物，同时正式美术仍待完成。素材全部通过处理后可返回 `completed`，也不表示游戏已经通过 QA。所有资产的 `visual_review` 目前均为 `not_performed`。

## 两个预下载素材库接口

`runtime/asset_libraries.py` 提供 `AssetLibraryProvider` 协议和 `MountedAssetLibraries` 实现。Kenney、OpenGameArt 使用不同库 ID，独立配置和调用：

| 接口 | 用途 |
| --- | --- |
| `availability()` | 返回两库是否挂载、格式支持与许可证策略，不暴露宿主绝对路径 |
| `search("kenney", query, offset=0, limit=8)` | 检索 Kenney 的相对路径/文件名 |
| `search("opengameart", query, offset=0, limit=8)` | 检索 OpenGameArt 的相对路径/文件名 |
| `read(candidate, max_bytes=...)` | 按检索凭据读取文件，重新核对资产和许可证，拒绝搜索后发生的变化 |

角色只能经由 `RoleTools.asset_sources()`、`search_assets()`、`import_asset()` 使用接口。每次调用进入共同权限、工具次数、耗时、超时和审计记录；其他角色与 Scheduler 没有素材库工具权限。宿主配置路径，模型不能指定外部目录。

默认读取 instruction 中的目标服务器路径：

```text
kenney       /workspace/assets/library/
opengameart  /workspace/assets/library-oga/
```

库不存在时正常返回不可用，不下载、不伪造目录、不把隐藏 Benchmark 资料作为素材来源。模型仍可选择另一个可用库，或在任务与宿主允许时选择生成；缺少必要来源时返回阻塞。

当前读取已解压的 PNG、JPEG、WebP、TTF、OTF、OGG、WAV。PNG/JPEG/WebP 进行格式、尺寸和完整性检查；字体、音频保留字节并校验哈希，其引擎加载需后续 Developer 验证。不会解压压缩包或读取 SVG、脚本和场景文件。检索是文件名/路径关键词 OR 匹配，空关键词表示浏览，不是图像语义搜索。每次扫描默认最多 20,000 个目录项，返回最多 20 个候选；扫描截断明确报告，不能据此声称全库为空。较大素材库可调整宿主限额，或替换为实现同一协议的索引服务。

有具体对象词时，`sprite/assets/png` 等通用词不再使所有同名目录都匹配；候选优先按文件名中的具体词排序，同等匹配时优先非图集/预览，分页顺序稳定。先扫描和排序路径，再读取、校验和散列候选。路径排序只是检索帮助，不代表图像已经过语义或透明度验收。

`library_first=true` 默认开启。生成前必须实际搜索每个可用库；生成请求的 `generation_fallback` 引用真实 `search_ids`，并解释返回候选为何不满足该素材。引用伪造搜索/路径、跳过可用库或没有候选评估时，宿主在花费图片调用前拒绝计划。若两个库都不存在，由宿主确认不可用后可直接生成，不伪造库存、不下载、不修改 instruction 的允许来源。候选是否满足审美/语义仍是模型判断，其理由完整留档供审阅。

许可证必须存在且非空，最长 32 KiB。Kenney 查找最近的 `LICENSE.txt`（兼容常见大小写及 `.md`）；OpenGameArt 必须找到**条目子目录中的 `LICENSE.txt`**，不会拿库根目录的说明替代条目许可证。无许可证的素材不进入可选清单。候选记录许可证来源和原文件哈希；发布的 `.txt` 保存解码后的原文，源文件若有 UTF-8 BOM，导出的文本不保留 BOM，所以原许可证哈希与导出文件哈希分别记录。模型的 `license_handling` 是接入建议，不替代原始条款，也不自动保证许可证解释正确。

库路径拒绝链接、Windows junction/reparse point、硬链接、特殊文件、路径越界与私有目录。宿主应通过 `forbidden_roots` 声明完整 Benchmark 快照目录和运行工作区，拒绝重叠挂载。示例已如此配置。素材目录由宿主保持只读；适配器仅打开文件读取，没有写库或网络下载接口。

## 服务器迁移与配置

沿用 Python 项目，不限定游戏开发主机操作系统。安装项目依赖：

```text
python -m pip install -e ".[test,live]"
```

`Pillow>=10,<13` 用于栅格格式检查和测试占位 PNG。现有异步文本模型适配器新增 `prefix="ART_PLANNER"`，Designer 默认前缀保持不变：

```dotenv
ART_PLANNER_MODEL=deepseek-flash
ART_PLANNER_TIMEOUT_SECONDS=600
ART_PLANNER_MAX_OUTPUT_TOKENS=32768
ART_PLANNER_REASONING_EFFORT=low

# Optional overrides; omit to use the two server paths from the instruction.
ART_KENNEY_ROOT=/workspace/assets/library
ART_OGA_ROOT=/workspace/assets/library-oga
ART_LIBRARY_SNAPSHOT=assets-snapshot-001
```

凭据仍读取 `MODEL_GATEWAY_API_KEY` 和 `MODEL_GATEWAY_BASE_URL`。可单独配置 `ART_PLANNER_TOKEN_PARAMETER`、`ART_PLANNER_JSON_MODE`、`ART_PLANNER_STREAM` 和 `ART_PLANNER_INPUT_TOKEN_MARGIN`；进程环境覆盖 `.env`。接口不在 import 时读凭据。Windows 路径可用 `E:/assets/kenney`；无需修改 instruction 或角色代码。

宿主接线示例：

```python
from adaptive_game_generation.providers import GatewayConfig
from adaptive_game_generation.roles import ArtProducer, ArtProducerConfig
from adaptive_game_generation.runtime import MountedAssetLibraries, Services

text_config = GatewayConfig.from_env(env_file, prefix="ART_PLANNER")
libraries = MountedAssetLibraries.from_env(
    env_file,
    forbidden_roots=(benchmark_root, workspace.root),
)
services = Services(
    text=text_provider,
    libraries=libraries,
    image=image_provider,
    image_info=image_service_info,
)
role = ArtProducer(ArtProducerConfig(max_output_tokens=text_config.max_output_tokens))
request = executor.request(
    "art_producer",
    "Create or revise the art package from the public task and design",
    ("Traceable sources and licenses", "Precise dimensions and Developer integration notes"),
)
result = await executor.execute(request, role)
```

以上服务应在创建/打开 executor 时注入。模型、预算、素材库快照和生图服务须在两组实验间保持一致。`libraries.configuration_id` 是路径、限额与宿主快照标识的摘要；它不是全库内容扫描。正式实验需要宿主固定只读库快照，本实现另记录实际选中文件及许可证的哈希。将库配置摘要与文本/图像服务标识一起写入 `RuntimeConfig.service_configuration_id`；示例展示了组合方式。

## 生图 mock 与真实服务预留

现有 `ImageProvider.generate(prompt: str, *, timeout: float) -> bytes` 接口继续使用。Art 传入的 `prompt` 是 `art-image.v1` JSON，可用 `ImageJob.model_validate_json(prompt)` 解码，包含英文提示词、目标尺寸、透明背景、行列和帧名。真实生图适配器应把这些字段映射到服务请求，返回符合尺寸的 PNG 字节，并遵守超时/取消；不能仅将整个 JSON 当作自然语言提示词而忽略尺寸等参数。

单角色示例使用的 `MockImageProvider` 无网络调用，返回确定性 RGBA 网格占位图，足够大的单元格印有 `MOCK`，不声称生成了模型描述的人物或场景。`ImageServiceInfo` 由宿主提供服务 ID、`is_mock` 和许可说明，模型不能覆盖这些字段。

Fixed Loop 默认注入 `DashScopeImageProvider`，使用原生 `AioMultiModalConversation.call` 读取 `.env` 的 `ART_IMAGE_MODEL` 生图；Art Planner 的文本调用方式保持不变。服务身份为 `is_mock=false`。宿主解析原生响应中的图片 URL，保存原图与处理记录，转换为指定尺寸的 PNG，并验证请求的透明区域；仍通过公共 `generate_image` 工具计入预算。旧 `OpenAICompatibleImageProvider` 仅在显式选择 `ART_IMAGE_PROVIDER=openai` 时使用。具体接口、透明模式和独立图片配置见 [Fixed Loop 开发与运行说明](fixed_loop.md#生图配置)。本次适配通过真实 SDK 加离线 HTTP 响应测试，未在线验证用户图片网关。已有单角色示例和历史 mock 审阅产物保持原样。

每次 mock 调用仍计入 `image_calls`，用于验证公共预算路径，**不代表发生了收费生图**。模型 token 使用真实响应 usage；失败调用也保留费用/保守预留记录。图片尺寸或格式错误、配额耗尽、导入失败会使本次产物不发布，已有共享状态不变。

## 测试与审阅

```text
python -m pytest -q
```

离线测试包含双控制器输入一致性、两个真实文件系统素材库接口、原文许可导出、搜索后来源变化、私有路径及链接拒绝、帧布局和路径冲突、资产版本保留、问题反馈、预算、模型/图片失败、字段缺失与完整提交。离线错误分支使用脚本化文本回复；不会把这些夹具冒充真实集成结果。

`tests/test_art_producer_repair.py` 逐字节回放 `fixed-loop-real-images-001` 的真实错误回复，验证网格错误、截断 JSON、来源与尺寸错误的反馈、纠错预算、历史版本行为及整图集导入。测试中的修正回复和库图片明确为离线夹具。`tests/test_fixed_loop.py` 另验证 Art 在同一轮内纠正方案后可进入 Developer、Player 和 QA；这不是新的在线运行结果。

单独运行真实规划测试（PowerShell）：

```powershell
$env:GAMECRAFT_ART_LIVE_TESTS='1'
$env:GAMECRAFT_ART_LIVE_OUTPUT='runs/art-producer-live-new'
python -m pytest -q -s tests/test_art_producer_live.py
```

Linux 可设置同名环境变量，或直接运行：

```text
python examples/art_producer_demo.py --output runs/art-producer-live-new
```

输出目录必须是新目录。测试复用已保存的真实 Designer 产物作为前置状态，校验其哈希和需求；导入动作记入执行日志，不伪造新的 Designer 模型调用。当前两个资产库未挂载，因此本次真实 Art 模型自主选择生成路径。实际库挂载通过两组临时小文件夹测试了读取和许可证流程，尚未宣称测试了服务器上的完整素材库。

真实尝试记录：

| 运行 | 结果 |
| --- | --- |
| `runs/art-producer-live-001` | 模型完成回复，但缺失 `license_handling` 并输出额外字段。提交被拒绝，生图 0 次，原始回复和用量保留 |
| `runs/art-producer-live-002` | `art-producer.v2` 明确必填字段后通过：1 次真实文本调用、12 次 mock 图片调用、12 项资产；角色 `partial`，集成检查通过 |

成功调用报告输入 19,720 token（其中缓存 18,048）、输出 6,877 token。完整模型请求、原始回复、图片请求、PNG、清单、状态与账目均见 [审阅记录](examples/art_producer_review/review.md)。Art Producer 本步骤不生成游戏或执行正式评测。后续已实现 [Developer](developer.md)，读取原始 instruction、Designer 规格及本 Art 清单接入工程；其独立测试见 [Developer 审阅记录](examples/developer_review/review.md)。

最终离线回归为 **194 passed / 4 skipped**，Art 真实模型集成另有 **1 passed**。收尾时还修复了公共导入器在 Windows 上自动转换 instruction 换行的问题，新增 LF/CRLF 原始字节保留测试；历史真实调用记录不回写，细节见 [审阅备注](examples/art_producer_review/reviewer_notes.md)。

## 参考图、动作合同与按需技能

正式 Fixed Loop / Scheduler 入口启用 `require_actor_motion` 与 `progressive_skills`；旧单角色配置仍可显式关闭。`reference_asset_ids` 由宿主解析为公开、哈希校验过的图像，支持时送入原生 DashScope SDK；不支持时拒绝引用请求。一次生成仍是一笔预算，部分成功的批次在传输失败后保留已完成素材，`production_failures` 明确列出待办。

`motion` 区分 frames/procedural/static，声明显示高度和状态帧；跨库独立 PNG 也能组成同一角色的动画。新增三个固定技能 asset_layout、actor_motion、reference_edit，模型可按需加载，预算和工具权限不扩大。详情与英文协议例子见 [library-first-002 诊断](fixed_loop_library_first_002_diagnosis.md)。
