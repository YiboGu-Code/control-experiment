# 原分辨率保留与生图内容约束

Fixed Loop 与 Scheduler 现在共用 `asset-pixels.v3`。本次没有在线模型、生图或真实 Godot 调用，没有修改 `.env` 或 Benchmark instruction。

## 像素与显示尺寸分离

v2 修复了拉伸，但仍将大图缩成方案要求的小图，细节会永久丢失。v3 不再对生成产物做缩小、放大、逐帧拉伸或独立裁边放大：保留接口返回画布的实际分辨率，进行必要的去底、质量检查，再保存为无损 PNG。原始返回字节仍保存在 `image_calls/NNN/source.*`。

例如，方案声明 128×32、4 帧，接口返回 1024×1024：源文件仍为 1024×1024，原生帧为 256×1024，每帧逻辑显示范围为 32×32。人物本身保持原图比例；如果原图的姿态或排布画错，必须重新生成，工具不会猜测重排。

宿主按照比例映射**区域边界**到原生像素，统一四舍五入公共边界，避免奇数尺寸造成接缝；显式边距、间隔、pivot、动画时长一起保留。Art 清单中的 `width/height` 和 `normalization.frames[].region` 是真实像素，`request.width/height` 是规划尺寸，`frames[].display_size` 是可选逻辑显示范围。素材库仍保留原始尺寸和坐标。

Developer 收到原生 PNG 和单独的显示范围。提示词要求使用一个缩放系数：

```gdscript
var s = min(display_size.x / texture.get_width(), display_size.y / texture.get_height())
sprite.scale = Vector2(s, s)
```

同一动画保持统一缩放与对齐；物理和碰撞尺寸不跟着高分辨率纹理变大。绘制风格平滑的素材使用 linear，明确的像素画才用 nearest。Developer 不能改写保护的美术文件。实际游戏中的节点比例和导入设置仍要看 Godot 预览。

无损的范围是：**去底完成后的已解码像素，在 PNG 编码、解码与裁切过程中不重采样、不量化。** 去底和边缘处理会有意修改匹配的背景像素；JPEG 源图自身已有损失不能恢复；在屏幕上缩小显示仍受显示像素数量限制。因此不声称任意源图或任意显示大小都能保留肉眼可见的全部细节。拒绝高于 8 位的 PNG，以免 Pillow 静默降位。

原生单边超过 4096、像素/文件/产物预算超限会明确失败，保留现有预算约束，不偷偷降采样。`ART_IMAGE_SIZE` 仍决定发给服务的生成分辨率；`ART_IMAGE_RESAMPLE` 为旧配置兼容保留，v3 不再应用，`public_settings.resample_applied=false`。mock 及旧自定义接口默认继续遵守请求尺寸；真实适配器通过宿主 `ImageServiceInfo.output_geometry=source_native` 明确声明原生分辨率语义，不从 PNG 自报信息推断权限。

## 人物生图的英文约束

`providers/image_prompts.py` 按素材种类加载人物、环境或 UI 说明。人物要求一格一个完整角色，用身体姿势表达运动，保持比例、服装、镜头和帧间尺度。前后两处强调禁止任何语言的文字、标签、数字、标志、水印、地面、平台、卡片、边框、场景背景、UI、投影和无关物体。帧名和动作描述仅是元数据，不能画进图中。风格说明只提供色彩和画法，不增加场景内容。

需要去底的精灵只允许纯 #FF00FF 底色（随后由宿主移除）或适配器支持的真实 alpha。背景素材可以保留场景；明确请求字体的 UI 素材不误套人物禁字规则。

DashScope 人物请求强制 `watermark=false`。Qwen Image 3.0 / 2.1 人物请求强制 `prompt_extend=false`；Wan 2.7 不发送该字段，因为官方明确不支持。其它型号仅在显式配置该参数时关闭人物扩写，不猜测未知接口能力。相关能力依据：[阿里云文生图说明](https://help.aliyun.com/zh/model-studio/text-to-image)。完整实际请求保存在 `image_calls/NNN/request.json`，不会修改聊天接口。

提示词不能保证模型每次遵循。现有质量门槛继续拒绝疑似文字碎片、地台、矩形面板和复杂背景，拒绝项发布零个可用帧；结构检查不是 OCR 或语义审核，最终视觉验收仍属于 Developer/QA。

## 离线验证与复跑

```bash
python examples/sprite_quality_demo.py --output runs/sprite-fidelity-review-002/evidence
python -m pytest tests/test_sprite_fidelity.py tests/test_sprite_quality.py tests/test_image_provider.py tests/test_dashscope_image.py tests/test_asset_normalization.py -q
```

`runs/sprite-fidelity-review-001/evidence/` 保存真实玩家/幽灵源图、旧小图、新原生图、完整提示词、质量问题和 `pixel_fidelity.json`。两张真实生成图去底后的 PNG 解码像素逐个相同；源图含错误内容仍被拒绝。合法 OpenGameArt 字体和背景保持原始像素和许可证。

`contracts/` 保存两个完整控制器的离线拒绝链路；`positive-handoff/` 保存两个控制模式下 Art→Developer 的原生像素、显示尺寸交接和完整输入输出。后者用合成像素夹具和脚本模型/引擎，均使用现有 Benchmark 真实 instruction，不是在线生成质量证据。测试还覆盖奇数边界、显式间隔、pivot、半透明、单像素细节、原生 SDK 请求参数、预算和错误素材隔离。

由你运行真实循环时使用新目录，不恢复旧检查点：

```bash
python examples/fixed_loop_demo.py --output runs/fixed-loop-native-pixels-001 --max-cycles 3 --max-model-calls 32 --godot-mcp
python examples/scheduler_demo.py --output runs/scheduler-native-pixels-001 --max-decisions 30 --max-role-calls 15 --max-model-calls 32 --godot-mcp
```

没有 `--mock-images` 时使用 `.env` 的真实生图配置；仍需原有隔离 Godot 环境。无需为了本次修复调整 `.env`，原生尺寸与文件占用通常比旧小图大。
