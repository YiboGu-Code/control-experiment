# fixed-loop-permissions-002 诊断与修复

诊断日期：2026-10-23。输入为 `runs/fixed-loop-permissions-002/`，原目录保留不变。本次未调用在线文本或图片模型，未修改 `.env`、原 instruction、历史产物或验收结果。

## 运行情况

上次的权限修复已生效。第一轮 Developer 经 5 次构建尝试后通过检查，随后 Player 和 QA 都正常执行。Developer 的 `partial` 不会阻止构建已通过的游戏进入试玩。

| 阶段 | 实际结果 |
| --- | --- |
| Designer | completed，生成真实任务的设计 |
| Art 第一轮 | completed，生成 16 个真实图片素材；没有完成任务要求的库浏览 |
| Developer 第一轮 | partial，`game-0001` 构建通过，但画面和交付待办未解决 |
| Player | completed，采集 5 条轨迹，分别重放 2 次，10 次状态与像素比较全部通过 |
| QA | completed，但游戏未就绪：画面、部分玩法证据、美术来源检查及交付轨迹仍有缺口 |
| Art 第二轮 | completed，新增 9 次生图并引入 OGA 素材 |
| Developer 第二轮 | failed，修复中的模型流被总运行期限中断；没有提交 `game-0002` |

Player 的重放通过仅证明这些输入在当前版本、固定环境与观测范围内可复现，不证明幽灵、机关、桥梁等所有需求已满足。QA 保持 `delivery_ready=false` 是正确的。

## 1. 总期限中断被误报成用量错误

运行从 2026-10-22 20:04:06（北京时间）开始，在 23:04:06 中断，恰好触及 `experiment.json` 中的 **10,800 秒**。第二轮 Developer 从约 22:35 开始，最后一条调用为 `model_calls/developer/011`；执行器记录 `timeout: Invocation deadline exceeded`，提供商记录取消，流未完整结束。

角色配置已允许长时间思考：Developer 单次墙钟上限 22,140 秒，Art 为 22,200 秒，但整体仍只有 10,800 秒。增加模型输出上限不能改变这个限制。

原控制器先检查 `token_usage_complete=false`，因此摘要显示为 `failed` 和“用量不完整”。现改为优先报告实际已到达的总期限，同时保留未知用量。中断调用的预留 token 不被伪造为已确认用量，也不会解除未知用量禁止继续派发的约束。

两种控制器默认总期限均改为 43,200 秒；可用 `.env` 的 `RUN_MAX_WALL_SECONDS` 配置，命令行 `--max-wall-seconds` 优先。预算仍被冻结到 `experiment.json`，角色不能自行扩大期限。显式指定 10,800 秒仍会严格执行。

暂存目录 `.staging/29aba70d8a3bf1b83adc74c8/game/` 中保留了第二轮修复草稿及装配轨迹；已有 2 次暂存预览通过，但不能据此把整份草稿当作已提交、已重放、已验收的版本。最终交付仍是 `game-0001`，缺少打包轨迹，导出规则没有偷偷采用失败调用的文件。

## 2. 假透明与素材内容问题

两次生成的 `player_spritesheet` 原图都是 1024×1024，alpha 范围为 **253–255**，没有任何全透明像素。旧归一化逻辑只在 alpha 最小值等于 255 时去底色，因此错误地跳过处理。原图品红背景还偏离纯 `#FF00FF`，仅去标准色也不够。

现在配置为色键模式时总会执行去背景，并保留原本的半透明主体。若四角都近乎不透明、呈明显品红且色差在容差内，会额外采用四角平均色，实际色值写入处理记录。不符合条件的背景不猜测；原生透明模式保留原 alpha 和主体颜色。只含 250–255 这类近乎不透明 alpha 的图片不再通过透明检查。

对两张原始图片的本地重处理结果：

| 生图调用 | 旧输出全透明像素 | 新输出全透明像素 | 总像素 |
| --- | --- | --- | --- |
| 002，首轮角色图 | 0 | 2,578 | 4,096 |
| 017，第二轮角色图 | 0 | 2,955 | 4,096 |

这只证明背景处理缺陷已修复。原图实际包含卡片边框、文字、平台地块和场景背景，并没有按请求交付独立角色图集；整图缩放也造成比例失真。去背景不能使这些素材自动合格。诊断目录保留原图、旧输出与新输出供对照，没有替换历史游戏资源。

对全部 25 张已保存原图的离线格式/透明处理检查，24 张通过，011 时间轴原图被正确拒绝：它实际是一张近乎不透明的游戏场景拼图，背景含粉色棋盘格，四角也不满足保守品红取色条件。不会为通过检查而猜测删除更多主体颜色。这张素材仍需重新生成或从库中选择；其他 24 张通过的也只是透明/尺寸校验，不是美术质量验收。

Art 提示和生图提示已区分“本张素材的唯一主体”与“配色风格参考”，避免把全游戏的对象描述混入每张独立角色图；强化库可用且 instruction 要求浏览时先检索，优先考虑独立小素材，仍保留合法自生成选项。提示改动的实际生成效果需要下一次在线运行验证。

## 3. 视觉修复反馈与职责

第一轮最后一次 MCP 预览仍报告严重错误：角色像草地块、品红边缘残留，幽灵不透明、时间轴不清晰等。Developer 的结束文字却称主要视觉问题已消除，存在与实际反馈不符的表述。

- Developer 看图工具现在除游戏截图外，还接收至多 4 张当前源码引用的真实 Art 素材，优先图集；仍走公共引用校验、图片大小限制和 token 计量。可从源素材内容区分美术问题与代码裁切问题，不要求文本 Developer 假装看过图片。
- 当前工程任一次视觉反馈中的 `error` 会被宿主保留为待办，模型空的 `remaining_work` 不能将其抹掉。提交实际修复并检查新版本后，旧版本诊断才不再限制当前结果。warning 不自动阻止完成。
- QA 提示明确：Player 负责采集、证据映射与重放；Developer 宿主负责把真实采集轨迹装入 `demo_outputs`。素材本身错误与代码集成错误分别指派；需要两个角色处理时拆分问题。Player 报告也不再把所有素材笼统叫作 mock。

本次没有强行把 Developer 改成 completed，也没有将 QA 的未验证需求改为通过。部分机制及最终画面仍需新版本真实试玩确认。

## 验证与保留文件

诊断文件夹：[runs/fixed-loop-permissions-002-review](../runs/fixed-loop-permissions-002-review/)。

- `diagnosis.json`：原始运行目录哈希、角色结果、QA 门槛、10 次重放状态、暂存草稿标记及图片处理统计。
- `diagnose.py`：可重复执行的本地取证程序，不读取 `.env`，不调用模型，前后校验整个原始目录未变化。
- `image-002/`、`image-017/`：`source.png`、`previous-output.png`、`normalized-output.png` 和处理明细。
- `all-image-normalization.json`：全部 25 张原图的本地透明处理检查，包含时间轴原图的明确拒绝原因。
- 离线回归测试覆盖截止时间与未知用量的优先级、配置覆盖、近乎不透明 alpha、偏色品红、原生透明主体保护、视觉源素材输入及错误保留。最终测试记录见该目录的 `verification.md`。

## 下一次真实运行

使用新输出目录，现有镜像无需因本次 Python/提示词修改而重新构建。先预检，预检不调用在线模型：

```sh
python examples/fixed_loop_demo.py --godot-mcp --preflight-only --output runs/fixed-loop-permissions-003-preflight --max-cycles 3 --max-model-calls 64 --max-wall-seconds 43200
```

然后真实模型、真实生图运行：

```sh
python examples/fixed_loop_demo.py --godot-mcp --output runs/fixed-loop-permissions-003 --max-cycles 3 --max-model-calls 64 --max-wall-seconds 43200
```

保留整个目录，重点查看 `summary.json`、`review.md`、最新 Developer/Player/QA 报告及 `model_calls/developer_visual/`。模型上限和思考模式继续沿用你的 `.env`；本次没有更换模型。若进行 Fixed Loop 与 Scheduler 对比，两组应使用相同的新代码、源素材快照、角色与视觉配置和总预算。
