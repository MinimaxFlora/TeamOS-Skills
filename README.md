# TeamOS Skills

团队自用的 AI Agent 技能仓库。每个技能一个独立目录、一份 `SKILL.md`，按 10 个业务场景分类，可直接被 TeamOS / Hermes Agent / Claude Code / Codex 等支持 Agent Skills 的运行环境加载。

- 技能总数：**20**
- 分类数：**10**（其中 5 个分类待补充）
- 最近更新：2026-10-29

## 分类一览

| # | 分类 | 目录 | 技能数 |
| - | ---- | ---- | ------ |
| 01 | 办公效率 | [`skills/office-efficiency`](skills/office-efficiency) | 10 |
| 02 | 内容创作 | [`skills/content-creation`](skills/content-creation) | 4 |
| 03 | 开发编程 | [`skills/development`](skills/development) | 2 |
| 04 | 数据分析 | [`skills/data-analysis`](skills/data-analysis) | 1 |
| 05 | 设计多媒体 | [`skills/design-media`](skills/design-media) | 3 |
| 06 | 知识管理 | [`skills/knowledge-management`](skills/knowledge-management) | 0 |
| 07 | 商业运营 | [`skills/business-operations`](skills/business-operations) | 0 |
| 08 | 教育学习 | [`skills/education`](skills/education) | 0 |
| 09 | 行业专家 | [`skills/industry-experts`](skills/industry-experts) | 0 |
| 10 | 生活服务 | [`skills/life-services`](skills/life-services) | 0 |

## 技能清单

### 办公效率

| 技能 | 说明 | 版本 |
| ---- | ---- | ---- |
| [wps-office-suite](skills/office-efficiency/wps-office-suite) | WPS Office 全家桶：四引擎（WPS / MS Office / LibreOffice / 纯 Python）文档处理，覆盖 Word、Excel、PPT、公文排版、批量水印、跨文档检索 | 5.3.5 |
| [office-automation-pro](skills/office-efficiency/office-automation-pro) | 办公流程自动化：文档处理、数据整理、邮件管理、日程安排、报表生成 | 1.0.0 |
| [excel-auto-zh](skills/office-efficiency/excel-auto-zh) | Excel / WPS 表格自动化：openpyxl 建报表、解析 xlsm、批量合并工作表 | 1.0.0 |
| [gongwenformat-pro](skills/office-efficiency/gongwenformat-pro) | 党政机关公文标准排版（GB/T 9704-2012）：Markdown / TXT / DOCX 转标准公文 Word | 1.4.0 |
| [moways-doc](skills/office-efficiency/moways-doc) | Word 报告自动排版：页面、字体、标题层级、页眉页码、三线表、目录，并修复中英标点混用 | 2.7.4 |
| [official-document-skill](skills/office-efficiency/official-document-skill) | 中文公文写作：起草、改写、润色、降 AI 味、格式校核，覆盖通知、报告、请示、讲话稿等文种 | 1.0.3 |
| [ppt-win32com-editor](skills/office-efficiency/ppt-win32com-editor) | 用 win32com 就地修改已有 PPT，保留原布局、图片、形状与动效，不重建文件 | 1.0.0 |
| [ppt-generator-skill](skills/office-efficiency/ppt-generator-skill) | 按主题、行业、风格生成 PPT，支持模板与自动配色，中英双语 | 1.0.2 |
| [ai-ppt-skill](skills/office-efficiency/ai-ppt-skill) | PPT Master：20 种图表类型、5 阶段交互流程，产出可直接编辑的 pptx | 1.0.0 |
| [666-v2](skills/office-efficiency/666-v2) | 轻量 PPT 生成器：需求收集 → 页面结构 → 出稿，适合快速成稿 | 1.0.0 |

### 内容创作

| 技能 | 说明 | 版本 |
| ---- | ---- | ---- |
| [libai](skills/content-creation/libai) | 李白：中文文本润色与去 AI 味，规则检测 + 分层改写 + 质控流水线 | 1.0.5 |
| [humanize-ai-text](skills/content-creation/humanize-ai-text) | 论文 / 期刊稿降 AI 率改写，守住数字、DOI、术语，支持 docx / pdf 批量核对 | 4.9.0 |
| [unclecheng-reduce-ai-perception-v2](skills/content-creation/unclecheng-reduce-ai-perception-v2) | 去除 AI 写作痕迹：高频词、过度结构化、假靶子、机械化连接词等问题检测与修复 | 1.0.5 |
| [douyin-copy-extract](skills/content-creation/douyin-copy-extract) | 抖音 / 快手 / 小红书 / 视频号链接一键提取标题、简介与口播文案 | 1.0.0 |

### 开发编程

| 技能 | 说明 | 版本 |
| ---- | ---- | ---- |
| [dev-god](skills/development/dev-god) | 编程大神：接手陌生代码库，拆需求、补实现、追根因、重构、联调，推进到可验证交付 | 1.2.4 |
| [contextweave-interactive-architecture](skills/development/contextweave-interactive-architecture) | 架构图一键生成：从 JSON / HTML / 代码生成泳道图、流程图，导出 SVG、可编辑 PPTX 与 Visio | 1.7.2 |

### 数据分析

| 技能 | 说明 | 版本 |
| ---- | ---- | ---- |
| [cnfinancialscraper](skills/data-analysis/cnfinancialscraper) | 中国金融机构数据爬取与分析：2334 家机构、60+ 财经媒体舆情、A 股公告研报、实时行情与指标引擎 | 1.0.51 |

### 设计多媒体

| 技能 | 说明 | 版本 |
| ---- | ---- | ---- |
| [slides-maker](skills/design-media/slides-maker) | 演示文稿设计与评审：从论文、代码、文档生成 deck，支持重构与批评式复审 | 6.0.1 |
| [ppt-craft-master](skills/design-media/ppt-craft-master) | PPT 内容策略 + 视觉设计 + 叙事导演三位一体，内置 28 种版式与终审评分卡 | 1.0.3 |
| [poster-design-studio](skills/design-media/poster-design-studio) | 海报 / 宣传单 / banner 设计：根据活动描述或产品照片生成可印刷成品 | 0.2.4 |

## 目录规范

```
skills/
└── <分类目录>/
    └── <技能 ID>/
        ├── SKILL.md          # 必需：YAML frontmatter（name / description）+ 技能正文
        ├── scripts/          # 可选：可执行脚本
        ├── references/       # 可选：参考文档、模板
        └── _meta.json        # 可选：来源市场元数据（slug / version）
```

- 技能 ID 使用小写英文、数字与连字符，与目录名保持一致；
- `catalog.json` 由脚本从目录与 frontmatter 生成，不要手改；
- 技能正文保持原样，不修改作者内容；分类调整只移动目录。

## 安装到 Agent

```bash
# 默认安装到 ~/.hermes/skills
bash scripts/install.sh

# 指定目标目录（例如 TeamOS 容器内）
bash scripts/install.sh /opt/data/skills
```

脚本会把 `skills/<分类>/<技能>` 平铺复制到目标目录，同名技能先删除再覆盖。

## 新增与维护

```bash
python scripts/check.py          # 校验目录结构、frontmatter、重名
python scripts/check.py --write  # 校验并刷新 catalog.json
```

新增一个技能：

1. 把技能目录放进对应分类，确保根目录有 `SKILL.md`，frontmatter 至少包含 `name`、`description`；
2. 执行 `python scripts/check.py --write`；
3. 在 README 对应分类的表格里补一行，提交。

## 版权说明

各技能版权归原作者所有，本仓库仅做分类整理，供团队内部使用。`_meta.json` 保留了来源 slug 与版本，便于追溯与更新。
