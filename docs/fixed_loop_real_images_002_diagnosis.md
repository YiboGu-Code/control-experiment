# fixed-loop-real-images-002：运行分析与修复

原始目录 `runs/fixed-loop-real-images-002/` 保持不变。诊断材料和真实 Godot 的离线对照输出位于 [独立诊断目录](../runs/fixed-loop-real-images-002-diagnostics/review.md)。

## 运行结果

[汇总](../runs/fixed-loop-real-images-002/summary.json) 表明这次已经越过 Art Producer，阻塞在 Developer 的基础构建。总时长约 15 分钟。

| 阶段 | 结果 | 含义 |
| --- | --- | --- |
| Designer | 1 次角色调用完成 | 新建 21 条需求 |
| Art Producer | 1 次角色调用、2 次文本请求完成 | 一次搜索、一次有效方案；导入 Kenney/OGA 共 4 项库素材，并通过原生 DashScope 生成 1 张标题图。没有方案纠错记录，5 项素材均非 mock |
| Developer 第 1 轮 | 3 次文本调用、2 次构建失败 | 首次回复试图写保护目录 `licenses/README.txt`，被协议校验拦截；重提后出现脚本类型、场景节点和场景切换错误 |
| Developer 第 2 轮 | 3 次文本调用、3 次构建失败 | 引入未加引号的资源 ID，修正后仍有相同节点/生命周期错误 |
| Developer 第 3 轮 | 3 次文本调用、3 次构建失败 | 重写文件但未修复根因，最终 `game-0003` 的 build_status 仍为 failed |
| Player | 3 个槽位全部跳过，0 次调用 | 没有满足基础构建门槛，未开始真实试玩或重放 |
| QA | 前两轮 completed，ready=false；第 3 轮取消 | completed 仅指完成审查，不能理解为交付验收通过 |

最终记录为 `cancelled`，QA 第 3 次请求的 metadata 也记录 `cancelled`；这不是模型额度耗尽或图片失败的记录。具体取消操作原因无法仅凭输出确定。框架保留此前提交的工程，导出了 `game-0003`，但 `delivery_ready=false`，没有 Player 轨迹。

文本调用共 15 次，其中 14 次有完整响应、最后 1 次被取消。账本 `token_usage_complete=false`，汇总 token 包含未确认调用的保守预留，不能全部当作服务商确认的实际消耗。9 次 Developer 回复的 `finish_reason` 都为 `stop`，输出最多约 8,370 token，并未触及配置的 65,536 上限；这次也不是增加输出额度就能解决的问题。

## 根因与证据

### 1. 场景文件混入脚本调用

`Gameplay.tscn` 第 97、126 行含 `add_theme_font_size_override(...)`；另两份场景还有 5 处。这是 GDScript 方法调用，不是 TSCN 属性声明。该错误可以使资源后续节点停止加载，因此日志里的 `TimelineBar` 消失并不是简单的“忘记声明父节点”。

真实离线 Godot 对照确认：原错误写法丢失后续节点，替换为 `theme_override_font_sizes/font_size = 32` 后节点可正常加载。两者进程退出码都是 0，仅检查退出码会漏判。[Godot TSCN 格式说明](https://docs.godotengine.org/en/4.5/engine_details/file_formats/tscn.html)

### 2. 在已移除的旧场景上继续获取 SceneTree

最终 `scripts/main.gd` 在 `change_scene_to_file` 后执行 `await get_tree().physics_frame`，随后再访问当前场景。该脚本属于被替换的旧场景，切换时它立即离开树，后续 `get_tree()` 因而为空。模型反复改成 `call_deferred` 或更换等待帧信号，没有改变节点生命周期。

真实离线对照使用常驻 autoload 负责切换、保存树引用并等待 `scene_changed`，随后初始化目标场景，成功打印指定场景已初始化。另一可行设计是在切换前保存待初始化状态，由目标场景读取。不能把“缓存引用后让即将销毁的节点继续等待”当作通用修复。[Godot SceneTree 生命周期说明](https://docs.godotengine.org/en/4.5/classes/class_scenetree.html#class-scenetree-method-change-scene-to-file)

### 3. 键盘事件配置未生成真实事件对象

`project.godot` 的 4 个输入动作把事件写成 `{"physical_keycode": ...}` 普通字典。离线引擎验证该写法没有产生所需的 `InputEventKey`，即使动作名存在、进程退出码为 0，也无法据此宣称键盘控制可用。以 `Object(InputEventKey, "physical_keycode": 68)` 等真实事件对象序列化后，检查通过。

这是本次源码检查发现的后续交互问题；原运行的 Player 被跳过，所以原结果尚未提供键盘试玩证据。

### 4. 纠错反馈不够聚焦

旧提示已经带有完整文件和原始日志，但相同错误埋在重复输出中。连续回复声称修复了节点层级和场景切换，实际仍保留上述写法。新增的集中摘要会标明相同错误出现次数及具体源文件行，而不是将“模型声称已修复”当作事实。

其他尚未解决的产品问题包括：关卡数据只用章节计算所需开关数，不能证明已经实现 24 个不同谜题；脚本与场景中的敌人、激光、门桥等协作机制不完整；角色可见性、UI 与关卡可解性尚无真实试玩验证。源文件中仅有碰撞形状也不能作为可见角色。它们继续由 Developer 实现、Player 验证和 QA 验收，本次没有把基础错误修复说成完整游戏验收。

## 代码修改

- Fixed Loop 默认改为 `developer.v3`，保留 v2 的真实轨迹装配能力。每次 Developer 最多 5 次文本调用，原先为 3 次；全局调用上限仍由 `--max-model-calls` 控制。
- 新增 `roles/developer/diagnostics.py`：在应用补丁前检查明确的资源格式和键盘事件序列化错误；生成含历史重复次数、源位置和建议的 `repair_brief`。生命周期模式为建议，不对所有 autoload 一概拒绝。
- 补充英文协议：声明式场景属性、常驻场景切换、真实输入事件、可见角色素材和实际关卡内容。校验失败通过原有有限纠错机制重提；不自动替模型改代码。
- “父节点路径消失”警告也会使 v3 构建失败，避免成功退出码或部分场景加载造成误判。原始引擎日志继续保存。
- 汇总增加 `final_build_status`、`game_builds`、`roles_not_invoked`，审阅页明确工程导出不等于可玩。未修改历史结果来伪装新输出。

`.env`、文本/图片模型接口和 Benchmark instruction 不变。新旧 Developer 配置在一次运行内冻结，未来 Scheduler 应使用相同角色协议和预算进行对比。

## 验证与复跑

68 项离线角色与流程测试通过，使用 Benchmark 的原始 instruction、已保存的真实模型产物和明确标注的纠正回复夹具。另有 3 项真实 Docker/Godot 对照测试通过，覆盖场景部分加载、场景生命周期、键盘事件；修正前复现错误，修正后均通过。没有发起在线文本或生图请求。

```sh
python -m pytest tests/test_developer.py tests/test_developer_diagnostics.py tests/test_fixed_loop.py tests/test_fixed_loop_images.py -q
```

真实引擎对照的完整输入与输出见 [诊断审阅页](../runs/fixed-loop-real-images-002-diagnostics/review.md)，运行方式见 [Developer 说明](developer.md#v3源文件检查与重复错误反馈)。

请用新目录运行下一轮完整实验：

```sh
python examples/fixed_loop_demo.py --output runs/fixed-loop-real-images-003 --max-cycles 3 --max-model-calls 32
```

仍默认使用真实文本模型和 DashScope 生图。新运行应在 `experiment.json` 中显示 Developer v3、单次 5 次调用；模型输入中的 `repair_brief` 和 `proposal_feedback` 会显示修复依据。是否能完整游玩仍需新运行的构建、Player 和 QA 证据，不能由本地最小复现通过推断。
