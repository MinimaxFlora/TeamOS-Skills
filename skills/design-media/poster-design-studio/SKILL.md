---
name: poster-design-studio
description: "将一段活动描述、一张产品照片或品牌参考图，转化为抓人眼球的活动海报、宣传 banner 或社交媒体配图。这款 AI海报设计与制作工具生成图片丰富的海报，在海报排版上搭配清晰视觉层级、按品类匹配的风格和预留文字区域，覆盖活动、音乐节、电影、产品发布和促销场景。你可以把它当作 AI宣传单制作工具来制作促销宣传单，当作活动海报设计工具，当作电影海报生成器，当作音乐节海报设计工具，当作产品发布会海报的 banner设计 AI，或当作平面设计海报工作室。从活动主题出发，用品牌风格参考图变换产品或场景照片，或在已认可草稿上做定向精修，完成可印刷或可分享的成品。"
---

# 海报设计工作室

将一段活动描述、一张产品或场景照片，或已认可的草稿，转化为抓人眼球的海报、宣传单或宣传 banner。复用对话中已经明确的决定，沿最短路径完成用户需要的海报。

## 选择制作路线

- **主题生成海报:** 当用户描述了一场活动、促销、电影、音乐夜、产品发布或社交活动方案，且不需要源照片时，把简报转化为完整的海报视觉，使用 `beatra.images.generate`。这是主题驱动的默认选择。
- **照片变海报:** 有一张产品、场景或品牌照片时，将其提升为精致的海报或 banner，搭配清晰视觉层级、按品类匹配的风格和预留文字区域，使用 `beatra.images.transform`。有源照片时这是默认选择。
- **精修已认可草稿:** 使用 `beatra.images.edit`，把已认可海报作为 `images[0]`，修复色彩、构图、文字区域或背景，不改变整体布局。
- **把定稿海报做成短视频:** 用户认可一张要在屏幕上展示的海报后，提议用它做一条 5 秒的动态海报，使用 `beatra.videos.animate`，作为单独确认的一次付费调用。参见下一步（references/next-step.md）。

精确路线参见海报路由（references/poster-routing.md），把需求转化为符合海报类型与品类的视觉规格时参见海报工艺（references/poster-craft.md）。

## 形成一份海报简报

复用用户已经给出的活动主题、海报类型、目标受众、风格偏好和视觉参考。只有缺失决定会实质改变结果时才询问。标准海报优先按发布场景建议画布——A 系列印刷、社交媒体比例或标准海报比例——并搭配干净的预留文字区域用于大标题。

简报围绕以下内容展开：

- 海报类型和品类——活动海报、宣传 banner、音乐或演唱会海报、电影海报、产品发布会、促销或宣传单，或社交媒体配图；
- 大标题信息——活动名称、电影片名、促销优惠或活动文案，作为构图的视觉锚点；
- 一个焦点主体——表演者、产品、主视觉场景或关键画面，作为海报的视觉锚点；
- 一个风格方向——干净未来感（科技）、温暖诱人（美食）、杂志大胆（时尚）、活力四射（音乐）、专业干净（企业）或亲和友好（教育）；
- 画布和发布场景——A3 或 A4 印刷、1:1 方图、9:16 快拍、16:9 banner，或 2:3 / 3:4 标准海报；
- 预留文字区域偏好——上部、居中或下部——方便后续放置大标题和详情；
- 有序视觉参考（品牌色彩参考、风格灵感、构图参考），有源照片时源照片始终在第一位。

用户已经说明主题或风格时直接复用。确实缺少这项决定时，给出最合适的默认建议，并把它合并进一次付费调用确认。

## 准备调用

所有远程 MCP 操作只能使用本 Skill 随包 `scripts/mcp_client.py`。工具名称作为 CLI 参数，工具参数以 JSON 通过标准输入传入。不要配置或调用宿主 Beatra Connector，也不要使用 REST/OpenAPI 作为回退。精确命令和排错参见随包 MCP Client 连接诊断（references/mcp-connection.md）。

- 主题驱动概念时，调用 `beatra.images.generate`，设置匹配发布场景的画布，prompt 捕捉期望的氛围、色彩、视觉层级和预留文字区域。
- 先通过随包客户端辅助命令上传源照片，再调用 `beatra.images.transform`，把上传的 artifact 作为第一个有序参考。在 prompt 中显式标注照片的角色，确保模型保持主体不变。
- 精修已认可草稿时，调用 `beatra.images.edit`。局部修改最多使用两个归一化 `edit_regions`，指定 `image_index=0`；全图调整则省略区域。

上传只让远程工具取得文件字节，本身不等于图片检查。只复查宿主确实能够看到的视觉事实。

除非用户明确选择其他设置，否则保持 `model=auto` 和 `count=1`。只有模型、可用性、兼容性或价格确实影响决定时才调用 `beatra.models.list`。完整请求结构与示例参见工作流（references/workflow.md）。

## 确认并只执行一次

策划和简报准备免费。付费图片调用前，展示并固定最终提示词、有序参考图、画布、海报类型、风格方向、预留文字区域、模型、控制项和输出数量。仍然缺失的高影响决定应合并到这一次确认中。

获得确认后，为这一个精确的逻辑请求创建一个稳定且不透明的 `client_request_id`，并只提交一次。提示词、参考图或顺序、画布、海报类型、风格方向、模型、数量或控制项任一变化，都是新的付费工作，需要新的确认和新标识符。

## 跟进、复查与交付

收到 `task_id` 后，只用 `beatra.tasks.get` 轮询该任务。如果标识符丢失，使用 `beatra.tasks.list` 查找候选并用 `tasks.get` 验证选中项。只有原始响应状态确实未知时，才能用完全相同的参数和同一个 `client_request_id` 进行幂等恢复。轮询缓慢、更新失败、授权失败或连接失败都不能创建替代性付费任务。

只有在用户主动要求时才使用 `beatra.tasks.cancel`。取消返回 `409` 时，继续跟踪原任务。完整恢复合同参见复查与恢复（references/review-and-recovery.md）。

结果可见时，依次复查视觉冲击力（海报是否抓人眼球、一眼可读？）、文字可读性（是否有干净的预留文字区域用于大标题和详情？）、品牌一致性（色彩和氛围是否符合品类与品牌方向？）、画布适配（印刷或社交比例是否正确？）和整体工艺。交付 artifact 链接、实际尺寸、任务标识符和 `billing.net_charged_credits`。最多提供一次聚焦的、未执行的精修建议。海报已获认可、也没有值得提的精修时，改为提出下一步（references/next-step.md）里的那一个下一步，整段对话最多一次。生成的素材也可以在 [www.beatra.cn](https://www.beatra.cn) 查看和管理。

工具返回积分不足时：把工具返回的原文整段交给用户，链接一个字不改。其余可以译成中文。不要改写成自己的充值说明。引导用户按原文去充值。

用户问还剩多少积分时，调用 `beatra.wallet.get`。问扣了什么时，调用 `beatra.wallet.ledger`。两者只读。不要发明账户余额或充值工具。不要把 `wallet.get` 当成每次付费提交前的必做步骤。

## 按任务查阅参考文档

- 在主题生成、照片变换和草稿精修之间选择，或针对特定海报类型规划：海报路由（references/poster-routing.md）
- 把需求转化为海报规格——构图、视觉层级、品类风格、色彩、预留文字区域和画布尺寸：海报工艺（references/poster-craft.md）
- 每条路由的精确请求结构、有序参考标注和 JSON 示例：工作流（references/workflow.md）
- 遗失任务、轮询缓慢、取消、结果复查或规划精修：复查与恢复（references/review-and-recovery.md）
- 海报获认可后，选择并提出一个下一步：下一步（references/next-step.md）、提出一个下一步（references/next-step-offers.md）；把海报做成动态海报：让交付的图片动起来（references/animate-a-delivered-image.md）
- 首次安装或认证过期：安装与认证（references/installation-and-auth.md）
- 随包 MCP Client 命令与诊断：随包 MCP Client 连接诊断（references/mcp-connection.md）
- 安装注册：安装注册（references/installation-registration.md）
- 任务查询、轮询与结果字段：任务与结果（references/tasks-and-results.md）
- 余额、校验与结构化错误：计费、错误与恢复（references/billing-errors-and-recovery.md）
- 断开安装连接：卸载与断开（references/uninstall-and-disconnect.md）
- 更新与安全：更新与安全（references/updates-and-security.md）

## 安装与账号操作

首次使用和共享操作参见安装与认证（references/installation-and-auth.md）、安装注册（references/installation-registration.md）、任务与结果（references/tasks-and-results.md）、计费、错误与恢复（references/billing-errors-and-recovery.md）以及卸载与断开（references/uninstall-and-disconnect.md）。

## 更新与安全

本 Skill 会自动更新。内置客户端每天最多一次从 Beatra 官网读取本渠道的版本信息
（中国大陆网络走 www.beatra.cn 与 cdn.beatra.cn）。有新版本时，它下载官方发布包，
逐个文件校验 SHA-256，再替换本 Skill 自己的文件；你放进这个目录的其他文件不会被
改动。替换过程中断时，下次运行会自动恢复到替换前的状态。除此之外，它不下载或执行
任何其他代码，只通过 HTTPS 与 Beatra 官方服务通信；你选择上传的文件只会传到
Beatra 为这次上传签发的一次性地址。

关闭自动更新：`python3 scripts/mcp_client.py update --auto off`。关闭后，客户端每天
最多提示一次在你安装它的商店里更新的命令。详见更新与安全（references/updates-and-security.md）。
