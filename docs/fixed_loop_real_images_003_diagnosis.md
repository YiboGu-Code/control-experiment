# 003 固定 Loop 诊断与修改

原始证据：`runs/fixed-loop-real-images-003`。本次诊断与测试：`runs/fixed-loop-real-images-003-diagnostics`。原始运行、交付工程和 instruction 保持原样。本次没有新的在线文本或图片调用。

## Developer 修复生效，但游戏未达到可交付质量

002 的三个版本均未通过构建，Player 未执行。003 的两个版本都通过了构建，五个角色均实际执行。第一版先遇到 autoload 与同名 `class_name` 冲突，随后在同一次 Developer 调用中修正；第二版一次构建通过。上一轮的资源文件、场景生命周期和按键绑定问题未再阻止启动。

| 项目 | 003 结果 |
| --- | --- |
| 停止原因 | `iteration_limit`，达到三轮上限 |
| 完整开发/试玩/QA 周期 | 2 次；首轮 Art 失败后下游跳过 |
| 文本调用 / 生图调用 | 20 / 6 |
| 输入 / 输出 token | 662,887 / 57,592，含 129,024 缓存输入 |
| 总耗时 | 约 24.4 分钟 |
| 游戏版本 | game-0001、game-0002 均 build passed |
| 真实采集 / 新进程重放 | 每版 3 条采集、6 次重放，全部一致 |
| 最终打包 | 2 条轨迹，都停留在菜单流程 |
| 就绪 | false；仅 1 项需求 verified_pass |

重放一致只说明同样的输入产生同样的观测。未走到的玩法分支仍可能损坏。

## 新发现及对应修改

1. **Art 首次提交没有纠错额度。** 前三次请求都在搜索，第 4 次提交五个资产均漏 `affected_scenes`。不是生图 API 失败，也不是 token 截断。Fixed Loop 现在给 Art 6 次总调用，其中最多 3 次搜索；搜索额度耗尽后 schema 和宿主共同限制继续搜索，剩余调用用于提交与纠错。字段仍由模型修正，宿主不伪造场景信息。
2. **构建证据误用了修改范围。** game-0002 仅修改 R015/R016，但 Godot 检查针对整个工程。旧实现只把构建证据映射到这两项，使 QA 对未修改文件的静态判断缺少可引用来源。现在构建证据关联当前工程全部需求，版本的 affected_requirement_ids 仍保留真实修改范围。构建仍不能证明玩法通过，运行证据也不会扩大映射。
3. **QA 把无效引用直接降级，未利用纠错额度。** 最后一轮有 35 个 criterion 决定被宿主否决，5 个 issue 更新被拒绝。现在允许一次计费的 grounding 纠错，再执行原有保守降级；不会放宽哈希、版本、需求关联或问题关闭条件。提示增加精确原文引用，禁止把“可用素材库”推导成“所有素材只能来自库”，并要求区分源码缺实现与运行尚未测试。
4. **试玩和打包覆盖过窄。** 原来的两个打包槽位取了最先采集的菜单轨迹，命名玩法场景被丢弃。现在固定选择普通入口与命名场景的组合；Player 增至 5 条试验，打包两条后仍能探索三条。600 帧限制不变。
5. **30 秒玩法没有适配 20 秒证据窗口。** Designer/Developer 必须规划和实现临近关键转换的确定性场景，复用普通游戏的真实规则；Player 优先验证这些场景。不能修改正常 30 秒计时、扩大公开 trace 上限或伪造胜利。
6. **模型此前未看到截图。** Player/QA 现在可把校验过的真实截图字节发给视觉模型。QA 只有引用本次发送的截图、对应当前版本证据和需求时才能做视觉判断；未发送的图片、未出现的场景仍不能验收。传输与权限使用公共执行器，两个控制范式可复用。

## 真实玩法与画面缺陷

自然回卷的本地隔离 Godot 诊断观察到：

```json
{"loops": 2, "recordings": 1, "ghost_nodes": 0, "timer": 28.5333333333334}
```

`player.gd::_on_loop_end` 保存了录像，但没有通知关卡生成幽灵；`level_base.gd::_spawn_ghosts` 仅在初始加载时调用，此时录像还是空的。独立触发 Ghost 后又复现 `Sprite2D.animation` 属性不存在的运行错误。两个复现分别见诊断目录 `engine/natural-rewind/logs.json` 和 `engine/ghost-animation-property/logs.json`。这是诊断副本中的真实引擎执行，非新的模型产物或正式 Benchmark demo。

源码还缺少门、激光等要求的完整节点和交互链，关卡生成有大量重复。Developer 协议补充了转换回调、实体生成/清理、正确 Sprite 类型、可操作场景和真实关卡内容的检查要求。此次修复的是生成与反馈流程，未手工改写 003 的历史游戏。

实际截图中，图集中的多个元素一起显示成角色/地形，素材大小与布局失衡。更换素材尺寸或图集布局后，代码必须同步调整区域裁切、缩放和碰撞体对齐；仅复制新贴图不能完成集成。以下是原始 003 的游戏画面：

![003 实际画面](E:/Game_Generation/adaptive_game_generation/runs/fixed-loop-real-images-003/workspace/public/artifacts/player/792d74c313d76a22c3248216/sessions/play_level_1_loop_corrected-capture/frame_0599.png)

## Godot MCP 的接入边界

Godot MCP 可提供场景树检查、编辑、运行与截图能力，具体工具随实现不同。例如 [Godot Asset Library 中的 Godot-MCP](https://godotengine.org/asset-library/asset/5245) 提供这些工具族。本次会话没有已连接的 Godot MCP 服务；未下载或启动一个能访问宿主全盘的编辑器服务。

当前项目已有网络关闭的 Godot 执行和截图/场景状态采集，本次优先补齐了“图片实际进入模型”的缺口。后续接 MCP 应替换/扩展这一执行后端：每次角色调用使用临时工程和独立进程，工具白名单按角色划分，修改只发生在 Developer 暂存工程，Player/QA 不能编辑游戏；每次工具调用记入同一预算。返回的场景与截图必须经校验后注册，编辑器后台状态不能穿越轮次。MCP 本身不保证美术质量，视觉反馈和玩法证据仍需要保留。

## 模型与复跑

本次验证已完成：按逐用例最新执行汇总，291 项离线回归通过、2 项真实隔离 Godot 诊断通过、2 项环境相关测试跳过（Windows 符号链接权限，以及未显式配置的 Python 容器镜像）。这不是一次全量套件零失败的记录；开发中发现的夹具/兼容问题已修正并复测，旧 XML 保留。详见 [验证汇总](../runs/fixed-loop-real-images-003-diagnostics/test_verification.json) 与 [审阅入口](../runs/fixed-loop-real-images-003-diagnostics/review.md)。配置预检通过，747 个原始证据文件哈希未变。

已开启五个文本角色思考与流式输出，超时改为 1,200 秒。Player 上限 8,192→16,384，QA 24,000→32,768；Designer/Art 保留 32,768，Developer 保留 65,536 并显式 high 推理强度。详见 [模型配置](model_configuration.md)。003 所有回复都正常 stop，不能将这些失败归因于 token 不够。思考和看图是否改善质量，需要下一次真实运行验证。

先检查配置与本地镜像（不会调用在线模型）：

```powershell
$env:PYTHONPATH = 'src'
python examples/fixed_loop_demo.py --output runs/fixed-loop-preflight-004 --preflight-only
```

继续保持三轮做联调：

```powershell
python examples/fixed_loop_demo.py --output runs/fixed-loop-real-images-004 --max-cycles 3 --max-model-calls 48 --max-wall-seconds 14400
```

48 次只是容纳新增纠错、思考及探索的联调预算，不保证每个角色耗尽其上限；仍由统一账本在调用前限制。三轮最后新采集的轨迹可能来不及装配，需要更多轮次才能再构建、重放。正式比较固定 Loop 与 Scheduler 时，需冻结相同模型、视觉输入、角色预算和总体停止条件。

检查下一次结果时，重点看 near-rewind 是否实际生成幽灵、门/激光/开关交互是否出现、打包轨迹是否进入玩法、视觉问题是否指派给对应角色，以及 `response.json`/`request.json` 中思考返回和图像内容块是否存在。视觉输入支持和网关在线兼容性尚未实测，不把离线接口测试当成线上质量验证。
