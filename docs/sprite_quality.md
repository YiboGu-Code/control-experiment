# 人物精灵处理与质量检查

Fixed Loop 和 Scheduler 共用公共执行器及 `asset-pixels.v3`，正式入口默认启用。聊天接口、Benchmark instruction、角色顺序和确定性重放协议不变；没有在线测试，也没有改 `.env`。

## 修复内容

v2 修复了整张图非等比拉伸，但等比缩成小帧仍会丢细节。当前 v3 保留生成接口返回的原始分辨率，按照声明布局映射原生切图坐标，不再缩小、放大或独立裁边重采样。PNG 是无损编码，逻辑显示大小单独记录为 `frames[].display_size`，Developer 用统一宽高缩放系数显示。详见 [原分辨率保留与生图内容约束](sprite_fidelity.md)。

去底后直接进行原分辨率检查。问题随候选 PNG 以有界元数据传递；它只能增加拒绝原因，不能跳过最终像素检查。`image_calls/NNN/metadata.json` 的 `normalization` 记录原生/逻辑尺寸、精确帧区域、`resampled=false`、`encoding=lossless_png`；去底之外的像素不因存储和切图改变。旧 v2 输出目录保留为历史证据，不代表当前处理结果。

紫红色处理有两种方式：

- 真实生成图片：请求不使用紫红色作为主体颜色，去除指定底色，并在边界大部分为一致紫红色时识别轻微色偏；处理底色紧邻像素的色边。原始图片保留。
- 素材库人物帧：已有有效 alpha 时原样保留；否则要求大部分边界可靠地指向紫红色，仅去除与边界连通的底色。封闭在人物内部的紫色细节保留。无法可靠推断的白底、复杂背景不会被强行抠除。

边缘处理是有界的色键近似，不是通用分割或精确的前景颜色恢复。对于半透明发光、同色服装、镂空区等，仍需看实际效果。`ART_IMAGE_TRANSPARENCY=native` 保留原生透明色彩；DashScope 仍使用原生 SDK 加 chroma_key。

## 质量门槛与交接

`sprite`、`sprite_sheet` 检查空帧、几乎全不透明、矩形前景面板、大片底座、跨单元边界、多个疑似字形碎片、独立宽条组件和去底后的紫红残留。检查单张前景组件，避免人物卡片加一行文字使全图占比降低而漏过检查。

这些是保守的几何检查，不是 OCR 或语义分割。它们可能误拒绝长袍、方形机器人、独立武器或粒子，也无法识别所有连在人物上的文字、地面和背景。报告始终标记 `semantic_review=not_performed`。`checks_passed` 只表示没触发结构问题，最终还要由 Developer 的 Godot 预览与 QA 看真实画面和需求。不会把“自动去底”描述成“人物语义分割已成功”。

| 状态 | 游戏可用帧 | 后续行为 |
| --- | --- | --- |
| materialized / checks_passed | 有 | 可装配，仍待视觉验收 |
| materialized / not_applicable | 有 | UI、字体、背景、瓦片沿用自己的用途，不套人物规则 |
| rejected | 无 | 保存原图、原因和诊断预览；Art partial，要求替换或修正 |
| unresolved | 无 | 等待可靠布局或替代素材 |
| materialized / mock_not_reviewed | 有占位图 | 仅宿主真实 mock 输出可获此状态；Art/QA 不把它当最终美术 |

两个控制器使用同一个 WorkPolicy，让 rejected / unresolved 继续成为 Art 的待办。不能用 no_op 清除。Developer 只得到通过处理的帧，不复制被拒绝的原图或预览，改名原图也会被包装检查阻止；更新素材时删除上一版过期帧。被拒绝的素材仍存在时，Developer partial，QA 的 final_art=false。

不自动通过新生图调用修复图片，避免隐藏花费。下一次 Art 调用从上次清单的 `normalization.quality.findings` 读取问题，在原有模型、图片及工具预算内重新选择素材或生成。文件读取、质量处理、写入沿用公共工具审计和限额；模型不能自报 mock 来绕过检查。原图、许可证、隐藏评测隔离和重放约束保持原有规则。

## 生图提示词与按需能力

新增 `providers/image_prompts.py`，宿主根据 `layout.kind` 与帧数选择单张精灵、精灵序列、环境或 UI 的英文能力说明，再组合该素材请求与必要的布局规则。单张素材只画一个指定对象和状态，不画动画合集；门、开关等对象禁止附带示范人物。序列要求同一主体、统一镜头和比例；人物运动靠姿势表达。精灵均禁止地面、平台、面板、文字和示例场景。显式切图区域与留白仍按请求保留。Art 的规划规则建议单姿态或 2–4 帧的小图集。背景素材仍允许画场景，不会被人物规则误约束。

这一拆分来自 [developer-opt-001 的真实运行诊断](fixed_loop_developer_opt_001_diagnosis.md)：旧模板将单扇门也描述为“角色动画”，与素材请求矛盾。约束改善不能保证模型服从；原图检查、去底、切图、隔离不合格素材与最终视觉验收仍必须执行。

这属于项目内部按需提示词模块，不是让模型读取任意 SKILL.md 或执行动态脚本；没有新增模型调用。提示词可以减少错误源图，但效果需要后续在线运行验证，不能代替后处理与拒绝门槛。

关于所有角色改成 skill 的设计判断：方向可行，建议固定简短的角色职责、原始任务权威性、权限和预算规则，再由宿主根据阶段选择受版本控制的能力包。能力包应包括输入选择、专用说明、输出 schema、可用工具和验收条件。例如 Art 的素材搜索/生成规划/视觉复核，Developer 的实现/构建修复/画面修复；每次只加载该阶段需要的材料。单纯把长 prompt 拆成文件再全部拼回去，不会减少上下文。

本次只落实生图类型模块，未将五个角色整体改造成动态 skills。后续实现阶段加载时，应记录包 ID、版本、输入产物摘要和每次调用，Fixed Loop 与 Scheduler 使用相同包与预算；调度策略只决定调用哪个角色，不能给一方额外能力。模型可以提出功能需求，但由宿主白名单选择模块，不把任意路径、隐藏资料或“技能文本”当作扩权渠道。

## 离线证据与复现

```bash
python examples/sprite_quality_demo.py --output runs/sprite-quality-review-002/evidence
python -m pytest tests/test_sprite_quality.py tests/test_image_provider.py tests/test_dashscope_image.py tests/test_asset_normalization.py -q
```

脚本默认使用已保存的 `platformer-time-loop/instruction.md`，校验摘要，不读取 `.env`。真实源图来自 `fixed-loop-permissions-002` 的玩家场景图和幽灵卡片，快照、之前的输出、原始任务参数及来源在 `tests/fixtures/sprite_quality/`；OpenGameArt 字体和洞穴层继续使用已有快照和原许可证。

本次证据在 `runs/sprite-quality-review-001/`：

- `evidence/review.md`：真实图的处理前后对比与质量预览。
- `evidence/<case>/source.png`、`before.png`、`candidate_after.png`：原始模型图、旧输出、新候选图。**candidate_after 仍可能含面板或地面，拒绝的候选不能放入游戏。**
- `matte_removed_full_resolution.png`：全分辨率去底效果，便于确认去掉了什么、还剩什么。
- `processing.json`、`quality.json`、`current_image_prompt.txt`：完整变换、问题原因及当前生图提示词；并未发送提示词给在线模型。
- `contracts/<controller>/`：使用真实角色实现、脚本模型响应、已保存真实图片和脚本引擎的离线链路；保存完整输入输出、状态、账本及重放契约检查。不是实际游戏质量或真实引擎验收。
- 测试 XML 与总览：回归结果。

旧 `asset_normalization_demo.py` 也已适配质量拒绝：坏人物图不再发布帧，合法字形和场景区域仍保持原始 RGBA。此前“8 块精确裁切”的报告是历史结果，不代表现在还会接受坏人物图。

## 由你运行在线场景

素材库优先选择、同一次 Art 调用内的质量反馈修复与引用归一化已接入两个控制器，详见[素材库优先与引用修正](library_first_and_grounding_repair.md)。正式入口在初次产出后最多修复两轮，只重做失败素材，继续受共享预算限制；失败候选保留为诊断证据。

安装现有项目依赖后使用新输出目录，沿用 `.env` 中的聊天与 DashScope 图像配置：

```bash
python examples/fixed_loop_demo.py --output runs/fixed-loop-sprite-quality-001 --max-cycles 3 --max-model-calls 32 --godot-mcp
python examples/scheduler_demo.py --output runs/scheduler-sprite-quality-001 --max-decisions 30 --max-role-calls 15 --max-model-calls 32 --godot-mcp
```

`--godot-mcp` 使用项目已配置的隔离检查环境；不传 `--mock-images` 时调用真实生图。若不启用 MCP，两种入口仍执行相同的离线素材门槛。不要续跑旧检查点；新旧处理版本和提示词不能混作同一实验配置。运行后可直接检查上述输出目录的 `image_calls`、Art 清单、预览和最终交付报告。
