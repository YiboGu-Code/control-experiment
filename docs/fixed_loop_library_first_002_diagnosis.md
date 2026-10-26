# library-first-002：画面、动画与流程中断诊断

本次检查基于 `runs/fixed-loop-library-first-002` 的真实模型记录、素材清单、工程、Player 截图和重放日志。**这不是完成了三轮优化的最终版本**：运行约 215.4 分钟，只提交 `game-0001`，QA 超时后终止，未进入下一轮代码与交付轨迹修复。原运行 842 个文件已逐一校验，均未改动。

完整事实与离线验证在 [art-animation-review](../runs/fixed-loop-library-first-002/diagnosis/art-animation-review/README.md)。该目录的新测试和修复副本不冒充真实模型输出或正式验收。

## 为什么效果差

| 观察 | 日志/源码证据 | 影响与处理 |
| --- | --- | --- |
| 两个素材库都不可用 | `experiment.json.libraries.availability` 中 Kenney、OGA 均为 false；最终 13 项全是 generate | “library-first”在本次根本没有可用的库。新增调用模型前的挂载检查，默认至少一个库可用；路径由服务器宿主配置 |
| 生图请求彼此没有图像参考 | 原请求只有文字、style_guide，没有 image 输入 | 文本风格描述不等于角色身份约束。增加原生 DashScope 参考图输入及哈希、权限、数量、字节和预算约束 |
| 主角只有一帧 | `player_sprite` 只有 `player_idle`；`player_actor.gd` 创建静态 Sprite2D，只切 flip_h | 这是素材和集成合同均缺失，不能靠加大 token 自动变出动作。新增显式动作合同、帧资源组装、未接入检查及时间序列验收指引 |
| 人物巨大、角色与碰撞体不符 | 角色/幽灵统一写死 `scale=0.25`，碰撞体只有 34×68；纹理却保留原生大图 | 文件没有被拉扁，不代表屏幕尺寸正确。新增逻辑显示高度与脚底对齐要求，诊断副本按 68 单位等比适配 |
| 回溯后画面持续泛白 | `flash_timer=0.35` 后没有衰减；帧 120、300、599 仍为 0.35 | 是代码缺陷。副本补充 physics delta 衰减；共享诊断增加倒计时无更新的提示 |
| Developer 从未成功看图 | 三次 MCP `run_project` 均被 strict 扫描以 `Callable` 等原语拒绝；`developer_visual` 无成功调用 | UI 回调触发过于宽泛的静态阻断。MCP 改为保留扫描警告，继续保留容器隔离、启动确认、固定工具白名单 |
| 两次导入进程被杀 | build 1、4 的 import 返回 137、stderr=Killed；Developer 当时只有 1 CPU / 1024 MiB | 很可能与资源有关，但无 OOM 遥测，不能断言。新增明确资源诊断；三类引擎容器默认 2 CPU / 3072 MiB / 512 MiB tmpfs，可配置且记录实际值 |
| QA 两条引用错误拖垮整轮 | 102 条标准里 R002:0、R015:0 引用校验失败；重新发送整份上下文和所有截图，第二次等待约 65.4 分钟后取消 | 新增局部引用修复，锁定其余 100 条；修复失败的 JSON 退回保守审查结果。真实网络中断、预算和未知用量仍按原规则处理 |
| 流式请求超过配置时限 | QA 配置 timeout=1800 秒，但 SDK 的读超时不等于整个流的截止时间 | 增加覆盖完整接收过程的总截止时间；持续心跳不能无限续时。部分流仍留存，不猜测 token 用量、不自动重试 |

模型调用耗时（不含引擎和生图）：Designer 17.1 分钟，Art 文本规划 13.4 分钟，Developer 47.5 分钟，Player 21.3 分钟，QA 78.3 分钟。最大浪费来自失效的预览链路、重复修复和超大的整份 QA 重写，不只是“角色 prompt 太长”。

账本中的 7,416,555 输入 token **不是已确认消耗**。完成请求的提供方回执合计输入 1,025,023、输出 329,220；QA 第二次请求没有最终 usage，账本包含保守预留，`token_usage_complete=false`。不得据此直接核算费用或比较范式效率。

## 本次落地的改动

### 1. 库优先与失败恢复

`ART_LIBRARY_POLICY=any` 为正式入口默认值，要求至少一个库目录存在；`both` 要求两个；`optional` 明确允许无库实验。检查不证明目录中所有资源可用：逐项搜索、格式与许可证校验仍在 Art 工具链中执行。

`library_preflight.json` 保存宿主解析后的真实目录；原 instruction 中的容器路径不会自动替 Python 挂载服务器目录。`ArtManifest.library_availability` 保存工具实际返回值，使 QA 能区分宿主事实和模型文字说明。未使用 OGA 不等于缺失 OGA 许可证。

生图批次中途传输失败时，保留已经完成的素材，剩余任务记录 `production_failures`；不偷偷再发一次请求。首张即失败且没有可保留素材时仍失败退出。权限、预算、非法数据、取消不被吞掉。待生产内容不会通过 QA final_art 门槛。

### 2. 参考图与动作合同

支持原生 Qwen 参考图的配置会公开 `supports_reference_images=true`。Art 可以请求 `reference_asset_ids`，引用已存在或同批先完成的资产；宿主读取公开图像、校验 SHA-256，再以原始像素 Base64 传给 DashScope SDK。最多 3 张、合计 8 MiB，不接受模型 URL、宿主路径或密钥。OpenAI Images/不支持的模型不静默忽略引用。

一次质量修复仍计为一次新图像请求。同一资产有旧候选且提供方支持引用时，未显式指定引用也会带上旧图，配合新的修复描述。`ART_IMAGE_REFERENCE_IMAGES=false` 可显式禁用这一能力，Chat 调用接口不变。原始图像继续保留原生分辨率、无损 PNG，不做下采样或非等比变形。

演员采用 `sprite/sprite_sheet`，新增 `motion`：

```json
{
  "mode": "frames",
  "display_height": 68,
  "clips": [
    {"name": "idle", "frames": ["hero/idle"], "fps": 6, "loop": true},
    {"name": "run", "frames": ["hero/run_a", "hero/run_b"], "fps": 8, "loop": true},
    {"name": "jump", "frames": ["hero/jump"], "fps": 8, "loop": false}
  ],
  "implementation": "Use resettable simulation ticks, consistent foot pivots and uniform render scale."
}
```

上例是协议示意，不是本次已生成的角色动作。帧可来自同一图集，也可引用多个库文件 `asset_id/frame_name`。宿主拒绝不存在的帧，检查完全相同的动画帧，生成 `assets/animations/<actor>.tres` 和 `motion_contracts.json`。Developer 若只引用 idle、既未接入 SpriteFrames 又未引用全套帧，会收到修复反馈。动作合同进入 Developer 报告、Player 和 QA。

`procedural` 必须给出状态相关动作方案；`static` 必须有符合玩法的理由，例如棋子。它们不证明已经获得真正的肢体动画。模型仍需实现状态切换、统一比例与脚底锚点，并以重置后的模拟 tick 选择动画帧；不能依赖墙上时钟。哈希不同不证明姿态正确，载入资源也不证明动画被播放，实际运动仍需多帧试玩验收。

### 3. 按需技能与局部 QA 修复

Art 的正式配置启用技能目录：`asset_layout`、`actor_motion`、`reference_edit`。模型通过 `action=load_skills` 按需加载正文，布局细则不再无条件放进每次初始 prompt。请求占用原有模型轮次，最多三个固定技能，无任意文件读取或新增工具权限。必需的来源、质量、动作合同和权限约束始终保留在核心协议及宿主校验中。

Developer 沿用已有 context 读取接口，新增 `skill/actor_motion`、`skill/presentation_checks`，可与源码读取批量请求。它们是项目内的受控能力说明，不是自动安装外部 Codex/Claude 插件，也不运行外部脚本。技能解决知识按需提供，不能替代模型的视觉能力或验收证据。

QA 使用 `qa-grounding-patch.v1`，只返回出错的 criteria/issues，禁止改写有效行或全局 ready 决定。修复输入保留原 instruction、相关需求和有边界的证据/来源；没有重发截图，故不得新增视觉通过判断。缺少证据应 abstain。真实样本的修复文本由 499,501 字节降为 125,460 字节，约减少 75%；这不是 token 或端到端速度的实测提升。修复最多使用 8192 输出 token，完整首次审查的深度思考和上下文上限不变。

## MCP 与外部工具调研

以下来自官方文档，未安装、未付费调用，也没有据此宣称已经验证生成质量。

| 方案 | 适用环节 | 选择与边界 |
| --- | --- | --- |
| [PixelLab MCP](https://api.pixellab.ai/mcp/docs) | 像素角色、多方向、同角色动作与 tile | 官方提供角色创建和 `animate_character`，比通用插画生成更贴近游戏帧资产；适合下一步做小规模动画质量对照。需要账号、按动作/方向/尺寸计费，且像素风未必适合当前全部美术 |
| [ComfyUI 官方 MCP](https://docs.comfy.org/agent-tools/mcp) | 本地参考图编辑、固定生成工作流 | 能运行已有 workflow、查看任务、取回产物；适合有 GPU 且愿意维护模型/节点的服务器。建议只开放预先审查的工作流 ID，不把任意 workflow、节点安装和模型下载权交给角色 |
| [Aseprite CLI](https://aseprite.com/docs/cli/) | 帧标签、动画导出、PNG+JSON 图集 | 更适合确定性制作/整理现有动画。无需为“能叫 MCP”增加一层；可将可信 CLI 封装为公共执行器工具。它本身不解决创作质量 |
| [Qwen 原生参考图编辑](https://www.alibabacloud.com/help/en/model-studio/qwen-image-edit-guide) | 当前服务的角色变体、局部修图 | 官方示例支持 qwen-image-3.0-pro 的 image+text 输入，本次先复用现有服务补齐这条链路；服务器 token plan 的实际能力仍待真实调用确认 |

技能设计参考 [Anthropic 的渐进式加载说明](https://www.anthropic.com/research/equipping-agents-for-the-real-world-with-agent-skills)：先提供目录，相关时加载正文。本项目的取舍是保持五个角色及公共预算不变，把角色内能力拆成受控技能；不让自动加载的第三方说明改变权限、执行隐藏评测或绕过确定性重放。

推荐顺序：先确认库确实可搜索、MCP 能成功看到普通界面和实际关卡；再验证参考图与动作状态接入；若连续动作仍漂移，再以相同画风和动作要求对照 PixelLab/固定 ComfyUI 流程。不要在两个控制器中使用不同工具和美术条件后归因于调度效果。

## 当前游戏仍未达到可交付质量

原始 5 条轨迹的 10 次独立重放全部通过，旧模拟时钟修复在这些轨迹上有效。它们没有证明完整关卡解法、动画表现或所有玩法通过。

首次 QA 草稿还指出 `button_nodes` 没有被构造、按钮素材未用、HUD/暂停/结算面板布局越界、缺少交付 demo 及 scenes 目录等问题；这些草稿没有提交到共享状态，不能当作正式 QA 结论。源码和截图支持其中的具体问题，下一轮 Developer 应接收完整有效审查后修复。章节入口点击落在按钮之间、交互键离开拉杆范围、没有空中采样属于 Player 试验覆盖问题；不能据此宣称游戏按钮坏了。

本次另提供经过精确源码匹配的诊断补丁：白光衰减、按 68 单位等比适配角色/幽灵、隐藏未定位幽灵、章节标题插值。**没有伪造缺失的动作帧，也没有修好全部玩法。** 已准备的 `presentation-prepared` 只供查看源码差异，未执行真实引擎。

## 服务器复跑

先在服务器 `.env` 填实际宿主目录（下面两行需替换），不要修改 instruction：

```dotenv
ART_KENNEY_ROOT=/actual/server/path/to/kenney
ART_OGA_ROOT=/actual/server/path/to/opengameart
ART_LIBRARY_POLICY=both
ART_IMAGE_REFERENCE_IMAGES=true
```

资源可以通过 `DEVELOPER_SANDBOX_MEMORY_MB`、`PLAYER_SANDBOX_MEMORY_MB`、`GODOT_MCP_SANDBOX_MEMORY_MB` 调整；同样支持各自 `_SANDBOX_CPUS`、`_SANDBOX_TMPFS_MB`。提高限额不预分配内存，也不能保证宿主没有资源竞争。两组实验应使用相同设置。

此次改动涉及 MCP 镜像内驱动，**需要重新构建 MCP 镜像**，否则预检会报告镜像过期：

```bash
docker build -t gamecraft-godot-mcp:4.5.1-mcp3.8.1 configs/godot_mcp
python examples/fixed_loop_demo.py --output runs/preflight-art-motion-001 --godot-mcp --preflight-only
python examples/fixed_loop_demo.py --output runs/fixed-loop-art-motion-001 --godot-mcp --max-cycles 3 --max-role-calls 15 --max-model-calls 64
python examples/scheduler_demo.py --output runs/scheduler-art-motion-001 --godot-mcp --max-decisions 30 --max-role-calls 15 --max-model-calls 64
```

预检不会调用模型或执行游戏，但会检查本地 Docker 镜像和驱动。每次使用新的输出目录。64 是为包含修复/技能读取的多轮实验提供空间的建议，两组共享；不要与以前 32 次上限的运行直接比较效率。

若先想验证现有游戏的确定性外观修复，无需模型或重新生图：

```bash
python examples/replay_presentation_repair.py --run runs/fixed-loop-library-first-002 --output runs/fixed-loop-library-first-002/diagnosis/presentation-server-001
```

它从原始注册工程建立 before/after 副本，使用原来的五条真实输入，各执行 capture 与两次 fresh replay，保存截图、状态、日志、差异和摘要；严格像素判据不变。不要把之前生成的 prepared 目录当作这个命令的输出目录。下一次可直接提供上述结果文件夹继续分析。
