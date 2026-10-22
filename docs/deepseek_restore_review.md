# DeepSeek 恢复验证

2026-10-20，按用户确认的方式验证：文本真实调用 DeepSeek，图片保持 mock。阿里云配置注释保留；没有开发固定 Loop 或 Scheduler。

五个既有角色的真实 DeepSeek 集成检查均已通过。首轮 Designer、Art Producer 各发现一个协议问题；修复后单独重测通过，原始失败输出没有改写或删除。角色可运行不代表游戏已达到交付标准，当前 QA 仍判定 `ready=false`。

## 结果

| 角色 | 最终检查 | 实际结果 | 完整输入输出 |
| --- | --- | --- | --- |
| Designer | 通过 | 1 次真实调用，26 条需求、8 个设计章节 | [第二轮记录](../runs/deepseek-restored-roles-002/designer/review.md) |
| Art Producer | 通过 | 1 次真实规划调用，12 个 mock 图片资产；角色如实返回 `partial` | [第二轮记录](../runs/deepseek-restored-roles-002/art/review.md) |
| Developer | 通过 | 2 次真实模型调用、2 轮隔离 Godot 构建检查，最终构建通过；接入 12 个既有 mock 资产 | [首轮记录](../runs/deepseek-restored-roles-001/developer/review.md) |
| Player | 通过 | 3 次真实模型调用，7 个真实执行会话，其中 2 条新操作轨迹各重放 2 次；2 次匹配、2 次不匹配均有记录 | [首轮记录](../runs/deepseek-restored-roles-001/player/review.md) |
| QA Reviewer | 通过 | 2 次真实调用，覆盖 116 条验收条件、形成 9 个问题，交付就绪为 false | [首轮记录](../runs/deepseek-restored-roles-001/qa/review.md) |

Player 的检查验证了采集、重放和差异记录机制，不要求把真实重放差异改写为通过。QA 使用既有 Player 输入（10 个会话、5,406 帧、47 张截图），不是本次新 Player 的输出；当前模型原始结论经证据检查后，有效结论为 29 条 pass、87 条 insufficient，17 条 grounding notes 保留在 QA 报告中。这里没有官方评测分数或视觉验收通过的声明。

## 发现与修复

1. **Designer 引用换行丢失。** 首轮模型将原文跨行句子中的换行替换为空格，输出被严格子串校验拒绝。`designer.v3` 提供从原 instruction 直接拆出的逐行文本作为引用辅助，并明确要求保留换行；原 instruction、校验规则和模型原始回复均不改写。修复后的真实调用通过。
2. **Art 输出路径缺少命名空间。** 首轮模型返回 `sprites/...`、`tiles/...`，不符合既有 `assets/` 目录规则。`art-producer.v3` 在 JSON schema 中加入原有路径规则的显式 pattern 和说明，提示模型把简写转换为 `assets/sprites/...` 等项目相对路径。旧权限检查继续生效，修复后真实规划与 mock 图片生成通过。
3. **QA 的首次响应使用了无效枚举。** 既有有界修复机制把准确的字段错误反馈给模型，第二次响应通过，2 次消耗均被记录；没有新增隐式重试。

历史 `designer.v2`、`art-producer.v2` 产物仍可读取。Designer 在同一设计历史中要求配置一致，因此修订旧 v2 设计时仍须显式提供该历史的原 `DesignerConfig`；此次 v3 验证使用新运行目录，没有迁移或覆盖旧设计。

## 测试与用量

- 修复前全量离线回归：**321 passed、2 skipped、6 deselected**。两项跳过分别是当前用户的符号链接权限和需显式配置的 Docker Python 镜像测试；真实模型测试由单独命令执行。
- Designer/适配器修复后定向回归：**79 passed**。
- Art Producer 修复后定向回归：**38 passed**。
- 首轮真实角色测试：**3 passed、2 failed**；两处修复后重测：**2 passed**。这些结果是保留失败历史后的分批结果，不把首轮报告伪装成全通过。
- 完整角色测试全部尝试合计：**11 次真实文本模型调用，449,684 输入 tokens、99,257 输出 tokens，其中 311,424 输入 tokens 命中缓存；12 次图片调用全部为 mock。**
- 前置短请求另计：**5 次真实文本请求，9,704 输入 tokens、1,353 输出 tokens**；其中 Developer 的虚构原文引用被拒绝，记录保留。
- 阿里云连接失败记录单独保留，不计为成功模型调用，没有收到可用于核对 token 的服务端用量。
- 检查了本次运行和文档中的 801 个文本文件，未发现 `.env` 中保存的实际 API Key 或网关地址。

## 测试范围

- Designer：从原始 Time Loop instruction 产生真实设计输出。
- Art Producer：导入已有真实 Designer 产物，真实调用素材规划模型，生成 mock 图片。
- Developer：导入已有真实 Designer、Art 产物，真实调用代码模型，在隔离 Godot 中检查构建。
- Player：导入已有真实 Developer 游戏，真实调用操作规划模型，执行 Godot 操作、采集轨迹、独立重放。
- QA Reviewer：导入已有真实 Developer 与 Player 证据，真实调用 QA 模型进行验收。

这里是五个已有角色的独立集成回归，后四者复用各自示例已有的上游输入。新 Designer 输出没有自动串入新 Art，其他角色同理。完整控制器的贯通仍属于尚未开发的第 4 步。

模型提示词、输入、输出均为英文；Benchmark instruction 未改写。图片占位不能作为正式美术完成或交付就绪的证明。

## 原始记录

- [DeepSeek 短请求报告](../runs/model-config-deepseek-restored-001/report.json)
- [离线回归结果](../runs/deepseek-restored-regression-001.xml)
- [首轮完整角色测试结果，含失败](../runs/deepseek-restored-roles-001/tests.xml)
- [修复后的真实重测结果](../runs/deepseek-restored-roles-002/tests.xml)
- [Designer 与适配器定向回归](../runs/deepseek-restored-targeted-002.xml)
- [Art Producer 定向回归](../runs/deepseek-restored-art-targeted-002.xml)
- [首轮 Designer 失败的完整输入输出](../runs/deepseek-restored-roles-001/designer/review.md)
- [首轮 Art Producer 失败的完整输入输出](../runs/deepseek-restored-roles-001/art/review.md)
- [Developer 完整输入输出](../runs/deepseek-restored-roles-001/developer/review.md)
- [Player 完整输入输出](../runs/deepseek-restored-roles-001/player/review.md)
- [QA Reviewer 完整输入输出](../runs/deepseek-restored-roles-001/qa/review.md)

## 后续复跑

当前 `.env` 默认指向 DeepSeek。先按 [模型配置说明](model_configuration.md) 做短请求检查，再按各角色开发文档的 opt-in 命令执行完整测试，给每次测试设置新的输出目录。Developer 和 Player 需要既有隔离 Godot 镜像；图片服务仍由示例显式注入 mock。

阿里云配置暂不启用。用户后续要求启用并自行执行后，可提供输出目录以分析 `summary.json`、`role_result.json`、`model_calls/`、`tool_audit.jsonl` 和公开产物。模型连接或解析失败时同样应保留该轮目录。
