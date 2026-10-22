# Fixed Loop 开发验证记录

任务使用资源快照中的 `platformer-time-loop/instruction.md`，原文 SHA-256 为 `dcb85ce30a25d88a5e0a12b6a0f91a8783e896d6816cdcb88d6b8432e5c10969`。运行从空共享状态开始，五个角色的上游产物在同一次运行内生成，没有导入旧角色示例，也没有读取隐藏评测资料。

实现与服务器命令见 [Fixed Loop 说明](fixed_loop.md)。下面两次历史运行使用 DeepSeek 的真实文本调用和 mock 图片。入口现已支持默认读取 `.env` 的真实生图配置；此项改动只做离线测试，不应把下面的记录当作真实生图的在线验证，历史运行目录也没有改写。

## 第一份真实联调

完整目录：[runs/fixed-loop-deepseek-001](../runs/fixed-loop-deepseek-001/review.md)。

配置固定为最多两轮、20 次模型调用。第一轮 Designer 回复遗漏八个设计章节，宿主拒绝提交，其余依赖角色按规则跳过；第二轮 Designer 重新调用成功，随后 Art、Developer、Player、QA 全部执行。这个失败及其消耗没有删除，也没有在同一固定槽位无限重试。

| 检查 | 实际结果 |
| --- | --- |
| Designer | 36 条需求；初次拒绝、第二次提交成功 |
| Art | 13 张 mock 图片 |
| Developer | 真实构建第一次失败、模型修复后通过；发布 `game-0001` |
| Player | 10 次真实引擎会话：1 次探测、3 次采集、6 次独立重放 |
| 重放 | 6/6 状态和像素逐帧一致；采集长度为 420、300、600 帧 |
| QA | 检查 84 条验收项；1 条需求通过、2 条失败，其余尚未验证通过 |
| 结束 | `iteration_limit`，导出当前游戏，`delivery_ready=false` |
| 用量 | 9 次真实模型调用；输入 349,562 token，输出 84,196 token，其中缓存输入 57,472 token；13 次 mock 生图 |

普通标题到关卡的操作、章节菜单及命名场景入口已产生真实证据；未充分覆盖的重绕、幽灵互动、叠加、结算和重置等行为仍由 QA 保留问题。由于第一轮设计失败占用了一轮，这份两轮运行只产生一个游戏版本，尚未来得及把 Player 轨迹装入下一版工程。因此其交付副本没有被额外补入未经验证的 demo 文件。

附加目视抽查：[普通试玩第 419 帧](../runs/fixed-loop-deepseek-001/workspace/public/artifacts/player/f9fa53851f0f0068b38592ff/sessions/title_play_level1-capture/frame_0419.png) 可见 mock 图块和 HUD 上沿的文字挤压/裁切。此观察仅作为运行后的开发审阅备注，没有写回生成状态或冒充当时 QA 文本模型的视觉证据。

`implementation_snapshot/` 保存了这次运行对应的实现源码，匹配其配置内的实现哈希。该记录使用的是本次开发早期的固定 Loop 版本；后续改进独立保留新运行，不改写旧账本或模型回复。

## 第二份真实多轮联调

完整目录：[runs/fixed-loop-deepseek-002](../runs/fixed-loop-deepseek-002/review.md)。

配置固定为最多三轮、32 次文本模型调用。第一轮五个角色均执行，第二轮按规则跳过没有待办的 Designer 和 Art，Developer 处理问题并装配真实轨迹，Player 对新版本重新检查。第二轮 QA 的后续模型请求最终被截止时间取消；由于该请求未返回完整用量，运行按规则停止，没有继续第三轮或自动重试付费调用。

| 检查 | 实际结果 |
| --- | --- |
| 输入 | 原始 instruction 哈希不变；实际模型输入输出未发现中文内容 |
| 设计与素材 | 30 条需求，12 张 mock 图片 |
| 最终选中项目 | 最新已提交的 `game-0002`，构建通过，副本位于 `delivery/game/` |
| 轨迹装配 | `demo_outputs/demo_01.json`、`demo_02.json` 均逐字节等于 `game-0001` 上实际采集的输入 |
| 新版本试玩 | 7 次真实引擎会话：1 次探测、2 次包内轨迹采集、4 次独立重放 |
| 新版本重放 | 4/4 状态与像素逐帧匹配 |
| QA 宿主核验 | 发给 QA 的实际输入中 `current_build_passed`、`current_play_evidence`、`packaged_traces_replayed` 均为真；`final_art` 为假 |
| QA 证据完整性 | 已核验 2,346 帧、14 张截图、78 个产物，原始记录在 QA 输入中 |
| 最终验收 | **未完成**；当前 `readiness=null`，`delivery_ready=false` |
| 结束 | `failed`，原因是取消请求的 token 用量未能确认 |

最新已提交的 QA 报告属于 `game-0001`，检查了 76 条验收项，**不能作为 `game-0002` 的验收结论**。新版本的宿主证据检查通过，也不等于语义验收通过。失败调用的输入、已返回的中间回复、取消元数据和保守账目均保留；没有伪造最后一个模型输出。

用量分两部分记录：

- 14 次完整返回的模型请求：输入 925,910 token，输出 126,465 token，缓存输入 125,696 token。
- 1 次未取得用量的请求：`model_calls/qa_reviewer/003/`，保存了输入及取消元数据。账本保留这次请求的资源预留，因此 `summary.json` 的输入 1,272,737、输出 150,465 是**含未核销预留的保守计数**，不是供应商确认的实际消耗。`token_usage_complete=false`。

两份开发运行合计发起 24 次文本请求，其中 23 次有完整返回，1 次用量未确认；图片生成合计 25 次，全部为 mock。

第二轮 Player 的包内采集与重放成功，但其四次模型回复连续遗漏一条 `gameplay_concern` 的 `session_ids`，被拒绝后未执行新的探索试验。已据此改进反馈：明确指出错误发现的下标、类别和缺失字段，并提醒当前剩余试验数。该反馈改动另作离线回归验证，未改写本次原始回复，也尚未再次进行付费实测。

## 多轮实现的补充

- 待办资格判断抽取为公共 `WorkPolicy`，供未来 Scheduler 复用。
- 设计变化后，Art 可以明确返回 `no_op` 保留原素材，随后 Developer 继续；不强迫重新生图。
- 默认最多装配 2 条已采集轨迹，Player 每次最多 3 条试验，保留后续探索名额。
- 新探索轨迹会进入下一轮 Developer 待办；按固定顺序优先装配新输入，再保留旧输入，不参考隐藏分数。
- 轨迹在新项目内重新采集并重放；源码、素材或轨迹被执行过程改写时拒绝发布。
- 上次调用的安全错误说明会进入下一轮角色输入；取消时补齐已记账调用的结果和检查点。

## 测试说明

离线测试用真实 Benchmark instruction、五个实际角色实现和公共执行器，文本与渲染边界使用明确标记的测试夹具。覆盖两轮交接、同一游戏版本的打包/重放/QA、新探索回流、素材保留、预算拒绝、未知用量、失败重试、取消、配置冻结、已完成调用恢复、最终副本哈希和秘密信息不落盘。

最终固定 Loop 测试组为 **18 passed**。其中包括素材保留、新探索回流和失败反馈测试。

全量离线回归执行结果为 **344 passed、1 failed、2 skipped、7 deselected**。唯一失败来自新测试读取 Designer 输入的字段路径写错：实际位置为 `input.request.objective`；修正后单项复测 **1 passed**，随后整个固定 Loop 测试组复测 **18 passed**。没有遗留的测试失败。两项跳过分别依赖 Windows 符号链接权限和显式指定的旧 Python 沙箱镜像；七项 `live` 测试未包含在离线命令中。上文两份真实运行由独立入口执行，不能说是这七项 live 测试全部通过。

实测后改进 Player 的证据引用反馈，并定向复测缺失引用、协议修复和检查点边界：**3 passed、32 deselected**。新用例确认错误定位到具体发现，模型修正后可继续，且被拒绝的试玩计划没有执行。运行入口的 `--help` 也已检查。

这些检查验证流程及证据关联，不构成官方 Benchmark 分数，也不能替代玩法和视觉质量验收。

## 真实生图接入的离线验证

本次把 Fixed Loop 默认图片服务改为读取 `.env` 的 `ART_IMAGE_MODEL`，增加独立图片网关覆盖项，保留显式 `--mock-images`。本机只验证配置解析，读出的图片模型为 `qwen-image-3.0-pro`；没有发送在线请求，也没有修改 `.env` 或上面的历史运行产物。

新增图片接口与入口集成测试最终结果为 **35 passed**：

```sh
python -m pytest tests/test_image_provider.py tests/test_fixed_loop_images.py -q
```

覆盖 base64 与 HTTPS URL 返回、无鉴权下载、原始图片存档、PNG 转换、精确尺寸、色键及原生透明、失败不降级、超时与取消留痕、失败生图计费、默认真实图片服务、无网络预检以及真实 Art/Developer 之间的产物交接。测试使用真实 Benchmark instruction、离线 HTTP 响应和引擎夹具，不声称在线生图成功。

同次运行的既有 `test_fixed_loop.py`、`test_provider.py`、`test_art_producer.py` 共 **100 项通过**。首次合跑时，新增测试有三项因 Windows 默认 GBK 读取 UTF-8 审阅文件而失败；补齐测试的显式编码后，上述新增测试组全部通过，无遗留失败。入口 `--help` 和 Python 编译检查通过。

## DashScope 原生图片接口适配（2026-10-21）

用户实测指出图片模型必须使用 DashScope 原生接口，因此 Fixed Loop 默认图片服务改为 `DashScopeImageProvider`。当前配置解析为 `wan2.7-image`，并从阿里云共享兼容地址推导同地域的 `/api/v1` 原生地址。文本角色继续使用原有 Chat Completions；对比改动前后的哈希，`.env` 和 `providers/openai_compatible.py` 均保持不变。

原生调用使用可取消的 `AioMultiModalConversation.call`，每次显式传入图片专用地址、密钥和期限；不改变 SDK 全局地址或密钥。原始 SDK 响应中的 request ID、错误、usage 与图片输出均可在原有调用目录中审阅。API 协议、安装及配置项见 [运行说明](fixed_loop.md#生图配置)。

以下离线测试合计 **106 passed**：

```sh
python -m pytest tests/test_dashscope_image.py tests/test_image_provider.py tests/test_fixed_loop_images.py tests/test_provider.py -q
```

其中原生接口测试实际执行已安装的 DashScope SDK 1.25.17，在 HTTP 发送层替换网络响应，确认生成地址、原生请求体、星号尺寸、单张输出、SDK 会话关闭及全局配置不变。其余测试覆盖原生错误/超时/取消、凭据优先级、地区地址推导、PNG 处理、公共预算、Art/Developer 交接及 Chat 回归。入口帮助与编译检查也通过。没有在线调用图片或文本模型，没有改写此前运行目录；此结果不代表已经在用户端点完成真实生图。
