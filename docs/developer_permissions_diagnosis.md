# Fixed Loop / Scheduler：Developer 持续 partial 的诊断与修复

分析日期：2026-10-22。只读取用户保存的真实调用；本次没有在线模型或图片生成请求，没有改动 `.env`、Benchmark instruction 或原始运行目录。

## 检查的真实运行

本次读取的是嵌套在 `runs/art-producer-live-002/` 下的两组新结果，不是顶层同名的旧测试。

| 运行 | 已提交版本中的构建尝试 | 当前记录 |
| --- | --- | --- |
| `runs/art-producer-live-002/fixed-loop-mcp-thinking-001` | game-0001：6 次；game-0002：7 次，均失败 | 第三轮 Developer 已开始但未记录完成；缺少最终 summary，不能视为完整结束的实验 |
| `runs/art-producer-live-002/scheduler-real-images-001` | game-0001：6 次；game-0002：8 次，均失败 | 第三次 Developer 调用被取消；最终 termination 为 cancelled；Player/QA 未运行 |

四份已提交的 Developer 报告都没有 MCP 检查，`self_review_completed=false`。报告内至少 27 次构建尝试停在导入阶段。它们不能用于得出 Fixed Loop 或 Scheduler 的质量优劣结论。

原始日志典型错误：

```text
ERROR: Could not create directory: 'res://.godot'.
ERROR: Could not create project data directory (res://.godot) in: res://
ERROR: Cannot save project metadata to file 'res://.godot/editor/project_metadata.cfg'.
ERROR: Cannot open file from path 'res://assets/...png.import'.
```

模型随后多次修改缓存目录设置，又得到 `res://godot` 的相同写入错误。缓存目录名无法改变宿主机权限。

## 三个问题

1. **输出子目录和已有文件不可写。** 历史代码仅将 invocation 的暂存根目录设为 `0777`；宿主新建的 `game/`、素材目录和文件仍受默认权限影响。容器 UID 为 `65534`，不能在其他宿主用户拥有的 `0755` 目录中创建缓存。两组运行的实现快照均没有用户后来新增的递归 chmod。因此这些日志不能证明那段新增修改无效。
2. **模型的缓存路径补丁造成新的源码完整性问题。** 两组最新 `project.godot` 都包含 `config/use_hidden_project_data_directory=false`。修复权限后，真实导入和启动可以通过，但新增的 `godot/editor/*.cfg` 等文件会进入源码检查，造成 `source_integrity` 失败。需恢复默认 `.godot`，而不是忽略源码变更检查。
3. **v4 完成状态保留了过期的 partial。** 早期补丁为等待预览而报告 `partial`，即使后来通过当前版本的自检并 `finish`，原逻辑仍读取最后一份补丁的 partial 标记。这是额外的潜在问题；本次用户运行尚未进入 MCP，因此它不是原始 27 次导入失败的原因。

`partial` 是角色报告，不是 Player 的硬门槛；当前游戏的构建通过后，即使尚有剩余工作，也可以进入实际试玩。最终交付仍需要 Player 重放和 QA 验收。

## 修复范围

- 公共沙箱先校验整棵输出树再修改权限，避免递归 chmod 跟随链接改变输出目录之外的权限。目录 `0777`，文件允许共同读写；公开输入保持只读挂载。
- 固定启动脚本检查容器真实写入能力。命令结束时，将本容器用户拥有的新文件和目录开放给宿主，处理 Godot 显式创建 `0755` 目录的情况。不会把游戏进程改为 root，也不开放网络或隐藏评测目录。
- 如果权限仍不允许写入，返回 `sandbox_filesystem_unwritable`，公共执行器终止运行并保留账目和未提交草稿，不再消耗多轮模型修补权限。
- v3/v4 静态检查拒绝缓存位置/可见性覆盖项，提示模型删除这些配置并保留 `.godot`。没有静默修改模型提交。
- v4 只有成功的当前版本自检才能采用最后 `finish.remaining_work` 的声明。真实未完成工作、mock 美术、过期或失败的自检仍会阻止 completed；QA 权限不变。

## 离线实测材料

材料位于 `runs/developer-permissions-review-001/`，审阅入口为其中的 `review.md`。

| 子目录/文件 | 内容 |
| --- | --- |
| `original_run_analysis.json` | 原始报告位置、各版本失败次数、错误摘录、控制器检查点 |
| `posix-permissions-final/` | 真实 Linux tmpfs + 不同 UID 的权限复现，修复前后 Godot 导入、已有 sidecar 更新、第二次导入、宿主缓存写入及参数原样传递检查 |
| `fixed-original-final/`、`scheduler-original-final/` | 最终沙箱修复后的原项目副本：引擎检查通过，但可见缓存触发源码完整性失败 |
| `fixed-restored-cache/`、`scheduler-restored-cache/` | 明确记录删除两条缓存覆盖设置的诊断副本：引擎、源码和素材完整性检查通过，原项目摘要未变 |
| `mcp-repair/` | 真实本地 Godot/MCP 两次预览及修复，完整输入输出；文本/视觉模型使用明确的离线夹具，美术保持 mock |
| `player-capture/` | 真实 Player 输入和渲染截图，原工程摘要未变 |
| `pytest-offline.xml` | 全量默认测试的机器可读报告；默认不启用在线模型测试 |

早期 `fixed-game-0002/`、`scheduler-game-0002/` 仅做引擎检查，后续加入源码与素材完整性检查后由 `*-original-final/` 取代。`posix-permissions/` 保留首次发现 Godot 显式 `0755` 的失败复现，最终修复记录为 `posix-permissions-final/`。不删除失败证据。

这些检查证明权限与流程修复有效，不证明两组游戏已经满足 instruction、画面质量合格或 QA 就绪。

最终默认回归：**516 passed，16 skipped**，无失败。跳过项为显式启用的在线/本地引擎测试及当前 Windows 的链接权限条件测试；其中本次另行启用的 Linux 权限、真实 MCP、真实 Player 三项全部通过。权限与进程生命周期专项合计 10 项通过。XML 和 `verification.json` 保存实际计数，不把重复执行的测试累加为新增覆盖。

## 用户复跑

迁移更新后的 `src/` 和 `examples/`。现有三个镜像已具备启动脚本所需工具，这次无需重建。使用新的输出目录，两条命令的共同角色调用和模型调用上限保持一致：

```sh
python examples/fixed_loop_demo.py --output runs/fixed-loop-permissions-002 --godot-mcp --max-cycles 3 --max-role-calls 15 --max-model-calls 64
python examples/scheduler_demo.py --output runs/scheduler-permissions-002 --godot-mcp --max-role-calls 15 --max-decisions 30 --max-model-calls 64
```

以上命令仍使用 `.env` 的真实 Chat 与图片模型，不包含 `--mock-images`。两种控制器的角色/引擎/预算配置由同一入口生成。若改变配置，需对两种范式共同应用并核对 `comparison_conditions_sha256`。

也可以先不调用模型，复查已保存游戏。以下命令仅在新副本中运行本地 Godot；`--restore-default-cache` 会把精确的配置修改记入 `diagnostic_patch.json`：

```sh
python examples/recheck_saved_developer.py --run runs/art-producer-live-002/fixed-loop-mcp-thinking-001 --output runs/fixed-engine-recheck-002 --restore-default-cache
python examples/recheck_saved_developer.py --run runs/art-producer-live-002/scheduler-real-images-001 --output runs/scheduler-engine-recheck-002 --restore-default-cache
```

`result.json` 保存原始报告位置和项目摘要、引擎命令与完整 stdout/stderr、源码/美术完整性和原目录未改变的检查。可用 `--revision game-0001` 选择旧版本。不传 `--restore-default-cache` 则原样检查。此脚本不会写入正式运行账本，也不生成 QA 或重放证据。
