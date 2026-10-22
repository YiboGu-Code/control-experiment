# fixed-loop-real-images-001：Art Producer 失败诊断

检查对象是用户实际运行的目录 `runs/fixed-loop-real-images-001`，原命令为：

```sh
python examples/fixed_loop_demo.py --output runs/fixed-loop-real-images-001 --max-cycles 3 --max-model-calls 32
```

## 运行事实

[汇总](../runs/fixed-loop-real-images-001/summary.json) 记录：Designer 完成 1 次，Art Producer 失败 2 次，共 5 次真实文本请求，生图请求 **0 次**。没有 Developer 工程或可交付游戏，17 条需求均未实现。连续角色调用失败默认上限为 2，所以第二轮 Art 失败后即停止，不会等到第 3 轮。

两次 Art 调用均先成功搜索库，再在提交素材方案时失败。原执行器只记录 `Art Producer returned an invalid JSON proposal`，丢失了对模型有用的具体修正信息。

| 文本调用 | 原始证据 | 诊断 |
| --- | --- | --- |
| 001 | [回复](../runs/fixed-loop-real-images-001/model_calls/art_producer/001/response.txt) | 搜索请求通过 |
| 002 | [回复](../runs/fixed-loop-real-images-001/model_calls/art_producer/002/response.txt) | `kenney-tileset-sheet` 声明 14×7=98 格，却只有 64 个帧名；98 格也超过现有 64 格上限。`kenney-ui-elements` 声明 128×128、1 列 6 行，128 无法被 6 整除 |
| 003 | [回复](../runs/fixed-loop-real-images-001/model_calls/art_producer/003/response.txt) | 下一轮重新搜索，通过 |
| 004 | [回复](../runs/fixed-loop-real-images-001/model_calls/art_producer/004/response.txt)、[响应](../runs/fixed-loop-real-images-001/model_calls/art_producer/004/response.json) | 不断枚举 `tile_001` 等帧名，最后一个完整名称为 `tile_3317`；输出用满 32,768 token，`finish_reason=length`，在字符串中间截断，JSON 不完整 |

另一个尚未执行到的错误：002 中 `2D/ui-pack/PNG/Light/ui_button_square.png` 没有出现在该次实际搜索结果中；即使只修正网格，也会被素材来源检查拒绝。004 已写出的第一个库资产对象还缺少 `source_path`。不能通过关闭校验、猜测文件路径或截去帧名来让流程“通过”。

这份结果没有发出 DashScope 生图请求，因此不能用于判定图片接口是否可用。现有图片适配器与 Chat Completions 调用方式保持不变。

## 修改

新调用默认启用 `art-producer.v4`：

1. 提示明确区分图集整体尺寸、规则网格和整张纹理。大型/不规则库图集可用 1×1 的 `atlas` 帧整体导入，原字节和尺寸不变；Developer 根据接入说明处理区域选取，不能声称已视觉确认各区域。也可以检索单独图块。
2. 模型违反 JSON、网格、来源或尺寸约束时，得到含字段位置的具体反馈并完整重提；保留已查到的目录结果。搜索和纠错共享默认 4 次文本调用上限，公共 token、时间、工具预算继续生效。
3. 无效回复只向下一次提示回传最多 8 KiB 摘录，完整原文留在调用档案。纠错记录写入后续提示和成功产物的 `proposal_rejections`。用完修正次数时，最终错误保留明确原因。
4. 仅在完整方案通过校验后执行素材导入或生图；不自动调用修图、不改 instruction、不修改许可、不伪造 QA 通过。显式 v2/v3 保持原有失败即退出行为。

用户允许为效果扩大 token 额度。本次检查确认实际 Art Planner 上限已经是 32,768，第一次 7 项资产方案只使用 2,558 输出 token；第二次上限耗尽来自失控枚举，且该方案即便完整也不合法。因此本次保留现有 `.env` 配置，先修复协议和纠错机制，没有通过降低 token 上限节省用量。`*_MAX_OUTPUT_TOKENS` 控制输出额度，并不能扩大服务端模型的上下文窗口。

## 验证

`tests/fixtures/art_producer_real_images_001/` 保存两份失败回复的原始字节及 SHA-256 来源记录。历史运行目录保持不变。

离线回归覆盖：真实失败回复 → 字段反馈 → 完整修正 → 保留搜索结果并导入整张图集 → mock 生图；未搜索路径与错误尺寸的修正；纠错次数耗尽；共享模型预算提前阻止重试；v2/v3 行为；Fixed Loop 在同一轮继续进入 Developer、Player、QA。

其中修正后的模型回复、库图片和引擎结果为测试夹具，不能作为新一轮真实模型质量或交付验收结果。没有执行新的在线模型或生图测试。

验证完成：相关 83 个不同离线用例已通过，包括角色原有校验、真实错误回放、Fixed Loop、图片接口接线和素材库边界。测试开发时发现的回调重复读取执行锁及 mock 产物状态断言问题已修正并复测；没有以测试通过替代真实模型质量结论。

复核命令：

```sh
python -m pytest tests/test_art_producer.py tests/test_art_producer_repair.py tests/test_fixed_loop.py tests/test_fixed_loop_images.py tests/test_asset_libraries.py -q
```

## 用户复跑

使用新目录，保留旧运行便于对照：

```sh
python examples/fixed_loop_demo.py --output runs/fixed-loop-real-images-002 --max-cycles 3 --max-model-calls 32
```

入口默认仍使用 `.env` 的真实文本模型和 DashScope 原生图片服务；不传 `--mock-images`。新运行会保存模型完整请求/回复、`experiment.json`、调用索引、图片调用档案、共享状态、控制器检查点和汇总。可以从 Art 后续请求的 `proposal_rejections` 直接看到纠错原因；角色成功时也可从公开 `asset_manifest.json` 查看纠错记录。新运行是否满足全部游戏要求仍需根据其 Developer、Player 和 QA 证据判断。
