# 结构化配色交接

Skill 先理解用户的语义配色，再提交可执行参数；后端使用确定性混色和对比度规则。不要把颜色决定只留在请求正文，也不要按某个行业名称固定套色。

## 新建普通图的三种选择

- 有整体配色、语义色调或多色主题：由 Agent 在用户表达／授权范围内选择合理 RGB，通过 `--base_palette` 提交。描述可以留在正文解释意图，执行色值放参数中。
- 没有整体配色偏好：显式传 `--use_default_palette`；只有局部高亮时可同时传 `--accent_targets`。
- 编辑已有图：沿用 `session_id` 或输入 CW 的已选配色，省略色板；默认开关不与已有图混用。更换整图配色使用新会话。

客户端会拒绝缺少选择的新普通图、空色板、未知字段、版本不支持、默认／选择冲突。无需另问用户要 Hex；常规视觉选择由 Agent 合理决定，只有真实歧义才澄清。

## 兼容单主色与预设

既有 `{"primary":"#RRGGBB"}`、`{"style_preset":"corporate_blue"}` 及已声明的版本化预设继续支持。版本化预设拥有角色颜色，不混用 primary。矩阵／其他 authoring 模板继续查询它们自己的预设能力。

## 通用双色与画布背景：base_palette version 1

```json
{"version":1,"primary":"#30564A","secondary":"#B67B35","background":"#F5F2E8"}
```

- version 必须为整数 1；primary 必填，secondary 与 background 可选；不与 style_preset 混用。
- primary 决定结构区域的色系；secondary 决定卡片边线与关系颜色，省略时使用 primary；background 决定整张画布，省略时为白色。
- 深色风格通过选定较暗 background 表达，程序按背景亮度派生卡片填充、字体对比度；不要另传未经声明的 tone/style/industry 字段。
- 色值使用 6 位 Hex；脚本可归一化支持的明确色名。阴影、字号和像素坐标不属于此协议。
- 局部高亮对象仍通过 accent_targets 表达；secondary 不意味着所有对象都必须被高亮。

执行前读取 `/capabilities.co_design.palette`：versions、fields 和 routes 必须覆盖所用字段及入口；脚本会再做同一检查。当前 version 1 支持联合生成（普通 general／OutlineIntent），不支持 authoring／专用 swimlane 入口。不得丢弃第二色或背景以绕过检查，不能未经授权切换业务结构／入口；可采用该入口已支持的预设，能力确实不足时明确说明。

## 提交前核对

把用户每条配色要求对应到整体字段或高亮对象：多色主题不能只留下其中一种颜色，深色不能只选一个深主色却遗漏背景。检查实际参数包含选择，不把自然语言理解假定为已传入。保存、续改、导出应沿用同一方案；用户未修改颜色时不重新选色。
