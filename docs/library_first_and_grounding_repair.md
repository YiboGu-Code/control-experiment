# 素材库优先、引用修正与模拟时钟验证

后续修复见 [library-first-001 诊断](fixed_loop_library_first_001_diagnosis.md)。正式规范化模式下，回退生图的每条引用搜索须声明包含该素材的 `asset_ids`（最多四个相关变体），避免门/开关检索代替人物检索。一轮可批量发出最多十二条独立查询，每条仍计入公共工具预算，搜索轮数限制不变。

本次针对 `fixed-loop-developer-opt-001` 的问题修改共享角色和公共工具，Fixed Loop 与 Scheduler 使用相同实现。模型配置与 `.env` 未更换；没有调用在线 chat 或生图服务。

## 1. 先选库素材，生成只补缺口

以前“先浏览库”仅是提示词建议，现在 `library_first=true` 还会校验生成理由。角色应优先选用适合任务的独立 PNG、瓦片、UI 和字体，并围绕可用素材协调风格。不能只因偏好另一种画风就跳过现成素材。

两个库仍是独立只读接口，默认对应 instruction 中的 `/workspace/assets/library/` 与 `/workspace/assets/library-oga/`。服务器路径不同可配置 `ART_KENNEY_ROOT`、`ART_OGA_ROOT`。缺少库时明确记录不可用，不下载、不伪造候选，也不删改 instruction。

每个真实搜索结果有 `search_id`。库可用时，生成素材需提供类似下面的字段；示例中的路径仅说明结构，实际必须选本轮搜索返回的路径：

```json
{
  "generation_fallback": {
    "reason": "The returned character sprites do not provide the required clock device",
    "search_ids": ["search-001", "search-002"],
    "rejected_candidates": [
      {"library": "kenney", "path": "<actual returned path>", "reason": "Character sprite, not a clock device"},
      {"library": "opengameart", "path": "<actual returned path>", "reason": "Background artwork, not an isolated object"}
    ]
  }
}
```

引用的搜索必须覆盖所有可用库；每个非空结果至少审阅一个真实候选。无结果且扫描截断时不能据此断言库中不存在素材。宿主验证来源与理由的完整性，不能自动证明模型的审美判断正确；原始候选、许可证和拒用理由都会留下，便于查看模型是否仍在不合理地选择生成。

同时修复了检索排序：`player character sprite` 不再因为所有路径都含 `Sprites/` 就返回前几个无关文件；具体文件名匹配优先，同等条件下优先独立图。仍是有界关键词检索，不是视觉搜索。库图集也必须按真实布局切片，不能把“来自 Kenney”当成整张图可直接用作人物的保证。

## 2. 本轮内的质量反馈修复

流程为：查库 → 提交素材计划 → 导入/生成 → 原有去底、切图与质量检查 → 将失败素材的具体原因和原生成请求交给规划模型 → 修订提示词/布局，或改用库素材 → 再次处理。

正式入口配置如下：

| 约束 | 值 |
| --- | --- |
| Art 文本规划总轮数 | 8，含搜索、字段纠错和质量修复 |
| 其中搜索轮数 | 最多 3，每轮最多 2 个库请求 |
| 质量修复轮数 | 初次产出后最多再修复 2 轮 |
| 单轮素材计划 | 默认最多 16 项；独立 Art 示例为 12 项 |
| 总图片调用 | 默认仍为 32，覆盖全部角色调用和修复尝试；可显式设置 `FIXED_LOOP_MAX_IMAGE_CALLS` |

所有调用仍经过公共执行器。角色上限不能覆盖总预算，未使用的修复机会也不会自动消费。图片和模型调用额度不足时，已有候选可保留为 partial；权限、提供商、未知账目、文件/时间等硬限制仍按原有失败规则处理，不绕过预算。

同一请求原样重投会被拒绝。已经通过的素材不会因为另一张失败而重做。失败精灵不能改称 UI/背景来逃避检查。新生成是**基于反馈修改提示词后重新生成**，当前没有伪装成图像 SDK 的参考图编辑接口；服务失败不自动重试。

开启修复时每轮候选独立保存于 `art_attempts/001/` 等目录，保留原图、许可证、归一化帧、预览和摘要。`asset_manifest.json` 的 `quality_attempts` 保存完整候选历史，最终 `assets` 只指向最新候选；失败图片不进入游戏。Developer 继续使用稳定的 `assets/normalized/...` 路径。原生像素与宽高比处理未改变。

目前反馈来自结构/几何检查，不能识别所有语义错误，也不保证生成模型服从。最终仍由 Developer 预览、Player 和 QA 检查实际画面与任务；这次没有降低质量门槛。

## 3. Designer 保留有效需求，只修有问题的引用

选择“格式归一化 + 定向引用修正”，不采用直接丢弃错误需求的办法：删除一条需求可能同时破坏章节关联和验收覆盖。

空白、换行、Markdown 强调和反引号差异可以匹配，但最终发布的 `source_excerpt` 仍恢复为原 instruction 中真实、连续的原文。代码里的标识符、通配符和运算符保留；不会替换词语、数字、按键或用语义近似来放行虚构引用。

真正找不到依据时，反馈明确列出需求编号。正式入口允许一次额外模型调用，只返回这些编号的引用修正；其余需求、描述、章节和编号保持不变。修正仍失败时给出具体编号，不悄悄删需求或宣布完成。发生修正时保存 `grounding_audit.json`。

对本次真实失败回复重新检查：**R025 已由格式归一化匹配，38 条需求全部保留，额外模型调用为 0**。这只证明原文引用可恢复，需求语义、完整性和可玩性仍需 QA 审查。暂不更换 Designer 或其他角色的模型。

底层 `DesignerConfig.max_grounding_repairs` 默认 0、`ArtProducerConfig.max_quality_repair_rounds` 默认 0，兼容旧单次适配器；正式两种控制器分别显式启用 1、2。配置随实验冻结，请使用新的完整运行目录。

## 4. 壁钟修复与真实重放

在原游戏的独立副本中，将水晶绘制的

```gdscript
sin(Time.get_ticks_msec() * 0.005)
```

替换为

```gdscript
sin(float(tick_index) / float(PHYSICS_HZ) * 5.0)
```

复用游戏已有、会在关卡/循环重置的模拟 tick，保持每秒 5 弧度的视觉频率，不改玩法、不修改 Player、不屏蔽像素差异。只有该表达式改变，原始游戏与历史评测记录保留原样。

新增 `examples/replay_clock_repair.py`，使用原运行中最新 Player 的实际输入轨迹和同一个 Docker Player 驱动。每条轨迹在修复前/后分别做采集、两次全新进程重放；仍逐帧比较状态和像素摘要，并核对真实截图和状态快照。冻结本地镜像 ID、关闭容器网络、只读挂载工程、使用新的用户数据，禁止宿主执行回退和自动拉取镜像。所有请求、日志、截图、差异与源码补丁保存到新目录，不写入原共享状态或伪造新 QA 结论。

**2026-10-25：用户服务器真实重放已通过。** `clock-server-replay` 中修复前为 2/10，通过模拟 tick 修复后为 10/10。离线复核重新读取了 30 次会话、6,300 帧及 132 张检查点截图；修复后的 2,100 对帧状态/像素摘要全部一致。前后工程只有水晶计时表达式变化，各条采集轨迹的状态摘要未变。详见[服务器重放审阅](../runs/fixed-loop-developer-opt-001/diagnosis/clock-server-replay/analysis/review.md)。此前本机 Docker 不可用的记录属于上次开发阶段，不能再作为本次结果的结论。

本次仅确认这 5 条轨迹在记录的镜像、渲染环境中确定性通过，不能据此宣布所有关卡、美术、玩法或跨平台一致性都已验收。旧游戏的标题图尺寸问题仍可见；原始角色状态和 QA 结论没有改写。

现有结果无需重复启动引擎。增加的 `--verify-only` 可以在复制回来的结果上复核完整性，不启动 Docker、Godot 或模型，不跟随服务器日志中的绝对路径。新的分析目录不能已存在：

```powershell
$env:PYTHONPATH='src'
python examples/replay_clock_repair.py --verify-only --output runs/fixed-loop-developer-opt-001/diagnosis/clock-server-replay --audit-output runs/fixed-loop-developer-opt-001/diagnosis/clock-server-replay/analysis/recheck-001
```

该入口重新核对工程/原始请求、全部会话、事件、帧摘要、实际截图及汇总。缺失重放、改变输入种子、改变已测工程或错误的成功汇总都会被拒绝。隔离声明必须是布尔值，每帧必须有合法摘要。保存 `verification.json`，并保持原始服务器文件不变。

需要重新执行实际引擎时，在已有 Player 镜像和可用 Docker 的服务器使用新的输出目录，不需要模型 API：

```powershell
$env:PYTHONPATH='src'
python examples/replay_clock_repair.py --output runs/fixed-loop-developer-opt-001/diagnosis/clock-server-replay-002
```

Linux 使用 `PYTHONPATH=src python examples/replay_clock_repair.py --output runs/fixed-loop-developer-opt-001/diagnosis/clock-server-replay-002`。默认来源为 opt-001、镜像为 `gamecraft-player:4.5.1`，可用 `--player-image` 指定已有镜像。先跑单条可加 `--trace near_rewind_reset`，但只能证明这条轨迹。默认复跑全部已采集轨迹。`--prepare-only` 只准备副本，绝不标记为真实重放通过。

请查看输出目录的 `summary.json`：只有修复后全部比较通过，`real_replay_verified` 才为 true。若仍失败，保留该目录便于继续定位其他确定性问题。

## 5. 审阅与下一次完整运行

上次离线审阅保存在 `runs/fixed-loop-developer-opt-001/diagnosis/library-grounding-replay/`，包括 R025、真实 Kenney 导入与两种控制器的完整离线 I/O。本次用户提供的运行副本未包含该历史目录，未重建或冒充原有证据。当前可查看的[服务器重放审阅](../runs/fixed-loop-developer-opt-001/diagnosis/clock-server-replay/analysis/review.md) 保存本次重新核验的数据、来源摘要与回归结果。文本/图片回复为离线夹具的历史材料不能当作在线效果或性能结果。

完整运行继续由用户执行，实际 `.env` 模型与 DashScope 图片接口保持原样：

```powershell
python examples/fixed_loop_demo.py --godot-mcp --output runs/fixed-loop-library-first-001 --max-cycles 3 --max-role-calls 15 --max-model-calls 64 --max-wall-seconds 43200
```

运行前确认两个库在服务器实际可读。接下来重点核对素材来源比例、每张图的拒用/修复理由、图片调用数、Designer 是否仍因引用阻塞、最新 Player 重放和 QA 验收结果。
