# 素材规范化与切图工具链

固定 Loop 和 Scheduler 的正式入口共用这套实现，不需要额外开关。Art Producer 声明布局，公共执行器生成独立帧，Developer 装配这些帧并检查整图误用。原始 instruction、素材库选择权、生图接口、试玩输入和确定性重放协议不变。

## 交接方式

1. Art 仍可搜索 Kenney / OpenGameArt 或自行生成图片。完整方案验证后才导入或生图，每张光栅图必须声明 `layout`。
2. 宿主保留原图、来源摘要、许可证，调用公共 `asset_normalize` 工具，生成逐帧 RGBA PNG、棋盘格联系表、坐标、枢轴、动画分组、时长和透明像素统计。
3. Developer 只装配 `assets/normalized/<asset_id>/<frame_name>.png` 和许可证；原图、联系表作为 Art 来源证据，不进入游戏。非光栅字体、音频维持原导入方式。
4. 代码仍引用原图时，宿主在构建前给出错误和可用帧路径，使用现有纠错预算修复。装配时删除上一版失效帧；发布前检查原图路径和改名后的已知图集副本。MCP 视觉助手可将联系表与游戏截图对照。

`AssetRecord.artifact_ref` 继续指向原图。可用帧在 `ProducedAsset.normalization.frames`；不要把来源引用当作游戏纹理。Developer 报告的 `asset_packages` 保留规范化信息，`integrated_assets` 每帧使用 `素材ID:帧名`。

生成图片从 `asset-pixels.v3` 起保留原生像素，当前为 `asset-pixels.v4`：在接口原图上去除指定的紫红色底，映射布局边界并逐帧裁取，保留原生分辨率和像素，使用无损 PNG 编码，不再缩到规划的小尺寸。`NormalizedFrame.display_size` 记录可选逻辑显示范围，Developer 统一缩放宽高；实际原生坐标、pivot 和动画时长保持完整。原始返回图、候选 PNG 和处理记录保留在 `image_calls/`。详见 [原分辨率保留与生图内容约束](sprite_fidelity.md)。

公共 `asset_normalize` 对生成 PNG 和素材库图片使用相同质量门槛。素材库帧尺寸、坐标和 pivot 不变：已有有效透明通道时保持像素；没有有效透明通道且边界可靠呈紫红色时，只去除与边界连通的底色，并处理紧邻底色的色边，不全局删除人物内部的紫色细节。UI、字体、背景、瓦片不套用人物轮廓规则。

具体行为、已知限制和前后对比见 [人物精灵处理与质量检查](sprite_quality.md)。请求布局和自动检查都不等于模型已经画对人物或动作。

## 布局协议

`layout.kind` 支持 `sprite`、`sprite_sheet`、`tile`、`tile_set`、`prop`、`background`、`ui`、`source_atlas`。

`asset-pixels.v4` 新增单块地砖 `tile` 和世界道具 `prop`，均可使用 `whole`。合法矩形地砖和道具底座不套用人物背景规则，空图和生成透明度失败仍隔离。库地砖保留颜色与透明度，不把本身为紫红色的地形当作人物底色删除；生成任务明确要求的去底仍由提供商处理并记录。显式布局任务的透明度失败进入 Art 的有限反馈修复，保留其他成功素材。详见 [真实运行诊断](fixed_loop_library_first_001_diagnosis.md)。

| mode | 用途及约束 |
| --- | --- |
| `whole` | 真正独立的精灵、背景或 UI；必须 1×1、一个帧名；图集类型不允许 |
| `grid` | 单元尺寸、左/上边距、横/纵间距、行列数；帧名按行优先 |
| `regions` | 显式矩形，可只选部分帧；frame_names 与矩形顺序一致 |
| `catalog` | 选取素材库元数据返回的准确帧名，宿主解析坐标 |
| `unresolved` | 无可靠布局的来源图集；不产生显示帧，保持待处理 |

规则网格示例（英文模型输出片段）：

```json
{
  "width": 130, "height": 34, "columns": 4, "rows": 1,
  "frame_names": ["idle", "run", "jump", "fall"],
  "layout": {
    "kind": "sprite_sheet", "mode": "grid",
    "cell_width": 32, "cell_height": 32,
    "margin_x": 1, "margin_y": 1, "spacing_x": 0, "spacing_y": 0
  }
}
```

不规则区域示例：

```json
{
  "columns": 1, "rows": 1, "frame_names": ["idle", "run"],
  "layout": {
    "kind": "sprite_sheet", "mode": "regions",
    "regions": [
      {"name": "idle", "x": 2, "y": 4, "width": 32, "height": 40, "pivot_x": 0.5, "pivot_y": 1.0, "animation": "idle", "duration_ms": 120},
      {"name": "run", "x": 40, "y": 4, "width": 36, "height": 40, "pivot_x": 0.5, "pivot_y": 1.0, "animation": "run", "duration_ms": 80}
    ]
  }
}
```

坐标为原图像素，枢轴为 0..1，时长单位为毫秒。矩形必须在原图内，图集帧不能覆盖整张图集。每素材最多 64 帧，总裁切面积最多 16,777,216 像素。帧名使用 ASCII 字母、数字、下划线、连字符，忽略大小写后唯一，不能使用 Windows 设备名。大图集应只选实际需要的区域。

素材库元数据支持同名、同目录的 `.json`（TexturePacker 对象或数组 frames）和 `.xml`（Kenney 风格 TextureAtlas/SubTexture）。只读已配置的公开库，最多 256 KiB；JSON 优先，最多返回前 64 个区域并记录截断。原名称转换为安全名称并附短摘要，模型使用返回名称。`meta.image` / `imagePath` 必须对应当前图片名称，不跟随其路径读取文件。

旋转、裁边还原信息、XML 实体或错误元数据不会被猜测处理。可提供已确认的显式区域，或标为 unresolved。导入时重新核对图片、许可证和元数据，搜索后有变化即拒绝。

JSON 元数据中已有的 pivot 和 duration 会保留；即使缺少 trimmed 标志，只要原始尺寸或帧放置信息说明需要裁边还原，也不会静默当作普通矩形处理。

## Godot 使用方式

帧已经切好，应作为完整纹理使用，不要再套用旧图集坐标：

```gdscript
$Sprite2D.texture = preload("res://assets/normalized/player/idle.png")
$Sprite2D.region_enabled = false
$Sprite2D.hframes = 1
$Sprite2D.vframes = 1
```

对 `centered=true` 的 Sprite2D，用 `offset = (Vector2(0.5, 0.5) - pivot) * frame_size` 将帧枢轴对齐节点原点。动画顺序、时长、统一缩放和帧间对齐仍由 Developer 实现并通过预览验证。工具不写游戏逻辑，不改变 Player 的输入、时钟或重放。

## 权限、预算与兼容

`asset_normalize` 仅供 Art Producer 的宿主适配器调用，不能执行任意脚本。它沿用公开工作区、SHA-256 检查、文件及总产物限额。裁切消耗一次工具调用，读写单独计费，不增加模型或生图调用。失败不发布半份素材包；隐藏评测资料和 `.env` 不进入输入。

两个正式入口的公共 `role_setup` 启用 `ArtProducerConfig.normalize_assets=True` 和 `DeveloperConfig.require_normalized_assets=True`，实验记录会冻结配置。旧清单和报告仍可读取；独立角色构造器保留旧默认值，以兼容历史示例。独立使用新链路也应同时启用这两个配置。新配置不要续跑旧检查点，请新建输出目录。

未解决的布局和质量拒绝的素材继续出现在两个控制器共用的 Art 待办中，不能通过 no_op 清除。`normalization.status=rejected` 保存 `quality.findings` 和诊断预览，但 `frames=[]`，不发布可用纹理；Developer 可以推进其他实现，但保持 partial，QA 的 final_art 门槛也不通过。人物空白帧会被拒绝，其他类型空白帧保留限制记录。不能仅凭几何检查成功认定视觉合格。

## 已有真实素材验证

结果在 `runs/asset-normalization-review-001/real-assets/`，复现命令如下（先安装项目依赖）：

```bash
python examples/asset_normalization_demo.py --output runs/asset-normalization-real-assets-002
```

脚本不读取 `.env`，不调用模型、生图或引擎。使用原始 `platformer-time-loop/instruction.md` 和 `fixed-loop-permissions-002` 中真实图片的快照；快照在 `tests/fixtures/asset_normalization/`，含原始清单、摘要、许可证。

| 素材 | 验证与实际观察 |
| --- | --- |
| 真实生成 player | 旧报告验证四帧几何裁切；当前脚本会将含洋红底、地面拼版的角色图隔离，不再发布游戏帧 |
| OpenGameArt bitmap font | 选取数字 0、1；字形和透明像素保持正确 |
| OpenGameArt 洞穴层 | 地面片段和故意选择的透明区域；原图是场景层，不能仅凭 platform_atlas 名称认作可用瓦片集 |

8 个区域逐像素比对通过，原始快照不变。每组保存原图、矩形标注、逐帧 PNG、联系表、规范化清单及正确/错误引用示例。`checks.json` 记录整图引用、改名图集副本和合法逐帧引用的检查结果。公共执行器账本确认 0 次模型、生图调用。

`runs/asset-normalization-review-001/contracts/` 保存两个控制器的完整离线输入输出、工作区、报告。模型、引擎和图片使用明确标记的测试替身，验证交接和重放协议兼容性，不能当作真实生成质量或真实 Godot 试玩证据。

## 在线复跑

由你执行，现有 `.env` 的生图仍走 DashScope 原生接口，文本模型调用方式不变。本次未做在线运行。

```bash
python examples/fixed_loop_demo.py --output runs/fixed-loop-normalized-assets-001 --max-cycles 3 --max-model-calls 32
python examples/scheduler_demo.py --output runs/scheduler-normalized-assets-001 --max-role-calls 15 --max-model-calls 48
```

这些上限用于联调，不代表正式对比预算已经匹配。运行后查看 Art 的 `asset_manifest.json`、`art_handoff.md`、`asset_previews/`，Developer 的 `asset_packages`、纠错反馈及 MCP 预览，和 Player/QA 对当前版本的证据。

若要 Developer 在循环内查看实际画面和联系表，在上述命令末尾加 `--godot-mcp`；需已有 [Godot MCP](godot_mcp.md) 文档中的本地容器环境。未加该参数时，切图和误用检查仍启用，Developer 不执行 MCP 自检。

这条链路保证明确布局的几何裁切、来源追踪和原图隔离；不自动识别轮廓、删除地面/文字/拼版，也不能自动判定某图是否被错误声明为独立背景。静态检查识别字面量，动态拼接路径主要依靠原图不装配及引擎检查暴露问题。画面、动画及需求满足仍需 MCP、Player 和 QA 验证。
