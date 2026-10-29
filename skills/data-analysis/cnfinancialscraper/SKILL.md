---
name: cn-financial-scraper
display_name: 中国金融机构数据爬取与分析
display_name_en: CN Financial Scraper
description_zh: 中国金融机构数据爬取与分析 v12.0.0 —— 2334家/43大类机构名单、60+财经媒体源全网舆情、A股报告/公告/研报、60+种查询的多源金融数据统一入口（自动降级+缓存）、爬取调度与网页操作增强、实时行情中枢 + 24 指标引擎 + 多源证据融合、🆕信息获取一键编排（路由→抓取→质量分档→能力边界→Markdown回执）、🆕sha256 可校验交付包与失败项重下、🆕登录态档案接管（Cookie 全脱敏）。MCP 110 工具 + 10 Resource + 14 Prompt。
description_en: "China financial data scraper and analyst v12.0.0: 2,334 institutions across 43 categories, 60+ finance media sources for full-web sentiment, A-share reports/announcements/research, unified multi-source data query with auto-fallback and cache, crawler dispatcher plus enhanced web ops, real-time quote hub with 24 indicators and evidence fusion, one-call information orchestration (route, fetch, quality grade, can/cannot answer, Markdown receipt), sha256-verifiable delivery packages with retry of failed downloads, and redacted cookie-profile session handoff. 110 MCP tools, 10 resources, 14 prompts."
version: 12.0.0
description: |
  中国金融机构数据爬取与分析综合 Skill v12.0.0。2334家/43大类机构名单、60+财经媒体源舆情、
  A股报告/公告/研报、60+种查询多源金融数据（自动降级+缓存）、爬取调度与网页操作增强、
  实时数据中枢、24 技术指标引擎、多源证据融合、110 MCP 工具 + 10 Resource + 14 Prompt。

  🆕 v12.0.0 四条线（信息获取 / 打包下载 / 数据分析 / 登录）：
    编排：gather_info() 一次跑完"路由→抓取→去占位→质量分→能力边界→回执→打包"，
          8 类意图各带问法、输入输出、典型耗时与 not_for（说清哪件事做不了）。
    诚实：反爬占位条目一律剔除并记账，空结果报 E_SOURCE_EMPTY，不把假数据判成 B 级成功。
    交付：包内自带 manifest.json（逐文件 sha256），verify 查得出改动/缺失/多塞，
          批量下载单条失败不中断整批，失败清单可直接只重下那几条。
    登录：Cookie 由用户导出或扫码接管，档案化 + 健康度四档，对外输出全脱敏（值永不回显）。

  何时触发：爬取机构、舆情爬取、东方财富、公告下载、实时行情、技术指标、统一查询、
  交付包、批量重下、我已经登录了、登录态还有效吗（完整触发词见下方 auto_trigger）。
auto_trigger:
  keywords: [爬取机构, 机构名单, 机构查询, 舆情爬取, 全网舆情, 舆情监测, 爬取负面,
    利好新闻, 负面新闻, 对话式舆情, 东方财富, 龙虎榜, 北向资金, 监管动态, 巨潮资讯,
    公告下载, 券商研报, 上市公司报告, 网页爬取, 归档网页, 搜索引擎搜索, 批量爬取,
    定时爬取, 智能爬取, 打包下载, ZIP导出, 研究报告, 文档分析, 报告导出,
    基金净值, 基金估值, 股票分析, 风险指标, 夏普比率, 定期报告解读,
    财报解读, 结构化数据, 期货行情, 宏观经济, 港股行情, 美股行情, 统一查询,
    数据源切换, 自动降级, 数据回测, 行为画像, 验证码识别, 代理池, 指纹会话,
    IP信誉, 爬取预算, 断点续爬, 跨源验证, 表格识别, 键值提取, 汇总分析,
    趋势分析, 异常检测, 同业对标, 实时行情, K线, 技术指标, MACD,
    证据融合, 冲突台账, 引用包, 语音交互, 知识库同步,
    信息获取, 一键获取, 先跑计划, 意图路由, 生成回执, 爬取结果预览, 质量分,
    数据可信吗, 能答什么, 缺口分析, 样本不足, 交付包, 校验交付包, sha256清单,
    批量重下, 失败清单, 登录后再抓, 我已经登录了, 扫码登录, Cookie导入, 登录档案,
    登录态还有效吗, storage_state, 登录墙]
  patterns:
    - "(爬取|获取|看下|看看|查)(机构|公司|银行|基金|券商|保险)(的|这|那)?(舆情|新闻|资讯|报道)?"
    - "(爬取|导出|生成)\\s*\\S*(word|excel|docx|xlsx|报告|表格)"
    - "(正面|负面|舆情|利空|利好|网评)\\s*(信息|报道|新闻|消息|舆情)"
    - "(指定|设置)?(定时|每天|每周|每月|每小时)?\\s*爬取.{0,8}舆情"
    - "(东方财富|龙虎榜|北向资金|监管|巨潮资讯|基金净值)"
    - "(归档|保存|下载|抓取)\\s*(网页|页面|文章|全文)"
    - "(期货|宏观|港股|美股|Shibor|同业)\\s*(数据|行情|利率|指标)"
    - "(风险指标|夏普|回撤|组合复刻|相似基金)\\s*(分析|计算|推荐)?"
    - "(识别|提取|抽取)\\s*.{0,6}(网页|页面|表格|内容|数字)"
    - "(总结|同步|校对|写入)\\s*.{0,8}(知识库|ima|dify|obsidian)"
    - "(代理|IP)\\s*(池|轮换|信誉|退避|被封)"
    - "(验证码|滑块|极验|reCAPTCHA|hCaptcha|被封禁|防封)"
    - "(跨源|交叉验证|多源|共识|冲突|证据|引用包)\\s*(值|记录|清单|聚类|分析)?"
    - "(趋势|异常|spike|放量|突变|预测|外推)"
    - "(实时|盘中|现在|此刻)\\s*(行情|报价|快照|价格|走势|涨跌)"
    - "(查|看|取)\\s*\\S{0,8}(K线|分钟线|前复权|板块涨跌|基金估值)"
    - "(MACD|RSI|KDJ|BOLL|ADX|OBV|均线)\\s*(金叉|死叉|超买|超卖|背离|指标|信号)?"
    - "(信息获取|一键获取|帮我(查|抓|取)|先(看|跑)?计划|跑(完)?计划|回执|爬取结果)"
    - "(质量|可信|靠谱|能答|不能答|样本(不足|量)|缺口)"
    - "(交付|打包|压缩)\\s*(包|件|zip)|\\s*(校验|验证)\\s*(包|zip|清单|sha)"
    - "(重下|重新下载|失败(项|清单|列表)|没下载(成|到))"
    - "(登录|会员|扫码|cookie|会话)\\s*(后|了|再|导入|导出|接管|档案|状态|过期|失效)?"
    - "(用我(的|自己)(账号|会话|登录)|我已(经)?登录)"
---

# cn-financial-scraper v12.0.0 — 信息获取一键编排 + 交付包自证 + 登录态接管

> **全量机构名单（2334家/43大类）** | **🆕 v12 8 类意图编排 / sha256 交付包 / 登录档案脱敏** | **110 MCP 工具 / 10 Resource / 14 Prompt** | **v11 实时数据中枢 + 24 指标引擎 + 证据融合** | **36种查询类型自动降级（akshare/东方财富/yfinance/RSS）** | **60+ 商业财经媒体源 + RSS + akshare 兜底** | **智能多级爬取 + 网页内容识别** | **数据分析汇总 + 语音交互**

## ⚡ 一键上手

直接对话即可触发，无需记忆命令：

```
帮我爬一下某上市公司最近7天的舆情
某基金公司最近3天的负面新闻，并导出 Excel
把这篇网页完整归档，包含图片和表格
搜索"贵州茅台2026半年报"然后把结果爬取下来
爬一下银行板块最近3天的舆情并导出 Word
我已经登录了雪球，用我的会话抓一下这个页面        🆕
这批公告 PDF 全部下载下来，给我一个能校验的交付包   🆕
先别真跑，把计划和数据源给我看一下再决定           🆕
```

**一键启动**（Windows 直接 `python run_sentiment.py` 或）：
```bash
python run_sentiment.py                          # 交互式对话
python run_sentiment.py "贵州茅台最近7天的舆情"     # 直接命令行
```

---

## 🧭 先试哪个（8 类意图 → 入口 → 你要手动衔接的地方）

`gather_info()` 是 v12 的唯一编排入口：**路由 → 抓取 → 去占位 → 质量分 → 能力边界 → 回执 → 打包**，
一次调用跑完，plan 里每一步都标了真实状态（`done`/`empty`/`skipped`/`failed`）。
拿不准问法属于哪一类时，先跑 `list_info_intents(keyword="你的原话")`，它直接回你路由结论与备选。

| 你想做什么 | intent | 一句问法示例 | 典型耗时 | 需要手动衔接的点 | 做不了什么 |
|---|---|---|---|---|---|
| 舆情/新闻监控 | `sentiment` | "贵州茅台最近7天负面舆情" | 30-120s | 只要 `package=True` 就出交付包，不必再调打包工具 | 未公开信息；撑不起比例结论时会明说 |
| 抓某个网页 | `web` | "把这个 URL 的正文和表格取下来" | 1-20s | 动态页需 Playwright，否则自动降级到 HTTP | 需登录且未建档的页面 |
| 搜索后再抓 | `search_crawl` | "搜'XX 2026 半年报'然后把结果爬下来" | 5-40s | 搜索引擎返回空时换 `engines`；结果需人工挑 URL | 付费墙正文 |
| 机构官网名单 | `institution` | "查一下招商证券的官网信息" | 1-10s | 名单缺项用 `expand_institutions` 补 | 未收录的新设机构需先扩充名单 |
| 行情/数据 | `market` | "510300 现在价格和今日走势" | <1-15s | 停牌行不算缺失（带 `suspended`）；样本不足指标返回 None | 实时 WebSocket 推送 |
| 公告/研报/财报 | `announcement` | "下载贵州茅台的最新公告" | 5-60s | PDF 下载后交给 `document` 意图解析；付费库只有索引 | 未公开信息；付费研报正文（版权） |
| 本地文档解析 | `document` | "分析这份 PDF"/"这两份 Word 做对比" | 1-30s/份 | 扫描件需先 OCR；目录输入先列文件 | 图片型 PDF、加密文件 |
| 登录后可见页面 | `login_page` | "我已经登录了，用我的会话抓这一页" | <1s 探测 / 1-30s 抓取 | Cookie 由用户自己导出（`add_login_profile`）或扫码接管 | 托管密码、破解验证码、违反站点条款的批量抓取 |

```python
from scripts.pipeline_runner import gather_info, render_pipeline_markdown

plan = gather_info(topic="贵州茅台最近7天负面舆情", dry_run=True)      # 只看计划，不发请求
res  = gather_info(topic="贵州茅台最近7天负面舆情", days=7, package=True)  # 实跑 + 交付包
print(render_pipeline_markdown(res))   # 回执：来源/质量分/能答什么/缺口/下一步
```

**三条诚实性保证**（v12 起是硬行为，不是可选项）：
- 反爬产生的 `[未能直接抓取] …` 占位条目一律剔除并计入 `meta.placeholders_dropped`，
  全部为空时直接报 `E_SOURCE_EMPTY`，不会把 15 条假数据判成 B 级成功；
- `quality` 给 A–D 分档并说明扣分项，样本 <10 条时 `answer_ability.cannot_answer`
  会显式禁止比例/排名类结论；
- 登录探测没有稳定证据时返回 `unknown` + HTTP 诊断，不硬猜"已登录"。

---

## 🧭 场景速查（想做什么 → 直接入口）

> 无需记忆命令，按「想做什么」直接找到入口。完整 API 与 MCP 工具表见 [README.md](README.md)。

| 你想做什么 | 直接入口 | 依赖 |
|---|---|---|
| 🆕 统一查询金融数据（股票/基金/期货/宏观/港股美股） | `get_financial_data("stock_quote", code="600519")` | 核心 |
| 🆕 查看所有可用数据源状态 | `list_data_sources()` | 核心 |
| 🆕 反爬系统状态报告 | `get_anti_block_status()` | 核心 |
| 🆕 发现免费代理并验证 | `discover_proxies(max_proxies=20)` | 核心 |
| 爬某公司/机构最近 N 天舆情 | `crawl_global_sentiment` 或 `python run_sentiment.py "XX最近7天舆情"` | 核心 |
| 查机构名单 / 机构信息 | `query_institution` / `search_institution` | 核心 |
| 爬单个网页内容 | `scrape_webpage(url)` | 核心 |
| 动态页面 / 类人浏览 / 验证码接管 | `browser_human_fetch(url)`（需 Playwright） | Playwright |
| 搜公告 / 券商研报 / 公司财报 | `search_announcements` | 推荐 |
| 股票 / 基金 / 可转债实时行情 | `get_financial_data("stock_quote", code="600519")` | 核心 |
| 期货 / 宏观(GDP·CPI·PPI·PMI) / Shibor / 港股美股 | `get_financial_data("macro_cpi")` | 核心 |
| 风险指标（年化/波动/回撤/夏普/卡玛） | `analyze_risk_metrics` / `calculate_risk_metrics` | 推荐 |
| 定期报告 / 财报解读 | `interpret_stock_report("600519")` / `interpret_stock` | 核心 |
| 生成研究报告 / 多格式导出 | `generate_research_report` / `quick_report` | 推荐 |
| 批量爬取 + 打包下载 | `CrawlPackager().package_batch_crawl` | 核心 |
| 搜索引擎搜索（中文优先） | `search_web` / `quick_search` | 核心 |
| 网页全页归档 | `quick_archive(url)` | 核心 |
| 扩充机构名单 / 补全官网 / 更新A股名单 | `expand_institution_list` / `UrlCompleter` / `StockListUpdater` | 核心 |
| 批量智能爬取（API→HTTP→浏览器降级） | `batch_smart_crawl(urls)` / `SmartFetcher` | 核心 |
| 网页内容识别（表格/键值/条目/分页/数字） | `smart_extract(html, url)` / `smart_page_extract(url)` | 核心 |
| 舆情快照汇总分析 / 机构简报 / Markdown 报告 | `aggregate_snapshots` / `build_institution_brief` | 核心 |
| 总结进知识库（IMA/Dify/Obsidian/本地）/ 知识库校对 | `python run_knowledge.py` / `sync_to_knowledge_base` / `verify_against_knowledge_base` | 核心 |
| 语音交互（说一句话爬取舆情） | `python run_voice.py` / `VoiceChat` | 核心（Windows） |
| 🆕 一站式信息获取（不确定用哪个工具） | `gather_info(topic="你的原话")` / `list_info_intents(keyword=…)` | 核心 |
| 🆕 登录后才能看的页面 | `add_login_profile` → `check_login_status` → `gather_info(intent="login_page", profile=…)` | 核心（扫码需 Playwright） |
| 🆕 要一个能自证完整性的交付包 | `package_delivery` → `verify_package`；批量链接下载用 `package_remote_files` | 核心 |
| 反爬深度增强（代理池/指纹/Header/节奏/预算/IP信誉/验证码/会话预热） | `AntiBlockPipeline` · `ProxyPool` · `TLSClientFactory` · `detect_captcha` | 核心 |
| 爬后深度分析（关键词/跨源/趋势/对标/洞察） | `enhanced_aggregate(articles, days=7)`（单项可换 `extract_keywords` 等） | 核心 |

## 📚 文档导航（找什么 → 去哪里）

| 找什么 | 去哪里 |
|---|---|
| 快速开始 + 场景入口 | 本文件「一键上手」+「先试哪个」+「场景速查」 |
| 完整功能 / 机构清单 / 媒体源 / MCP 工具表 / 变更日志与不兼容说明 | [README.md](README.md) |
| 故障排查（返回空 / 限流 / 依赖缺失 / 成功率低） | [TROUBLESHOOTING.md](TROUBLESHOOTING.md) |
| 安全审计 / 数据源合规 | [SECURITY.md](SECURITY.md) |
| 全部测试 | `python runtests.py`（开发副本才带 `tests/`；用例数以运行输出为准，技能包内不含测试） |

---

## 能力矩阵

| 模块 | 能力 | 依赖层级 | 入口 |
|---|---|---|---|
| **机构名单** | 2334 家金融机构，43 大类，URL 注册表 | 核心 | `query_institution` |
| **全网舆情** | 4 类 60+ 商业财经媒体源 + RSS 直连 + 搜索引擎 + akshare 兜底，情感/严重度分级，模糊去重 | 核心 | `crawl_global_sentiment` |
| **网页爬取** | 六级降级链（Scrapling→Playwright→HTTP），缓存+熔断+重试 | 核心 | `scrape_webpage` |
| **浏览器爬取** | 深度类人操作（阅读停顿/4种滚动模式/贝塞尔鼠标/悬停/多标签/iframe合并）、会话保持、验证码人工接管、proxy、单例复用 | Playwright | `browser_human_fetch` |
| **公告/研报** | 巨潮公告 PDF、券商研报、公司财报 | 推荐 | `search_announcements` |
| **行情/数据** | 股票/基金/可转债/两融/大宗/ETF流/龙虎榜/LPR/国债/北交所 | 核心 | `get_financial_data` |
| 🆕 **多源金融数据桥接** | 36种查询类型统一入口，akshare→东方财富API→新浪→yfinance→RSS→搜索 自动降级，健康追踪 | 核心 | `financial_data_mcp.py` |
| **结构化数据** | 期货/宏观(GDP·CPI·PPI·PMI)/Shibor/港股美股 | akshare | `get_structured_data` |
| **产品分析** | 真实风险指标（年化/波动/回撤/夏普/卡玛）、风格、组合复刻、相似推荐 | 推荐 | `analyze_risk_metrics` |
| **搜索** | 百度/搜狗/DDG/SearXNG 多引擎，中文优先 | 核心 | `search_web` |
| **定期报告解读** | 财报自动解读（业绩概览/同比环比/ROE/毛利率/现金流/亮点/风险/评分评级），纯规则引擎零依赖 | 核心 | `interpret_stock_report` |
| **智能爬取** | 🆕 API→HTTP→浏览器三级降级链、批量并发+去重+成功率统计 | 核心 | `batch_smart_crawl` |
| **内容识别** | 🆕 表格/键值块/文章条目/分页/金融数字抽取、页面类型判别（零依赖） | 核心 | `smart_extract` |
| **汇总分析** | 🆕 快照聚合、模糊去重、情感/来源/时间分布、机构截面、Markdown/JSON 导出 | 核心 | `aggregate_snapshots` |
| **语音交互** | 🆕 语音输入→对话引擎→语音播报，Windows SAPI 零依赖（可选 vosk/pyttsx3 升级） | 核心 | `python run_voice.py` |
| **报告** | 研究报告生成、文档分析、图表、多格式导出（含真实 PDF / HTML 网页版） | 推荐 | `generate_research_report` |
| 🆕 **反爬深度增强** | 代理池 / 免费代理发现 / 行为画像 / TLS 指纹 / Header顺序随机 / 指纹一致性会话 / 请求节奏调度 / 预算控制 / IP 信誉 / 验证码识别 / 会话预热 / 一站式管道 | 核心 | `anti_block_enhanced.py` |
| 🆕 **数据分析深度增强** | 关键词实体抽取 / 跨源验证 / 趋势异常 / 同业对标 / 洞察生成 / 时间分桶 / 一站式聚合 | 核心 | `analysis_enhanced.py` |

> **依赖层级说明**：**核心** = 零 pip 依赖即可用 | **推荐** = `--recommended` 安装后可用（bs4/lxml/docx） | **Playwright** = 需额外 `pip install playwright && playwright install chromium` | **akshare** = 需额外 `pip install akshare`

### ⚠️ 能力边界（重要）

| 类别 | 说明 |
|---|---|
| ✅ **完全支持** | 机构名单查询、基金/ETF/FOF/股票产品解析、A股公告搜索下载、券商研报、新闻资讯、舆情监控、批量爬取（断点续爬）、文档解析（PDF/Word/Excel/PPT/HTML）、报告导出（Word/PPT/Excel）、风险指标分析、结构化市场数据（期货/宏观/Shibor） |
| ❌ **不支持** | 需登录的页面（如微信公众号后台、券商内部系统）、付费墙内容（如 Wind 终端数据）、实时 WebSocket 推送、移动端 APP 内嵌页面、验证码自动破解（需第三方打码服务） |
| ⚠️ **部分支持** | 动态渲染页面（需 Playwright，用 `mode="realtime"`）、反爬极严的网站（如某些银行官网，成功率 30-50%）、大批量爬取（需控并发 ≤5 + 断点续爬）、海外网站（需代理，部分有地域限制） |

**预期成功率**：天天基金 95%+ / 东方财富 90%+ / 同花顺 85%+ / 基金公司官网 70-90% / 银行官网 30-50% / 反爬严格的网站 20-40%。

---

## 安装（三层依赖）

```bash
python setup_env.py                # 仅核心（零 pip 依赖，舆情/HTTP 可用）
python setup_env.py --recommended  # + bs4/lxml/docx/openpyxl（解析+导出）
python setup_env.py --full         # + scrapling/playwright/mcp/akshare（全功能）
```

| 功能 | 需要的依赖 | 安装命令 |
|---|---|---|
| 舆情爬取 / 机构查询 / HTTP 爬取 | 无（核心自带） | `python setup_env.py` |
| PDF/Word/Excel 解析 + 报告导出 | bs4, lxml, python-docx, openpyxl | `python setup_env.py --recommended` |
| 浏览器自动化（动态页面/类人爬取） | playwright | `pip install playwright && playwright install chromium` |
| 结构化数据（期货/宏观/Shibor） | akshare | `pip install akshare` |
| 全球金融数据（美股/港股/ETF/外汇） | yfinance | `pip install yfinance` |
| TLS 指纹模拟（JA3 真实浏览器级） | curl-cffi | `pip install curl-cffi` |
| 全功能 | 全部 | `python setup_env.py --full` |

---

## 🆕 v12.0.0 三条新线速查

### 1️⃣ 信息获取编排（`scripts/pipeline_runner.py`）

```python
from scripts.pipeline_runner import (INTENTS, route, plan_for, gather_info,
                                     render_pipeline_markdown, quality_score,
                                     answer_ability, PIPELINE_ERRORS, self_check)
route("我已经登录了，用我的会话抓这一页")            # → intent=login_page，confidence=high
gather_info(topic="贵州茅台", days=7, dry_run=True)   # 只出计划与契约，不发请求
res = gather_info(topic="贵州茅台", days=7, analyze=True, fuse=True,
                  package=True, out_dir=r"D:\out")    # 实跑 + 交付包
res["quality"]          # {'grade': 'B', 'score': 76.7, 'fill_rate': …, 'counts': …}
res["answer_ability"]   # can_answer / cannot_answer / confidence / why
render_pipeline_markdown(res)   # 回执：来源、发布时间、质量分、缺口、下一步
```

9 个错误码（`PIPELINE_ERRORS`）每个都带 `cause` + `next_step`：`E_SOURCE_EMPTY`、
`E_LOGIN_REQUIRED`、`E_ROUTE_NOT_FOUND`、`E_SAMPLE_TOO_SMALL`、`E_PACKAGE_FAILED` …

### 2️⃣ 交付包自证（`scripts/crawl_packager.py`）

```python
from scripts.crawl_packager import (build_delivery_package, verify_delivery_package,
                                    package_remote_files)
build_delivery_package([p1, p2], zip_name="maotai_2026q3",
                       manifest_payload={"topic": "贵州茅台", "records": 42})
verify_delivery_package("maotai_2026q3.zip")   # ok / checked / mismatched / missing / extra
package_remote_files(urls, retries=2)          # 单条失败不中断整批；失败清单只重下那几条
```

CLI：`python scripts/crawl_packager.py deliver "<文件1,文件2>" 包名`、`verify 包名.zip`
（校验不通过 exit 2，便于脚本判断）。

### 3️⃣ 登录态接管（`scripts/login_session.py`）

```python
from scripts.login_session import (add_profile, list_profiles, update_profile,
                                   check_login, cookie_header, apply_to_session,
                                   storage_state, wait_for_manual_login,
                                   login_status_report)
add_profile("eastmoney", "quote.eastmoney.com", cookie_file=r"C:\cookies.txt")
check_login("eastmoney")                        # fresh/expiring/expired/session_only + 下一步
n = apply_to_session(my_session, "quote.eastmoney.com")   # 注入后照常爬
wait_for_manual_login("xueqiu", "https://xueqiu.com")     # 扫码完成后自动收会话
```

档案默认落 `data/login_profiles/`（`CNFS_LOGIN_DIR` 可改道到包外），交付前由 `clean.py` 删除；
任何对外输出（summary / 列表 / 交付清单）只给 Cookie 条数与名称，**绝不回显值**。
边界：不托管账号密码、不破解验证码、不做违反站点条款的批量抓取。

### MCP 工具 96 → 110（v12 新增 14 个）

`list_info_intents` / `gather_info` / `render_info_receipt` / `package_delivery` /
`verify_package` / `package_remote_files` / `add_login_profile` / `list_login_profiles` /
`update_login_profile` / `remove_login_profile` / `check_login_status` /
`export_login_storage_state` / `wait_for_login` / `capability_self_check`
（v11 的 17 个实时/指标/融合工具与 v10 的调度、网页操作工具全部保留）

---

## 历史版本速查（v5 → v11，完整说明见 [README.md](README.md)）

| 版本 | 一行能力 | 常用入口 |
|---|---|---|
| v11.0 | 实时数据中枢 / 24 指标引擎 / 多源证据融合 | `get_hub()` · `analyze_bars()` · `fusion_aggregate()` |
| v10.0 | 爬取调度器 · Shadow DOM/iframe · Tushare·CoinGecko·外汇 · LRU+TTL 缓存 | `quick_dispatch()` · `shadow_pierce()` · `QueryCache` |
| v9.0 | 36 查询 9 适配器自动降级 · 反爬深度（代理/Header/指纹/节奏） | `query_financial_data()` · `AntiBlockPipeline` |
| v8.0 | 反爬管道 + 分析增强（关键词/跨源/趋势/对标/洞察） | `enhanced_aggregate()` |
| v7.7 | 自适应爬取止损 · 知识库语音指令 · 断点续传下载 · Sortino/趋势四档 | `AdaptiveCrawlPolicy` · `batch_download()` |
| v7.6 | 知识库总结同步（IMA/Dify/Obsidian/本地）与校对 | `sync_to_knowledge_base()` |
| v7.5 | 六级编码检测链 + download_file 重试/Referer/反爬页校验 | `download_file()` |
| v7.2 | URL 跨进程持久化去重 | `UrlDeduplicator("data/seen_urls.json")` |
| v7.1 | 定期报告自动解读（业绩/ROE/现金流/评分评级） | `interpret_stock_report("600519")` |
| v7.0 | API 挖掘 · 宏录制回放 · 异步并发标签池 · 名单扩充 | `discover_api()` · `play_macro()` |
| v6.0 | 深度类人浏览器 · 真实 PDF 报告 · zip 批量下载 | `BrowserScraper.humanlike_fetch()` |
| v5.0 | 结构化市场数据 · 中文搜索引擎 · 真实风险指标 | `get_structured_data()` · `quick_search()` |

---

## 关键命令

| 命令 | 用途 |
|---|---|
| `python run_sentiment.py` | 交互式舆情对话 |
| `python run_voice.py` | 语音交互模式（v7.7.0 支持知识库语音指令） |
| `python run_knowledge.py --target local` | 知识库总结同步（v7.6.0） |
| `python runtests.py` | 跑全部测试（仅开发副本带 `tests/`；数量以运行输出为准） |
| `python setup_env.py --full` | 全功能安装 |
| `python mcp_server.py` | 启动 MCP Server（stdio，**110 工具 + 10 Resource + 14 Prompt**） |
| `python scripts/crawl_packager.py deliver "f1,f2" 包名` | 生成含 sha256 清单的交付包；`verify 包名.zip` 复验 |
| `python scripts/pipeline_runner.py "你的原话"` | 信息获取编排 CLI（路由 → 计划 → 回执） |
| `python scripts/scrapable_registry.py stat` | 机构名单统计 |
| `python scripts/structured_market_data.py` | 结构化数据自检 |

---

## 故障排查速查

| 症状 | 原因 | 解决 |
|---|---|---|
| cls/sina/jisilu/wallstreetcn 返回空 | 接口被封 | v5.0 已接线 akshare 自动兜底，无需手动处理 |
| 主站返回 200 但内容是验证码页 | 软封禁 | 自动检测并切浏览器兜底；`DomainRateLimiter` 按域限速 |
| 中文搜索召回差 | 默认引擎不合适 | 百度/搜狗优先（`baidu_html`/`sogou_html`） |
| 舆情抓取慢 | 每源冷启动 | v5.0 浏览器单例复用；`parallel_workers=4` 并发 |
| 行情/新闻请求被限流 | 频率过高 | 全部请求已统一走自适应限流（`RatelimitedSession`） |
| `ModuleNotFoundError: playwright` | 未装浏览器依赖 | `pip install playwright && playwright install chromium` |
| akshare 返回空 | 未装 akshare | `pip install akshare` |
| 更多 | — | [TROUBLESHOOTING.md](TROUBLESHOOTING.md) |

---

## ❓ 常见问题（FAQ）

### Q1: 舆情爬取返回空怎么办？
**A**: 1）检查目标名称是否正确（用 `list_sentiment_targets` 查看）；2）尝试扩大时间范围（`days=7`）；3）增加媒体源类别（`source_categories=["authoritative", "financial_vertical", "self_media"]`）。

### Q2: 结构化数据（期货/宏观）返回 None？
**A**: 需要安装 akshare：`pip install akshare`。未安装时函数自动返回 None，不会报错。

### Q3: 动态渲染页面（SPA）爬不到内容？
**A**: 默认 `mode="static"` 只走 HTTP。SPA 页面需用 `mode="realtime"`（需 Playwright）：
```python
result = scrape_webpage(url="...", mode="realtime")
```

### Q4: 爬取银行官网成功率很低？
**A**: 银行官网普遍反爬严格（验证码/IP 封禁），预期成功率 30-50%。建议：1）用 `browser_human_fetch` 走浏览器；2）加 proxy 轮换；3）降低频率。

### Q5: 如何自定义舆情监控目标？
**A**: 两种方式：
```python
# 方式 1：对话式
chat_handle("新增自定义目标：恒生电子")

# 方式 2：API
add_custom_sentiment_target("custom", "恒生电子")
```

### Q6: pip 安装依赖很慢怎么办？
**A**: 使用国内镜像：
```bash
pip install -i https://pypi.tuna.tsinghua.edu.cn/simple -r requirements.txt
# 或永久设置
pip config set global.index-url https://pypi.tuna.tsinghua.edu.cn/simple
```

### Q7: 不知道自己的问法会走哪条链，也不知道哪几步要手动衔接？
**A**: 先 `list_info_intents(keyword="你的原话")` 看路由结论与备选，再 `gather_info(..., dry_run=True)` 看步骤计划与数据源；确认后去掉 dry_run——整条链在 `gather_info` 内部自动衔接，`plan` 每步标 `done/empty/skipped/failed`。

### Q8: 抓到的结果长什么样？能直接下结论吗？
**A**: `render_info_receipt(result)` 出 Markdown 回执（逐条来源+发布时间、共识事件、洞察、质量分档、缺口、下一步）。结论前看 `answer_ability`：样本 <10 条禁止比例/排名类结论，单源禁止跨源一致性结论，`confidence=low` 就别拿去当结论。

### Q9: 结果条数比预期少？
**A**: 反爬失败留下的 `[未能直接抓取] …` 占位条目会被编排层剔除并记进 `meta.placeholders_dropped`（条数+来源）；全空时直接报 `E_SOURCE_EMPTY` 并给"放宽 days / 加 source_categories / 换 intent"的下一步，不会把空壳算成成功。

### Q10: 登录后的页面怎么爬？Cookie 会不会被回显出去？
**A**: 用户自己导出 Cookie 建档（`add_login_profile`）或扫码接管（`wait_for_login`）→ `check_login_status` 验健康度 → `gather_info(intent="login_page", profile="…")`。所有对外输出只含 Cookie 条数与名称，**值永不回显**；档案在 `data/login_profiles/`，交付前由 `clean.py` 删除。不支持托管密码、破解验证码、违反站点条款的批量抓取。

### Q11: 交付包怎么证明没被改过、没漏文件？
**A**: `package_delivery` 的 zip 内含 `manifest.json`（逐文件 sha256+字节数），接收方 `verify_package(zip_path)` 得到 `mismatched`/`missing`/`extra`；批量链接下载用 `package_remote_files`——单条失败不中断整批，失败项进 `manifest.failed`，下一轮只重下那几条。

> 📘 更多故障排查见 [TROUBLESHOOTING.md](TROUBLESHOOTING.md) | 🔒 安全审计见 [SECURITY.md](SECURITY.md)

---

## 详细文档

完整功能说明、机构清单、媒体源/目标库、MCP 工具表（110 个）、实战场景、变更日志与不兼容说明，见 [README.md](README.md)。
