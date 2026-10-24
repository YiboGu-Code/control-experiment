# Fixed Loop developer-opt-001 诊断与修复

分析对象为用户真实运行的 `runs/fixed-loop-developer-opt-001`，对照 `runs/fixed-loop-permissions-002`。本次只读取历史输入、输出、代码、日志及截图，并运行离线回归；没有调用在线模型、生图服务或真实引擎，没有修改 `.env` 或历史交付工程。

## 三项优化是否生效

| 项目 | 本次真实结果 | 判断 |
| --- | --- | --- |
| 按阶段控制思考 | 22 次 Developer 主调用全部 `deep`、实际请求全部 `reasoning_effort=high`、`enable_thinking=true`，输出上限仍为 131072 | 参数通路已启用，但这次没有发生降档，不能算已取得提速收益 |
| 按需加载上下文 | 实际发出 8 次 `context` 决策；主调用平均输入从 94,962 降到 41,266 token，峰值从 163,373 降到 55,753 | 确实生效；但原缓存实现丢失已读文件，造成重复请求和拒绝 |
| 局部代码修改 | 9 份响应包含 `edits`，其中 7 次被接受并应用，共编辑 8 个文件次；没有每轮都重写整个工程 | 确实生效；局部编辑本身不代表模型思考时间短 |

比较基于 SDK 实际请求和服务确认的 usage；不把字节估算当 token，不把失败请求的预算预留当实测消耗。完整指标及逐调用来源在 [comparison.json](../runs/fixed-loop-developer-opt-001/diagnosis/comparison.json)。

## 时间花在哪里

| 指标 | permissions-002 | developer-opt-001 |
| --- | ---: | ---: |
| 全部角色执行累计时间 | 180.0 分钟 | 241.9 分钟 |
| Developer 累计时间 | 94.5 分钟 | 130.6 分钟 |
| Developer 主模型时间 | 76.7 分钟 | 103.9 分钟 |
| Developer 视觉模型时间 | 16.0 分钟 | 24.2 分钟 |
| Developer 其余时间 | 1.8 分钟 | 2.5 分钟 |
| 主模型调用数 | 11（其中 1 次中断） | 22 |
| 视觉模型调用数 | 5 | 5 |
| 主调用确认的思考输出 token | 154,128 | 319,882 |

本次 Developer 约 98.1% 的时间耗在模型调用。输入减少了，但调用次数和思考输出增加，所以**本次没有提速**。仅上下文问题引起的 5 次被拒绝决策就消耗了 33.6 分钟；这是已观察到的无效调用时间，不是对下次节省时间的保证。

这两次不是严格 A/B：旧运行受 3 小时预算中止，本次总预算为 12 小时；素材规范化配置也不同。旧运行只提交 game-0001，第二次 Developer 超时；本次提交了 game-0001 和 game-0002。不能将全部耗时或画质差异归因于三项优化。

此外，第 1 轮 Designer 因 R025 的原文引用不完全匹配被拒绝，消耗约 14.8 分钟；第 2、3 轮才进入后续角色。最后一次 QA 的流式连接在中途断开，约 10.5 分钟后失败，并未形成当前版本的验收结果。

## 最终游戏质量

本次可见改善是人物轮廓、比例和可读性，Reset 控件也较明显；已打包两条演示输入轨迹。不过游戏仍不能视为交付就绪：

- 最新 game-0002 的 Player 比较是 **2/10 通过、8/10 失败**；10 次状态比较都一致，失败来自画面像素。相比之下，本次 game-0001 和旧基线的已提交 game-0001 均为 10/10 通过。
- 当前代码 `scripts/gameplay_final.gd:623` 在绘制出口水晶时使用 `Time.get_ticks_msec()`。同一输入帧的状态一致，但不同进程的水晶亮度不同。实际帧 0 的差异框为 `(1103,509)-(1217,623)`，差异 32 像素，落在水晶位置。该证据支持壁钟动画是重放失败的原因之一，不能据此宣称排除了所有其他原因。
- 标题徽标溢出画面：`TextureRect` 使用大尺寸原图，却没有设置 `expand_mode=EXPAND_IGNORE_SIZE`；指定控件大小仍可能被原图最小尺寸撑开。这是界面布局问题，不应通过非等比压缩图片解决。
- 场景仍大片空旷，部分交互物依赖基础几何图形，时间轴面板偏薄。相较旧版本有局部提升，但未达到理想成品质量。
- 第二版素材清单中 9 项规范化成功、8 项被拒绝。部分“单个人物”图片实际上包含多姿态、文字和面板；单扇门图片含人物和重复门。拒绝这些素材是有效保护，不能为追求完成状态放宽检查。
- game-0002 在最后一次修改后没有完成最新工程的 MCP 收尾自检。最后 QA 请求中断；已有的完整 QA 报告属于 **game-0001**，不能拿来证明 game-0002 已通过。

原始截图供对照：

- [旧版 gameplay](../runs/fixed-loop-permissions-002/workspace/public/artifacts/player/d8ec874a0abe0a0b871a483b/sessions/c1_l1_near_rewind_replay-capture/frame_0599.png)
- [本次 game-0002 gameplay](../runs/fixed-loop-developer-opt-001/workspace/public/artifacts/player/792d74c313d76a22c3248216/sessions/near_rewind_reset-capture/frame_0089.png)
- [本次标题菜单](../runs/fixed-loop-developer-opt-001/workspace/public/artifacts/player/792d74c313d76a22c3248216/sessions/ordinary_menu_probe-capture/frame_0000.png)
- [错误的单人物原图](../runs/fixed-loop-developer-opt-001/workspace/public/artifacts/art_producer/f0308b8eb711b84113fc1c0c/assets/sprites/player_idle.png)
- [错误的单扇门原图](../runs/fixed-loop-developer-opt-001/workspace/public/artifacts/art_producer/f0308b8eb711b84113fc1c0c/assets/sprites/door.png)

不同截图的关卡状态、帧号不完全一致；这些图片用于观察具体缺陷，不是配对美术评分。

## 已修复的框架问题

1. **上下文缓存与额度死锁**：读取改为在原有 512 KiB 上限内累积；补丁后从已验证快照刷新内容和摘要，其他已读文件继续保留。重复读取不占新增资料额度；支持显式释放；自动选入的小文件在 48 KiB 上限内尽量保留。实际历史输入的离线复查复现原来的 5 个阻塞，修复后全部消除。它只验证读取与改写资格，不虚构替代运行结果。
2. **阶段不能恢复**：校验拒绝后的强制深度思考，改为在后续补丁构建通过时重新判定；初次实现、未知/复杂错误、玩法修改仍保留深度。当前有效预览已完成但额度耗尽时允许轻量报告剩余问题，不能低强度继续写代码，不能清除宿主记录的视觉错误。过期或失败预览仍不能冒充完成。
3. **壁钟导致像素不一致**：增加针对 `_draw*` 函数直接读壁钟的静态错误检查，反馈要求使用关卡重置的模拟 tick；提示词也明确要求确定性可见动画。不修改 Player 的像素比较规则。该检查范围有限，仍需独立真实重放。
4. **控件被原图撑大**：增加 TextureRect/TextureButton 尺寸设置的窄范围提示检查，明确保持宽高比、设置 expand/ignore_texture_size，并检查普通菜单。检测不到共享辅助函数配置时只给 advisory，不武断阻断构建。
5. **生图约束相互矛盾**：原先单个对象也套用角色动画序列提示。现在单张素材明确只画一个指定对象、一个状态；门、开关等不得附带演示人物。动画图集另用序列模板。保留无文字、无地面、纯色去背和原生像素处理要求，仍通过同一规范化与质量检查约束模型实际输出。这里只改提示词，没有重新生成或美化历史图片，也不保证模型一定遵从。
6. **失败定位**：Designer 原文校验错误明确包含需求编号；SDK 流式传输中断保存明确错误类别和原始块，保持 usage 未确认，不自动重试，不把残缺 QA 当完整验收。

上述公共模块由 Fixed Loop 与 Scheduler 共用。输出 token 上限、模型思考开关、工具权限、隐藏评测隔离与验收门槛均未放宽。

## 结果文件与离线验证

入口：[diagnosis/review.md](../runs/fixed-loop-developer-opt-001/diagnosis/review.md)。包含完整指标、真实上下文复查、静态发现、单张素材提示词前后全文、两种控制器的离线完整 I/O、测试结果，以及原始文件完整性检查。离线夹具用于验证流程与边界，不代表模型质量或真实提速。

相关回归按当前测试去重后 367 项通过、0 项失败；两种控制器完整链路均通过。分析前记录的 1,413 个原始运行文件摘要全部保持一致。

统计和上下文复查可重复运行（输出文件必须不存在）：

```powershell
$env:PYTHONPATH='src'
python examples/analyze_developer_run.py --runs runs/fixed-loop-permissions-002 runs/fixed-loop-developer-opt-001 --output runs/fixed-loop-developer-opt-001/diagnosis/comparison-recheck.json
python examples/recheck_developer_context.py --run runs/fixed-loop-developer-opt-001 --output runs/fixed-loop-developer-opt-001/diagnosis/context-recheck.json
```

## runs 清理

已清理 13 个未被源码、示例、测试或文档引用的过时失败/预检目录，以及 `designer-live-008/fixed-loop-permissions-002` 这一逐文件 SHA256 相同的重复副本（997 个文件）。共删除 14 棵目录树、释放 187,232,493 字节，约 **178.6 MiB**。

每个删除目标都先核对绝对路径在 runs 内并拒绝链接路径；完整路径、原因、文件摘要和删除结果见 [cleanup.json](../runs/fixed-loop-developer-opt-001/diagnosis/cleanup.json)。最新运行、对照基线、仍被引用的真实素材与复现输入均保留。有些名字相似的嵌套目录内容不同，因此保留；没有按名称或日期批量删除所有旧结果。目录索引见 [runs/README.md](../runs/README.md)。

## 下一次真实运行

本次未做在线测试。请用全新输出目录，保持此前参数，便于继续对比：

```powershell
$env:PYTHONPATH='src'
python examples/fixed_loop_demo.py --godot-mcp --output runs/fixed-loop-developer-opt-002 --max-cycles 3 --max-role-calls 15 --max-model-calls 64 --max-wall-seconds 43200
```

这会沿用 `.env` 中真实 chat、视觉与 DashScope 生图配置。下次重点核对缓存相关拒绝是否消失、是否实际发生收尾降档、模型调用数和耗时、普通菜单布局、最新版本的 Player 像素重放与 QA 完整验收。旧运行目录保留原样供追溯。
