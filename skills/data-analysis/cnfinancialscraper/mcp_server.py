#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
cn-financial-scraper MCP Server — 中国金融数据爬取与研究工具
通过 MCP 协议暴露机构查询、网页爬取、产品解析、公告下载、新闻资讯等工具，
让 Claude Code 可以直接调用中国金融数据爬虫的核心功能。

使用方式（Claude Code 配置 mcpServers）:
{
  "mcpServers": {
    "cn-financial-scraper": {
      "command": "python",
      "args": ["./mcp_server.py"],
      "env": {
        "PYTHONDONTWRITEBYTECODE": "1"
      }
    }
  }
}
"""
from __future__ import annotations

import sys
sys.dont_write_bytecode = True

import json
import os
import re
import logging
import asyncio
from datetime import datetime
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR / "scripts"))

try:
    from mcp.server.fastmcp import FastMCP
except ImportError as e:
    print(f"❌ MCP SDK 不可用: {e}", file=sys.stderr)
    print("请运行: pip install mcp", file=sys.stderr)
    sys.exit(1)

# 顶层导入核心模块（避免每次工具调用重复 import）
try:
    from scrapable_registry import ScrapableRegistry
except ImportError:
    ScrapableRegistry = None

try:
    from scraper import FinancialPageScraper
except ImportError:
    FinancialPageScraper = None

try:
    from institution_scraper import InstitutionScraper
except ImportError:
    InstitutionScraper = None

try:
    from news_scraper import EastMoneyNewsAPI
except ImportError:
    EastMoneyNewsAPI = None

try:
    from announcement_scraper import AnnouncementManager, PDFDownloader
except ImportError:
    AnnouncementManager = PDFDownloader = None

try:
    from research_report_scraper import BrokerReportManager
except ImportError:
    BrokerReportManager = None

try:
    from comprehensive_report_scraper import ComprehensiveReportManager
except ImportError:
    ComprehensiveReportManager = None

try:
    from document_parser import parse_document as doc_parse
except ImportError:
    doc_parse = None

try:
    from report_exporter import ComprehensiveExporter
except ImportError:
    ComprehensiveExporter = None

try:
    from batch_institution_crawler import BatchInstitutionCrawler
except ImportError:
    BatchInstitutionCrawler = None

try:
    from report_indexer import StockIndexer
except ImportError:
    StockIndexer = None

try:
    from document_analyzer import DocumentAnalyzer
except ImportError:
    DocumentAnalyzer = None

# v9.0 金融数据 MCP 桥接
try:
    from financial_data_mcp import (
        FinancialDataRouter, query_financial_data,
        list_available_sources, list_query_types,
    )
except ImportError:
    FinancialDataRouter = query_financial_data = None
    list_available_sources = list_query_types = None

# v9.0 反爬增强
try:
    from anti_block_enhanced import (
        FreeProxyDiscovery, ProxyPool, FingerprintSession,
        RequestTempoScheduler, HeaderOrderRandomizer,
    )
except ImportError:
    FreeProxyDiscovery = ProxyPool = FingerprintSession = None
    RequestTempoScheduler = HeaderOrderRandomizer = None

# v3.0 新增数据源
try:
    from sina_scraper import get_realtime_quote as sina_quote
except ImportError:
    sina_quote = None

try:
    from cls_scraper import get_telegraph as cls_telegraph
except ImportError:
    cls_telegraph = None

try:
    from jisilu_scraper import get_convertible_bonds as jisilu_bonds, search_bonds as jisilu_search
except ImportError:
    jisilu_bonds = jisilu_search = None

# v4.0 新增模块
try:
    from crawl_scheduler import CrawlScheduler, get_scheduler
except ImportError:
    CrawlScheduler = get_scheduler = None

try:
    from crawl_packager import batch_crawl_and_package as _batch_crawl_and_package
except ImportError:
    _batch_crawl_and_package = None

try:
    from content_compressor import compress_content, CompressConfig
except ImportError:
    compress_content = CompressConfig = None

try:
    from enhanced_parser import parse_file_enhanced as _parse_file_enhanced
except ImportError:
    _parse_file_enhanced = None

try:
    from research_report_generator import ResearchReportGenerator, ReportConfig, ReportTheme
except ImportError:
    ResearchReportGenerator = ReportConfig = ReportTheme = None

# v4.3 全网舆情爬虫
try:
    from sentiment_crawler import (
        SentimentCrawler, SentimentSourceLoader, SentimentTargetLoader,
        crawl_sentiment as _crawl_sentiment,
        list_sentiment_targets as _list_sentiment_targets,
        list_sentiment_sources as _list_sentiment_sources,
        add_custom_sentiment_target as _add_custom_target,
    )
except ImportError:
    SentimentCrawler = SentimentSourceLoader = SentimentTargetLoader = None
    _crawl_sentiment = _list_sentiment_targets = _list_sentiment_sources = _add_custom_target = None

try:
    from sentiment_exporter import export as _export_sentiment
except ImportError:
    _export_sentiment = None

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("mcp_server")

server = FastMCP("cn-financial-scraper")


def _module_error(module_name: str, install_hint: str = "") -> str:
    """生成模块缺失的错误信息（含安装指引）"""
    hint = install_hint or f"pip install -r requirements.txt"
    return f"❌ {module_name} 模块未安装，请运行: {hint}"


# ==================== 机构名单查询 ====================

@server.tool()
async def query_institution(institution_type: str = "", keyword: str = "") -> str:
    """查询中国金融机构名单。支持按类型筛选和关键词搜索。
    机构类型（27大类）: 基金管理公司, 证券公司, 保险公司, 信托公司, 商业银行, 
    私募基金, 外资金融机构, 金融租赁, 第三方销售, 理财公司, 期货公司,
    消费金融公司, 保险资产管理公司, 再保险公司, 汽车金融公司, 金融控股公司,
    货币经纪公司, 金融资产投资公司, 企业集团财务公司, 融资担保公司,
    期货风险管理子公司, 城投机构等。

    Args:
        institution_type: 机构类型（留空查询全部）
        keyword: 搜索关键词，如"华夏"、"招商"（可选）
    """
    try:
        if ScrapableRegistry is None:
            return _module_error("scrapable_registry")
        registry = ScrapableRegistry()

        if keyword:
            results = registry.search(keyword)
            if not results:
                return f"未找到包含「{keyword}」的机构。"
            lines = [f"🔍 搜索「{keyword}」结果 ({len(results)}条):", ""]
            for r in results[:30]:
                scrapable = r.get('scrapable', False)
                website = r.get('website', '') or ''
                lines.append(
                    f"  {r['name']}  [{r['type']}]  "
                    f"{'✅ ' + website if scrapable and website else '❌ 无URL'}"
                )
            return '\n'.join(lines)

        stats = registry.get_statistics()
        lines = [
            f"📊 全量金融机构统计 ({stats['total']}家)",
            f"可爬取: {stats['scrapable']}家",
            "",
            "按类型统计:",
        ]
        for t, info in sorted(stats.get('by_type', {}).items(), key=lambda x: -x[1]['total']):
            lines.append(f"  {t}: {info['total']}家 ({info['scrapable']}可爬取)")

        if institution_type:
            insts = registry.list_by_type(institution_type)
            lines.extend(["", f"「{institution_type}」列表 ({len(insts)}家):"])
            for i in insts[:20]:
                website = i.get('website', '') or ''
                scrapable = i.get('scrapable', False)
                lines.append(
                    f"  {i['name']}  {'✅ ' + website if scrapable and website else ''}"
                )
            if len(insts) > 20:
                lines.append(f"  ... 还有 {len(insts)-20} 家")

        return '\n'.join(lines)
    except Exception as e:
        return f"查询失败: {str(e)}"


# ==================== 网页爬取 ====================

@server.tool()
async def scrape_webpage(url: str, use_dynamic: bool = False) -> str:
    """爬取指定URL的网页内容，返回文字摘要。
    支持静态页面和动态渲染（JavaScript）两种模式。
    适用场景：爬取金融机构官网、新闻页面、产品页面等。

    Args:
        url: 完整网页URL
        use_dynamic: 是否使用动态渲染（处理JS加载的页面），默认False
    """
    try:
        if FinancialPageScraper is None:
            return _module_error("scraper")
        scraper = FinancialPageScraper()
        content = scraper.scrape_url(url, use_dynamic=use_dynamic)
        if content:
            text = str(content)[:3000]
            return f"✅ 成功获取 {len(text)} 字符\n\n{text}"
        return f"❌ 爬取失败，目标网站可能有限流或需要登录。"
    except Exception as e:
        return f"爬取失败: {str(e)}"


@server.tool()
async def scrape_institution(name: str) -> str:
    """按机构名称爬取其官网内容。自动从注册表中查找URL并爬取。
    适用场景：需要快速获取某金融机构的官网最新信息。

    Args:
        name: 机构名称关键词，如"华夏基金"、"招商证券"
    """
    try:
        if InstitutionScraper is None:
            return _module_error("institution_scraper")
        scraper = InstitutionScraper()
        result = scraper.scrape_by_name(name)
        if result and result.get('content'):
            text = result['content'][:2000]
            return (
                f"✅ {result.get('name', name)} 爬取成功\n"
                f"来源: {result.get('url', '未知')}\n"
                f"内容:\n{text}"
            )
        return f"❌ 未找到机构「{name}」或爬取失败。"
    except Exception as e:
        return f"爬取失败: {str(e)}"


# ==================== 金融产品解析 ====================

@server.tool()
async def parse_financial_product(url: str, product_type: str = "fund") -> str:
    """解析金融产品页面信息。支持基金、ETF、FOF、股票、债券等。
    适用场景：需要提取某只基金/股票/债券的详细产品信息。

    Args:
        url: 产品页面URL，如天天基金、东方财富产品页
        product_type: 产品类型 — fund(基金) / etf(ETF) / fof(FOF) / stock(股票) / bond(债券)
    """
    try:
        # 动态导入 web_parser
        import importlib
        web_parser = importlib.import_module("web_parser")
        result = web_parser.parse_financial_product(url, product_type)
        if result:
            text = json.dumps(result, ensure_ascii=False, indent=2)[:2500]
            return f"✅ {product_type.upper()} 解析成功\n\n{text}"
        return f"❌ 解析失败，请检查URL或产品类型。"
    except Exception as e:
        return f"解析失败: {str(e)}"


# ==================== 新闻资讯 ====================

@server.tool()
async def crawl_financial_news(limit: int = 10) -> str:
    """爬取最新金融新闻资讯。来源：东方财富、同花顺。
    适用场景：了解最新金融动态、市场热点、政策变化。

    Args:
        limit: 返回条数，默认10，最多30
    """
    try:
        if EastMoneyNewsAPI is None:
            return _module_error("news_scraper")
        scraper = EastMoneyNewsAPI()
        # v4.4.0 修复：fetch_latest() 不存在，改用 get_market_news()
        try:
            news = scraper.get_market_news(limit=min(limit, 30))
        except Exception:
            news = scraper.get_stock_news(limit=min(limit, 30))
        if not news:
            return "暂无新闻数据。"
        lines = [f"📰 最新金融新闻 ({len(news)}条)", ""]
        for i, item in enumerate(news, 1):
            title = item.get('title', '无标题')
            source = item.get('source', '')
            date = item.get('date', '')
            lines.append(f"  {i:>2}. [{source}] {title}  ({date})")
        return '\n'.join(lines)
    except Exception as e:
        return f"新闻爬取失败: {str(e)}"


# ==================== 公告搜索与下载 ====================

@server.tool()
async def search_announcements(keyword: str, limit: int = 10) -> str:
    """搜索上市公司公告。覆盖沪深两市上市公司公告。
    适用场景：查询某公司或某关键词的最新公告。

    Args:
        keyword: 搜索关键词，如"华夏基金"、"分红"、"业绩预告"
        limit: 返回条数，默认10
    """
    try:
        if AnnouncementManager is None:
            return _module_error("announcement_scraper")
        scraper = AnnouncementManager()
        announcements = scraper.search(keyword, limit=min(limit, 20))
        if not announcements:
            return f"未找到包含「{keyword}」的公告。"
        lines = [f"📋 公告搜索结果「{keyword}」({len(announcements)}条)", ""]
        for i, ann in enumerate(announcements, 1):
            lines.append(
                f"  {i:>2}. {ann.get('title', '无标题')}  "
                f"{ann.get('date', '')}  {ann.get('code', '')}"
            )
        lines.append("")
        lines.append("提示: 需要下载PDF可使用 download_announcement 工具")
        return '\n'.join(lines)
    except Exception as e:
        return f"公告搜索失败: {str(e)}"


@server.tool()
async def download_announcement(url: str, save_dir: str = "") -> str:
    """下载公告PDF文件到本地。
    需先通过 search_announcements 获取公告URL。

    Args:
        url: 公告PDF的下载链接
        save_dir: 保存目录（可选，默认保存到 data/announcements/）
    """
    try:
        if PDFDownloader is None:
            return _module_error("announcement_scraper")
        scraper = PDFDownloader()
        output_dir = save_dir or str(SCRIPT_DIR / "data" / "announcements")
        os.makedirs(output_dir, exist_ok=True)
        path = scraper.download(url, save_dir=output_dir)
        return f"✅ PDF已下载到: {path}"
    except Exception as e:
        return f"下载失败: {str(e)}"


# ==================== 券商研报查询 ====================

@server.tool()
async def query_broker_reports(stock_code: str = "", broker_name: str = "") -> str:
    """查询券商研究报告。支持按股票代码或券商名称筛选。
    适用场景：查询个股的最新券商研报评级、目标价、分析观点。

    Args:
        stock_code: 6位股票代码，如 600519（可选）
        broker_name: 券商名称，如"中金公司"、"中信证券"（可选）
    """
    try:
        if BrokerReportManager is None:
            return _module_error("research_report_scraper")
        manager = BrokerReportManager()

        if stock_code:
            result = manager.generate_report_summary(stock_code)
            if result:
                return f"【{stock_code} 券商研报】\n\n{result[:3000]}"
            return f"未找到 {stock_code} 的券商研报。"

        if broker_name:
            reports = manager.get_reports_by_broker(broker_name)
            if reports:
                lines = [f"📊 {broker_name} 研报 ({len(reports)}条)", ""]
                for r in reports[:15]:
                    lines.append(
                        f"  {r.get('stock_code', '')} {r.get('stock_name', '')}  "
                        f"评级:{r.get('rating', '')}  目标价:{r.get('target_price', '?')}"
                    )
                return '\n'.join(lines)
            return f"未找到 {broker_name} 的研报。"

        return "请提供 stock_code 或 broker_name 参数。"
    except Exception as e:
        return f"查询失败: {str(e)}"


# ==================== 综合报告查询 ====================

@server.tool()
async def get_company_reports(stock_code: str) -> str:
    """获取上市公司综合报告（年报/半年报/季报+券商研报）。
    适用场景：一站式查询某只股票的定期报告和最新研报。

    Args:
        stock_code: 6位股票代码
    """
    try:
        if ComprehensiveReportManager is None:
            return _module_error("comprehensive_report_scraper")
        manager = ComprehensiveReportManager()
        data = manager.get_all_reports(stock_code)
        if not data:
            return f"未获取到 {stock_code} 的报告数据。"
        summary = manager.generate_report_summary(stock_code)
        return f"【{stock_code} 综合报告】\n\n{summary[:3000]}"
    except Exception as e:
        return f"查询失败: {str(e)}"


# ==================== 文档解析 ====================

@server.tool()
async def parse_document(file_path: str) -> str:
    """解析金融文档内容（PDF/Word/Excel）。
    适用场景：读取金融PDF研报、Word报告、Excel数据表格。

    Args:
        file_path: 文档完整路径
    """
    try:
        if doc_parse is None:
            return _module_error("document_parser")
        ext = Path(file_path).suffix.lower()
        supported = {'.pdf', '.docx', '.doc', '.xlsx', '.xls', '.txt', '.md', '.csv'}
        if ext not in supported:
            return f"不支持的文件格式: {ext}。支持: {', '.join(sorted(supported))}"

        result = doc_parse(file_path)
        if isinstance(result, dict) and result.get("error"):
            return f"❌ {result['error']}"

        text = result.get("text_content", "") if isinstance(result, dict) else str(result)
        if text:
            content = text[:3000]
            return f"✅ 解析成功 ({len(text)}字符)\n\n{content}"
        return "❌ 解析失败，文件可能已损坏或加密。"
    except Exception as e:
        return f"解析失败: {str(e)}"


# ==================== 研报导出 ====================

@server.tool()
async def export_stock_report(stock_code: str, formats: str = "xlsx") -> str:
    """导出上市公司研究报告。支持PPT/PDF/Word/Excel多种格式。
    适用场景：将股票研究数据导出为可打印或可编辑的文档。

    Args:
        stock_code: 6位股票代码
        formats: 导出格式，逗号分隔，如"ppt,docx,xlsx"（默认xlsx）
    """
    try:
        if ComprehensiveExporter is None or ComprehensiveReportManager is None:
            return _module_error("report_exporter 或 comprehensive_report_scraper")

        manager = ComprehensiveReportManager()
        data = manager.get_all_reports(stock_code)

        if not data:
            return f"未获取到 {stock_code} 的报告数据。"

        exporter = ComprehensiveExporter()
        fmt_list = [f.strip() for f in formats.split(",")]
        output = str(SCRIPT_DIR / "data" / "reports")
        os.makedirs(output, exist_ok=True)

        lines = [f"📁 导出 {stock_code} 报告", ""]
        for fmt in fmt_list:
            try:
                if fmt == 'ppt':
                    from report_exporter import PPTExporter
                    path = PPTExporter().export_comprehensive_report(data, stock_code, output)
                elif fmt == 'docx':
                    from report_exporter import WordExporter
                    path = WordExporter().export_comprehensive_report(data, stock_code, output)
                elif fmt == 'xlsx':
                    from report_exporter import ExcelExporter
                    path = ExcelExporter().export_comprehensive_report(data, stock_code, output)
                elif fmt == 'pdf':
                    from report_exporter import PDFExporter
                    path = PDFExporter().export_comprehensive_report(data, stock_code, output)
                else:
                    lines.append(f"  ❌ 不支持格式: {fmt}，支持: ppt, docx, xlsx, pdf")
                    continue
                lines.append(f"  ✅ .{fmt} -> {path}")
            except Exception as e:
                lines.append(f"  ❌ .{fmt} 导出失败: {str(e)}")

        return '\n'.join(lines)
    except ImportError as e:
        return f"缺少导出依赖库: {e}。请安装: python-pptx / reportlab / openpyxl"
    except Exception as e:
        return f"导出失败: {str(e)}"


# ==================== 批量爬取 ====================

@server.tool()
async def batch_crawl_institutions(names: str = "",
                                   institution_type: str = "") -> str:
    """批量爬取金融机构信息。支持按名称列表或机构类型批量操作。
    适用场景：需要同时获取多家机构的官网最新信息。

    Args:
        names: 机构名称列表，逗号分隔，如"易方达基金,华夏基金,广发基金"
        institution_type: 机构类型，如"基金管理公司"、"证券公司"（与names二选一）
    """
    try:
        if BatchInstitutionCrawler is None:
            return _module_error("batch_institution_crawler")
        crawler = BatchInstitutionCrawler()

        if names:
            inst_list = [{"name": n.strip()} for n in names.split(",")]
            results = crawler.crawl_by_names(inst_list)
        elif institution_type:
            results = crawler.crawl_by_type(institution_type)
        else:
            return "请提供 names 或 institution_type 参数。"

        lines = [f"📊 批量爬取完成 ({len(results)}家)", ""]
        for r in results[:20]:
            name = r.get('name', '?')
            status = '✅' if r.get('success') else '❌'
            lines.append(f"  {status} {name}")
        if len(results) > 20:
            lines.append(f"  ... 还有 {len(results)-20} 家")

        return '\n'.join(lines)
    except Exception as e:
        return f"批量爬取失败: {str(e)}"


# ==================== 全量索引查询 ====================

@server.tool()
async def search_report_index(keyword: str, report_type: str = "") -> str:
    """在全量报告索引中搜索，支持按类型筛选。
    适用场景：搜索全市场股票报告中包含某关键词的条目。

    Args:
        keyword: 搜索关键词，如"分红"、"回购"、"业绩预增"
        report_type: 报告类型 — periodic(定期报告) / broker(券商研报)（可选）
    """
    try:
        if StockIndexer is None:
            return _module_error("report_indexer")
        indexer = StockIndexer()
        results = indexer.search_reports(
            keyword,
            report_type=report_type if report_type else None
        )
        if not results:
            return f"索引中未找到包含「{keyword}」的报告。"
        lines = [f"🔍 索引搜索「{keyword}」({len(results)}条)", ""]
        for r in results[:15]:
            lines.append(
                f"  {r.get('code', '')} {r.get('name', '')}  "
                f"{r.get('title', '')}  {r.get('date', '')}"
            )
        if len(results) > 15:
            lines.append(f"  ... 还有 {len(results)-15} 条")
        lines.append("")
        lines.append("提示: 使用 get_company_reports 查看具体报告内容")
        return '\n'.join(lines)
    except Exception as e:
        return f"搜索失败: {str(e)}"


# ==================== 文档分析整理 ====================

@server.tool()
async def analyze_document(file_path: str, focus: str = "") -> str:
    """深度分析金融文档。自动分类（年报/研报/公告等），提取元数据（标题/机构/日期/股票代码）、
    章节结构、财务指标、风险因素、术语表，生成结构化 Markdown 报告。
    适用场景：分析单份 PDF/Word/Excel 金融文档，提取关键信息。

    Args:
        file_path: 文档完整路径
        focus: 重点关注维度（可选）：财务指标 / 风险 / 章节 / all（默认全部）
    """
    try:
        if DocumentAnalyzer is None:
            return _module_error("document_analyzer")
        analyzer = DocumentAnalyzer()
        result = analyzer.analyze(file_path)
        if result.get("error"):
            return f"❌ 分析失败: {result['error']}"

        # 根据 focus 剪裁输出
        if focus == "财务指标" or focus == "指标":
            indicators = result.get("financial_indicators", {})
            if not indicators:
                return "未提取到财务指标。"
            lines = [f"【{Path(file_path).name} 财务指标】", ""]
            for k, v in indicators.items():
                if k not in ("amounts_yi", "total_yi", "percentages"):
                    lines.append(f"  {k}: {v}")
            return '\n'.join(lines)
        elif focus == "风险":
            risks = result.get("risk_factors", [])
            if not risks:
                return "未提取到风险因素。"
            lines = [f"【{Path(file_path).name} 风险因素】({len(risks)}条)", ""]
            for i, r in enumerate(risks, 1):
                lines.append(f"  {i}. {r}")
            return '\n'.join(lines)
        elif focus == "章节":
            sections = result.get("sections", [])
            if not sections:
                return "未识别到章节结构。"
            lines = [f"【{Path(file_path).name} 章节结构】({len(sections)}个)", ""]
            for sec in sections[:25]:
                indent = "  " * (sec.get("level", 1) - 1)
                lines.append(f"{indent}{'#' * sec.get('level', 1)} {sec['title']}")
            return '\n'.join(lines)

        # 默认输出完整 Markdown
        return analyzer.to_markdown(result)
    except Exception as e:
        return f"分析失败: {str(e)}"


@server.tool()
async def organize_documents(dir_path: str) -> str:
    """批量整理文档目录。扫描指定目录下所有 PDF/Word/Excel 文件，
    逐份分析分类后按类别聚合，生成索引文件（JSON）和汇总报告（Markdown）。
    适用场景：整理一个文件夹中的研报、财报、公告等金融文档。

    Args:
        dir_path: 文档目录路径
    """
    try:
        if DocumentAnalyzer is None:
            return _module_error("document_analyzer")
        analyzer = DocumentAnalyzer()
        result = analyzer.organize_directory(dir_path)

        if result.get("error"):
            return f"❌ 整理失败: {result['error']}"

        lines = [
            f"📁 文档目录整理完成",
            f"  来源: {dir_path}",
            f"  文档总数: {result['total']}",
            f"  类别数: {len(result.get('categories', {}))}",
            "",
            "【按类别统计】",
        ]
        for cat, count in sorted(result.get("categories", {}).items(), key=lambda x: -x[1]):
            lines.append(f"  {cat}: {count}份")

        lines.append("")
        lines.append(f"📄 索引文件: {result.get('index_file', '')}")
        lines.append(f"📝 汇总报告: {result.get('summary_file', '')}")

        return '\n'.join(lines)
    except Exception as e:
        return f"整理失败: {str(e)}"


@server.tool()
async def compare_documents(file_paths: str) -> str:
    """并排对比多份金融文档。提取相同维度（类型/日期/机构/关键指标等），
    高亮差异，适合对比多家公司的年报或不同券商的研报。

    Args:
        file_paths: 文档路径列表，逗号分隔，如 "report_a.pdf,report_b.pdf"
    """
    try:
        if DocumentAnalyzer is None:
            return _module_error("document_analyzer")
        paths = [p.strip() for p in file_paths.split(",") if p.strip()]
        if len(paths) < 2:
            return "请提供至少2份文档路径（逗号分隔）。"

        analyzer = DocumentAnalyzer()
        result = analyzer.compare(paths)

        lines = [
            f"📊 文档对比 ({result['compared']}份)",
            "",
            "【各文档概览】",
        ]
        for f in result.get("files", []):
            lines.append(f"  📄 {f['name']} — {f['category']} | {f['summary'][:80]}...")

        lines.append("\n【维度对比】")
        for dim, values in result.get("dimensions", {}).items():
            if dim == "关键指标":
                continue
            lines.append(f"  {dim}: {'  |  '.join(str(v) for v in values)}")

        differences = result.get("differences", [])
        if differences:
            lines.append(f"\n【⚠️ 发现 {len(differences)} 处差异】")
            for diff in differences:
                lines.append(f"  • {diff}")
        else:
            lines.append(f"\n✅ 未发现明显差异")

        # 关键指标对比
        indicators = result.get("dimensions", {}).get("关键指标", [])
        if indicators and len(indicators) >= 2:
            lines.append("\n【关键财务指标对比】")
            all_keys = set()
            for ind in indicators:
                all_keys.update(k for k in ind.keys() if k not in ("amounts_yi", "total_yi", "percentages"))
            if all_keys:
                for key in sorted(all_keys):
                    vals = [str(ind.get(key, "-")) for ind in indicators]
                    lines.append(f"  {key}: {'  |  '.join(vals)}")

        return '\n'.join(lines)
    except Exception as e:
        return f"对比失败: {str(e)}"


# ==================== 实时行情 (v3.0 新增) ====================

@server.tool()
async def get_stock_realtime(stock_codes: str) -> str:
    """获取A股实时行情。数据来源：新浪财经。
    适用场景：查询个股实时价格、涨跌幅、成交量、成交额等。

    Args:
        stock_codes: 股票代码，多个用逗号分隔，如 "600519,000858,300750"
    """
    try:
        if sina_quote is None:
            return _module_error("sina_scraper")
        codes = [c.strip() for c in stock_codes.split(",") if c.strip()]
        if not codes:
            return "请提供至少一个股票代码。"
        data = sina_quote(codes)
        if not data:
            return "未获取到行情数据，请检查股票代码是否正确。"
        lines = [f"📊 实时行情 ({len(data)}只)", ""]
        for code, info in data.items():
            lines.append(
                f"  {info.get('name', '?')} ({code})  "
                f"价格:{info.get('price', '?')}  "
                f"涨跌:{info.get('change_pct', 0):+.2f}%  "
                f"成交:{info.get('amount', 0):.0f}万"
            )
        return '\n'.join(lines)
    except Exception as e:
        return f"行情查询失败: {str(e)}"


# ==================== 基金净值历史 (v3.0 新增) ====================

@server.tool()
async def get_fund_nav_history(fund_code: str, days: int = 30) -> str:
    """获取基金历史净值数据。数据来源：天天基金。
    适用场景：查看基金近期净值走势、计算收益率。

    Args:
        fund_code: 6位基金代码，如 000001
        days: 查询天数，默认30天
    """
    try:
        url = f"https://fund.eastmoney.com/f10/F10DataApi.aspx?type=lsjz&code={fund_code}&page=1&per={min(days, 60)}"
        from http_utils import fetch_text
        text = fetch_text(url)
        if not text:
            return f"未获取到基金 {fund_code} 的净值数据。"
        import re
        records = re.findall(r'<tr[^>]*>.*?<td[^>]*>(\d{4}-\d{2}-\d{2})</td>.*?<td[^>]*class="tor[^"]*">([\d.]+)</td>.*?<td[^>]*class="tor[^"]*">([\d.]+)</td>', text, re.DOTALL)
        if not records:
            return f"未获取到基金 {fund_code} 的净值历史。"
        lines = [f"📈 基金 {fund_code} 历史净值 (近{days}天, 共{len(records)}条)", ""]
        for date, nav, acc_nav in records[:20]:
            lines.append(f"  {date}  单位净值:{nav}  累计净值:{acc_nav}")
        if len(records) > 20:
            lines.append(f"  ... 还有 {len(records)-20} 条")
        return '\n'.join(lines)
    except Exception as e:
        return f"净值查询失败: {str(e)}"


# ==================== 财联社电报 (v3.0 新增) ====================

@server.tool()
async def crawl_cls_telegraph(limit: int = 20) -> str:
    """获取财联社7x24小时电报快讯。数据来源：财联社(cls.cn)。
    适用场景：获取最新市场快讯、政策消息、公司公告速递。

    Args:
        limit: 返回条数，默认20，最多50
    """
    try:
        if cls_telegraph is None:
            return _module_error("cls_scraper")
        items = cls_telegraph(limit=min(limit, 50))
        if not items:
            return "暂无财联社电报数据。"
        lines = [f"📡 财联社7x24电报 ({len(items)}条)", ""]
        for i, item in enumerate(items, 1):
            level_icon = {"A": "🔴", "B": "🟡", "C": "🟢"}.get(item.get("level", "C"), "⚪")
            title = (item.get("title", "") or "")[:100]
            lines.append(f"  {i:>2}. {level_icon} {title}")
        return '\n'.join(lines)
    except Exception as e:
        return f"电报获取失败: {str(e)}"


# ==================== 可转债数据 (v3.0 新增) ====================

@server.tool()
async def get_convertible_bond_data(bond_code: str = "") -> str:
    """获取可转债数据（转股价/溢价率/到期收益率/评级等）。
    数据来源：集思录(jisilu.cn)。
    适用场景：可转债投资分析，筛选低溢价率标的。

    Args:
        bond_code: 可转债代码（可选，留空返回全量列表）
    """
    try:
        if jisilu_bonds is None:
            return _module_error("jisilu_scraper")
        if bond_code:
            bonds = jisilu_search(bond_code)
        else:
            bonds = jisilu_bonds(listed_only=True)
        if not bonds:
            return "未获取到可转债数据，请检查集思录是否可访问。"
        lines = [f"📊 可转债数据 ({len(bonds)}只)", ""]
        lines.append(f"  {'代码':<8} {'名称':<12} {'现价':>6} {'转股价':>6} {'溢价率':>6}")
        lines.append("  " + "-" * 50)
        for b in bonds[:20]:
            lines.append(
                f"  {b.get('bond_id', '?'):<8} {str(b.get('bond_nm', '?'))[:12]:<12} "
                f"{float(b.get('price', 0)):>6.2f} {float(b.get('convert_price', 0)):>6.2f} "
                f"{float(b.get('premium_rt', 0)):>5.1f}%"
            )
        if len(bonds) > 20:
            lines.append(f"  ... 还有 {len(bonds)-20} 只")
        return '\n'.join(lines)
    except Exception as e:
        return f"可转债查询失败: {str(e)}"


# ==================== v4.0 定期自动爬取 ====================

@server.tool()
async def schedule_crawl_task(name: str, frequency: str = "daily",
                               urls: str = "", keywords: str = "",
                               institution_type: str = "",
                               action: str = "crawl_and_compress",
                               focus: str = "全面",
                               max_runs: int = 0,
                               sentiment_targets: str = "",
                               sentiment_categories: str = "",
                               sentiment_source_categories: str = "",
                               sentiment_days: int = 7,
                               sentiment_positive_only: bool = False,
                               sentiment_negative_only: bool = False,
                               sentiment_export_format: str = "all") -> str:
    """创建定期自动爬取任务。任务将在后台按指定频率自动执行。

    支持两种模式：
      A. 常规爬取（默认值 action="crawl_and_compress"）— 同 v4.0
      B. 全网舆情爬虫 — action 传 "crawl_sentiment" 或 "crawl_sentiment_export"

    模式 B 额外参数：
        sentiment_targets: 单/多个机构（逗号分隔），如"贵州茅台,工银瑞信基金"
        sentiment_categories: 目标类别（fund_company/listed_company/...）
        sentiment_source_categories: 媒体类别（authoritative/self_media/...）
        sentiment_days: 时间窗口（天，默认7）
        sentiment_positive_only: 仅保留正面
        sentiment_negative_only: 仅保留舆情
        sentiment_export_format: 导出格式 dialog/word/excel/csv/json/all/auto

    Args:
        name: 任务名称，如"每日茅台舆情"
        frequency: 执行频率 — every_5_minutes / hourly / every_6_hours / daily / weekly / monthly / custom_cron
        urls: 目标URL列表，逗号分隔（可选）
        keywords: 搜索关键词，逗号分隔（可选）
        institution_type: 机构类型（可选）
        action: 执行动作 — crawl_only / crawl_and_compress / crawl_and_package / crawl_compress_package / crawl_sentiment / crawl_sentiment_export
        focus: 压缩关注维度 — 财务/风险/行业/政策/事件/全面
        max_runs: 最大执行次数，0表示无限
    """
    try:
        if get_scheduler is None:
            return _module_error("crawl_scheduler")
        scheduler = get_scheduler()

        url_list = [u.strip() for u in urls.split(",") if u.strip()] if urls else []
        kw_list = [k.strip() for k in keywords.split(",") if k.strip()] if keywords else []
        inst_types = [institution_type] if institution_type else []
        st_list = [t.strip() for t in sentiment_targets.split(",") if t.strip()] if sentiment_targets else []
        sc_list = [c.strip() for c in sentiment_categories.split(",") if c.strip()] if sentiment_categories else []
        ssc_list = [c.strip() for c in sentiment_source_categories.split(",") if c.strip()] if sentiment_source_categories else []

        task = scheduler.create_task(
            name=name, frequency=frequency,
            target_urls=url_list, target_keywords=kw_list,
            target_institution_types=inst_types,
            action=action, focus_dimension=focus,
            max_runs=max_runs,
            sentiment_targets=st_list,
            sentiment_categories=sc_list,
            sentiment_source_categories=ssc_list,
            sentiment_days=sentiment_days,
            sentiment_positive_only=sentiment_positive_only,
            sentiment_negative_only=sentiment_negative_only,
            sentiment_export_format=sentiment_export_format,
        )

        if not scheduler.is_running():
            scheduler.start()

        mode_hint = ""
        if action in ("crawl_sentiment", "crawl_sentiment_export"):
            mode_hint = (f"   🆕 全网舆情模式: targets={st_list or '默认'} 类别={sc_list or '默认'} "
                         f"媒类别={ssc_list or '全部'} days={sentiment_days}\n")

        return (
            f"✅ 定时任务已创建\n"
            f"   ID: {task.task_id}\n"
            f"   名称: {task.name}\n"
            f"   频率: {task.frequency.value}\n"
            f"   动作: {task.action.value}\n"
            f"   下次执行: {task.next_run_at}\n"
            f"{mode_hint}"
            f"   \n💡 使用 list_scheduled_tasks 查看所有任务"
        )
    except Exception as e:
        return f"创建任务失败: {str(e)}"


@server.tool()
async def list_scheduled_tasks(status: str = "") -> str:
    """查看所有定期爬取任务及执行状态。

    Args:
        status: 按状态筛选 — active / paused / completed / error（留空查看全部）
    """
    try:
        if get_scheduler is None:
            return _module_error("crawl_scheduler")
        scheduler = get_scheduler()
        tasks = scheduler.list_tasks(status=status if status else None)

        if not tasks:
            return "📋 暂无定时任务。使用 schedule_crawl_task 创建新任务。"

        lines = [f"📋 定时任务列表 ({len(tasks)} 个):", ""]
        for t in tasks:
            freq_map = {
                "every_minute": "每分钟", "every_5_minutes": "每5分钟",
                "every_10_minutes": "每10分钟", "every_30_minutes": "每30分钟",
                "hourly": "每小时", "every_6_hours": "每6小时",
                "every_12_hours": "每12小时", "daily": "每天",
                "weekly": "每周", "monthly": "每月",
            }
            freq_label = freq_map.get(t.frequency.value, t.frequency.value)
            status_icon = {"active": "🟢", "paused": "🟡", "completed": "✅", "error": "🔴"}.get(t.status.value, "⚪")
            lines.append(f"  {status_icon} [{t.task_id}] {t.name}")
            lines.append(f"     频率: {freq_label} | 动作: {t.action.value} | 已执行: {t.run_count}次")
            if t.last_run_at:
                lines.append(f"     上次: {t.last_run_at}")
            if t.next_run_at:
                lines.append(f"     下次: {t.next_run_at}")
            if t.last_error:
                lines.append(f"     错误: {t.last_error[:100]}")
            lines.append("")

        lines.append(f"调度器状态: {'▶️ 运行中' if scheduler.is_running() else '⏸️ 已停止'}")
        return '\n'.join(lines)
    except Exception as e:
        return f"查询失败: {str(e)}"


@server.tool()
async def cancel_scheduled_task(task_id: str, action: str = "delete") -> str:
    """取消或暂停定期爬取任务。

    Args:
        task_id: 任务ID（从 list_scheduled_tasks 获取）
        action: 操作 — delete(删除) / pause(暂停) / resume(恢复)
    """
    try:
        if get_scheduler is None:
            return _module_error("crawl_scheduler")
        scheduler = get_scheduler()

        if action == "pause":
            ok = scheduler.pause_task(task_id)
            return f"⏸️ 任务 {task_id} 已暂停" if ok else f"❌ 未找到任务 {task_id}"
        elif action == "resume":
            ok = scheduler.resume_task(task_id)
            return f"▶️ 任务 {task_id} 已恢复" if ok else f"❌ 未找到任务 {task_id}"
        else:
            ok = scheduler.delete_task(task_id)
            return f"🗑️ 任务 {task_id} 已删除" if ok else f"❌ 未找到任务 {task_id}"
    except Exception as e:
        return f"操作失败: {str(e)}"


# ==================== v4.0 批量爬取打包 ZIP ====================

@server.tool()
async def batch_crawl_and_package(names: str = "",
                                   institution_type: str = "",
                                   zip_name: str = "") -> str:
    """批量爬取机构信息并自动打包为 ZIP 文件。

    Args:
        names: 机构名称列表，逗号分隔，如"易方达基金,华夏基金"
        institution_type: 机构类型，如"基金管理公司"（与names二选一）
        zip_name: 自定义ZIP文件名（可选）
    """
    try:
        if _batch_crawl_and_package is None:
            return _module_error("crawl_packager")
        if not names and not institution_type:
            return "请提供 names 或 institution_type 参数。"

        path = _batch_crawl_and_package(
            names=names, institution_type=institution_type, zip_name=zip_name
        )
        size_mb = os.path.getsize(path) / (1024 * 1024) if os.path.exists(path) else 0
        return f"📦 ZIP 打包完成\n   文件: {path}\n   大小: {size_mb:.2f} MB"
    except Exception as e:
        return f"打包失败: {str(e)}"


# ==================== v4.0 内容压缩 ====================

@server.tool()
async def compress_crawl_results(source: str = "",
                                  file_path: str = "",
                                  focus: str = "全面",
                                  max_pages: int = 3) -> str:
    """分析爬取内容或文件，提取关键信息并压缩为 2-3 页结构化摘要。

    适用场景：一大段爬取内容/一份PDF/一份研报 → 2-3页精华摘要。

    Args:
        source: 文本内容（直接输入）
        file_path: 文件路径（与source二选一）
        focus: 关注维度 — 财务 / 风险 / 行业 / 政策 / 事件 / 全面
        max_pages: 目标页数（默认3）
    """
    try:
        if compress_content is None:
            return _module_error("content_compressor")

        target = file_path if file_path else source
        if not target:
            return "请提供 source（文本）或 file_path（文件路径）。"

        config = CompressConfig(focus=focus, max_pages=max_pages,
                               max_chars=max_pages * 1000)
        result = compress_content(target, focus=focus)

        lines = [
            f"📄 内容压缩报告",
            f"标题: {result.title}",
            f"摘要: {result.summary[:200]}",
            f"",
            f"--- 压缩报告 ---",
            result.structured_report,
            f"",
            f"📊 压缩统计: 原文 {result.stats['source_chars']:,} 字 → "
            f"压缩后 {result.stats['output_chars']:,} 字 "
            f"({result.stats['compression_ratio']})",
        ]
        return '\n'.join(lines)
    except Exception as e:
        return f"压缩失败: {str(e)}"


# ==================== v4.0 增强文件解析 ====================

@server.tool()
async def parse_file_enhanced(file_path: str, fmt: str = "auto") -> str:
    """增强文件解析器。支持 PPT/PPTX、HTML网页、Markdown、CSV、JSON 等格式。
    自动检测文件类型，提取文本、表格、元数据等。

    适用场景：解析 PPT 演示文稿、网页内容、Markdown 文档等。

    Args:
        file_path: 文件路径或URL
        fmt: 格式 — auto(自动检测) / pptx / html / markdown / csv / json
    """
    try:
        if _parse_file_enhanced is None:
            return _module_error("enhanced_parser")
        result = _parse_file_enhanced(file_path, fmt)
        if result.get("error"):
            return f"❌ {result['error']}"

        lines = [f"✅ 解析成功: {result.get('file_type', '?').upper()}"]
        if result.get("title"):
            lines.append(f"标题: {result['title']}")
        if result.get("slide_count"):
            lines.append(f"幻灯片: {result['slide_count']} 页")
        if result.get("tables"):
            lines.append(f"表格: {len(result['tables'])} 个")
        if result.get("sections"):
            lines.append(f"章节: {len(result.get('sections', result.get('headings', [])))} 个")
        if result.get("char_count"):
            lines.append(f"字符: {result['char_count']:,}")

        text = result.get("text_content", "")[:2000]
        lines.append(f"\n--- 前2000字符 ---\n{text}")
        return '\n'.join(lines)
    except Exception as e:
        return f"解析失败: {str(e)}"


# ==================== v4.0 深度文件分析 ====================

@server.tool()
async def analyze_file_deep(file_path: str, analysis_type: str = "full") -> str:
    """深度分析文件内容。提取主题、情感倾向、趋势信号等。

    Args:
        file_path: 文件路径
        analysis_type: 分析类型 — full(全面) / topics(主题) / financial(财务) / risk(风险)
    """
    try:
        # 先解析
        if parse_file_enhanced is None:
            return _module_error("enhanced_parser")
        parsed = _parse_file_enhanced(file_path)
        if parsed.get("error"):
            return f"❌ 解析失败: {parsed['error']}"

        # 再压缩分析
        if compress_content is None:
            return _module_error("content_compressor")

        text = parsed.get("text_content", "")[:10000]
        if not text:
            return "❌ 文件内容为空"

        focus_map = {"full": "全面", "topics": "行业", "financial": "财务", "risk": "风险"}
        focus = focus_map.get(analysis_type, "全面")

        result = compress_content(text, focus=focus)

        lines = [f"🔍 深度文件分析: {Path(file_path).name}", ""]
        lines.append(f"## 文档分类")
        try:
            from document_analyzer import classify_document
            cat, conf = classify_document(text, file_path)
            lines.append(f"  {cat} (置信度: {conf}%)")
        except ImportError:
            pass

        lines.append(f"\n## 关键发现\n")
        for i, pt in enumerate(result.key_points, 1):
            lines.append(f"  {i}. {pt}")

        lines.append(f"\n## 财务指标 ({len(result.financial_highlights)} 项)")
        for k, v in list(result.financial_highlights.items())[:8]:
            lines.append(f"  {k}: {v}")

        if result.risk_summary:
            lines.append(f"\n## 风险因素 ({len(result.risk_summary)} 条)")
            for r in result.risk_summary[:5]:
                lines.append(f"  ⚠️ {r}")

        lines.append(f"\n📊 原文 {result.stats['source_chars']:,} 字 | "
                     f"压缩比 {result.stats['compression_ratio']}")
        return '\n'.join(lines)
    except Exception as e:
        return f"分析失败: {str(e)}"


# ==================== v4.0 研究报告生成 ====================

@server.tool()
async def generate_research_report(source: str = "",
                                    theme: str = "stock_research",
                                    title: str = "",
                                    focus: str = "全面",
                                    file_path: str = "",
                                    stock_code: str = "",
                                    output_formats: str = "markdown") -> str:
    """生成图文并茂的金融研究报告。支持从URL/文件/股票代码一键生成。
    自动采集数据 → 分析压缩 → 报告写作 → 图表生成 → 多格式导出。

    适用场景：生成个股研报、行业分析、基金评价、市场周报等。

    Args:
        source: 文本内容或URL（可选）
        theme: 报告主题 — stock_research(个股研报) / industry_analysis(行业分析) / fund_evaluation(基金评价) / institution_survey(机构调研) / market_weekly(市场周报) / announcement_brief(公告解读)
        title: 报告标题（可选，自动生成）
        focus: 关注维度 — 财务/风险/行业/政策/事件/全面
        file_path: 文件路径（可选，作为数据源）
        stock_code: 6位股票代码（可选，自动获取报告数据）
        output_formats: 输出格式，逗号分隔 — markdown/docx/pptx/html/pdf
    """
    try:
        if ResearchReportGenerator is None:
            return _module_error("research_report_generator")

        # 构建配置
        from research_report_generator import ReportConfig, ReportTheme, OutputFormat

        config = ReportConfig(
            theme=ReportTheme(theme),
            title=title,
            focus_dimension=focus,
        )

        if source and source.startswith(('http://', 'https://')):
            config.urls = [source]
        elif file_path and os.path.isfile(file_path):
            config.files = [file_path]
        elif stock_code:
            config.stock_codes = [stock_code]
        elif source:
            config.custom_data = {"raw_text": source}

        # 设置输出格式
        fmt_map = {"markdown": OutputFormat.MARKDOWN, "docx": OutputFormat.DOCX,
                    "pptx": OutputFormat.PPTX, "html": OutputFormat.HTML,
                    "pdf": OutputFormat.PDF}
        config.output_formats = [
            fmt_map[f.strip()] for f in output_formats.split(",")
            if f.strip() in fmt_map and f.strip() != "markdown"
        ]

        gen = ResearchReportGenerator()
        result = gen.generate(config)

        lines = [result.to_summary()]
        lines.append(f"\n--- 报告预览 (前500字) ---")
        lines.append(result.markdown_content[:500])
        if len(result.markdown_content) > 500:
            lines.append(f"\n... 全文共 {len(result.markdown_content):,} 字")
        lines.append(f"\n📊 图表: {len(result.chart_paths)} 张")

        return '\n'.join(lines)
    except Exception as e:
        return f"报告生成失败: {str(e)}"


@server.tool()
async def export_research_report(markdown_content: str = "",
                                  file_path: str = "",
                                  output_formats: str = "docx",
                                  title: str = "研究报告") -> str:
    """将 Markdown 报告内容导出为 Word/PPT/HTML/PDF 格式。

    Args:
        markdown_content: Markdown 文本内容（与file_path二选一）
        file_path: Markdown 文件路径（与markdown_content二选一）
        output_formats: 导出格式，逗号分隔 — docx/pptx/html/pdf
        title: 报告标题
    """
    try:
        if file_path and os.path.isfile(file_path):
            with open(file_path, 'r', encoding='utf-8') as f:
                content = f.read()
        elif markdown_content:
            content = markdown_content
        else:
            return "请提供 markdown_content 或 file_path。"

        from research_report_generator import ReportResult, ReportConfig, OutputFormat
        from datetime import datetime

        # 构造 ReportResult
        result = ReportResult(
            config=ReportConfig(),
            title=title,
            template_id="custom",
            markdown_content=content,
            created_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        )

        # 导出
        gen = ResearchReportGenerator()
        fmt_map = {"markdown": OutputFormat.MARKDOWN, "docx": OutputFormat.DOCX,
                    "pptx": OutputFormat.PPTX, "html": OutputFormat.HTML,
                    "pdf": OutputFormat.PDF}

        config = ReportConfig()
        config.output_formats = [
            fmt_map[f.strip()] for f in output_formats.split(",")
            if f.strip() in fmt_map
        ]

        output_files = gen._export(result, config)

        lines = [f"📦 报告导出结果", ""]
        for fmt, path in output_files.items():
            size = os.path.getsize(path) if os.path.exists(path) else 0
            lines.append(f"  ✅ [{fmt.upper()}] {path} ({size/1024:.1f} KB)")

        return '\n'.join(lines)
    except Exception as e:
        return f"导出失败: {str(e)}"


# ==================== v4.0 快速摘要 ====================

@server.tool()
async def quick_crawl_summary(url_or_file: str,
                               focus: str = "全面") -> str:
    """一键快速爬取+压缩。输入 URL 或文件路径，自动爬取/解析并输出 2-3 页精华摘要。

    Args:
        url_or_file: URL 或文件路径
        focus: 关注维度 — 财务/风险/行业/政策/事件/全面
    """
    try:
        if compress_content is None:
            return _module_error("content_compressor")

        # 判断是URL还是文件
        if url_or_file.startswith(('http://', 'https://')):
            # 爬取
            if FinancialPageScraper is None:
                return _module_error("scraper")
            scraper = FinancialPageScraper()
            content = scraper.scrape_url(url_or_file)
            if not content:
                return f"❌ 爬取失败: {url_or_file}"
            text = str(content)[:15000]
        elif os.path.isfile(url_or_file):
            # 解析
            if parse_file_enhanced:
                parsed = parse_file_enhanced(url_or_file)
                text = parsed.get("text_content", "")[:15000]
            else:
                with open(url_or_file, 'r', encoding='utf-8') as f:
                    text = f.read()[:15000]
        else:
            return f"❌ 无法识别来源: {url_or_file}"

        if not text:
            return "❌ 内容为空"

        result = compress_content(text, focus=focus)

        lines = [
            f"⚡ 快速摘要",
            f"来源: {url_or_file}",
            f"标题: {result.title}",
            f"",
            result.structured_report,
            f"",
            f"📊 {result.stats['source_chars']:,} 字 → "
            f"{result.stats['output_chars']:,} 字 ({result.stats['compression_ratio']})",
        ]
        return '\n'.join(lines)
    except Exception as e:
        return f"快速摘要失败: {str(e)}"


# ==================== 主入口 ====================


# ==================== v4.3 全网舆情爬虫 ====================

@server.tool()
async def crawl_global_sentiment(
    targets: str = "",
    target_categories: str = "",
    sources: str = "",
    source_categories: str = "",
    days: int = 7,
    positive_only: bool = False,
    negative_only: bool = False,
    max_articles: int = 80,
    fmt: str = "dialog",
    run_backtest: bool = True,
    dry_run: bool = False,
    strict_date: bool = True) -> str:
    """🆕 v4.6 全网舆情爬虫（正面新闻+舆情）+ 强制日期过滤 + 数据回测。

    根据用户对话要求，从权威媒体/财经垂直/地方媒体/自媒体/国际媒体
    采集基金公司/上市公司/证券公司/银行/保险/信托等机构的
    正面新闻与舆情。

    🛡️ v4.6 增强：
      - 严格日期过滤：仅返回请求时间窗内的文章（默认启用）
      - 默认开启数据回测（4维验证：新鲜度/交叉源/历史快照/数字一致性）
      - 爬取后日期核验统计（date_validation 字段）
      - 不延展爬取范围：严格按用户指定的目标/媒体/时间窗执行

    支持：
      - 指定单个或多个机构（targets，逗号分隔），仅看正面或仅看舆情
      - 限定目标类别（fund_company / listed_company / securities 等）
      - 限定媒体类别（authoritative / financial_vertical / self_media / international）
      - 时间窗口（默认 7 天，严格过滤）
      - 多种输出方式（dialog/word/excel/csv/json/all/auto）
      - 数据回测（默认开启，对每条结果做 4 维验证）
      - 预览模式（dry_run=True 时仅展示爬取计划，不发 HTTP 请求）

    Args:
        targets: 目标名列表，逗号分隔（如"贵州茅台,工银瑞信基金,招商银行"）
        target_categories: 目标类别，逗号分隔（fund_company/listed_company/securities/commercial_bank/insurance/trust_company/private_fund/foreign_institution/futures/wealth_management/leasing_consumer_finance，留空=选 top 类）
        sources: 媒体名称列表（精确匹配，可选）
        source_categories: 媒体类别，逗号分隔（authoritative/financial_vertical/self_media/international）
        days: 时间窗口，默认7（严格过滤，不返回时间窗外文章）
        positive_only: 仅保留正面新闻
        negative_only: 仅保留舆情
        max_articles: 最大文章数
        fmt: 输出格式 dialog/word/excel/csv/json/all/auto
        run_backtest: 是否对每条结果做 4 维数据回测（默认开启）
        dry_run: 是否仅预览爬取计划（不发 HTTP 请求，用于爬取前确认）
        strict_date: 是否严格日期过滤（默认True，丢弃时间窗外文章）
    """
    try:
        if _crawl_sentiment is None:
            return _module_error("sentiment_crawler")
        # 解析参数
        target_list = [t.strip() for t in targets.split(",") if t.strip()] or None
        cat_list = [c.strip() for c in target_categories.split(",") if c.strip()] or None
        src_list = [s.strip() for s in sources.split(",") if s.strip()] or None
        sc_list = [c.strip() for c in source_categories.split(",") if c.strip()] or None

        snapshot = _crawl_sentiment(
            targets=target_list,
            categories=cat_list,
            sources=src_list,
            source_categories=sc_list,
            days=days,
            positive_only=positive_only,
            negative_only=negative_only,
            max_articles=max_articles,
            dry_run=dry_run,
            confirmed=not dry_run,
            run_backtest=run_backtest,
        )

        if not snapshot or not snapshot.articles:
            return (
                "📭 本次未获取到结果，建议：\n"
                "  · 放宽时间窗 days=30\n"
                "  · 添加更多 media source / target\n"
                "  · 重试或检查网络"
            )

        # 输出
        if fmt == "dialog":
            from sentiment_exporter import to_dialog
            return to_dialog(snapshot)

        if _export_sentiment is None:
            return _module_error("sentiment_exporter")
        outputs = _export_sentiment(snapshot, fmt=fmt)
        lines = [
            f"✅ 全网舆情快照 {snapshot.snapshot_id}",
            f"   共 {snapshot.stats.get('total', 0)} 条 | 正面 {snapshot.positive_count()} | "
            f"舆情 {snapshot.negative_count()} | 中性 {snapshot.neutral_count()}",
            "",
        ]
        for k, v in outputs.items():
            if k == "dialog":
                lines.append(f"--- 对话提示 ---\n{v}")
            else:
                lines.append(f"📦 [{k.upper()}] {v}")
        lines.append(f"\n💡 提示：可继续运行 export_sentiment_report 重新导出，或查看 data/sentiment_snapshots/")
        return '\n'.join(lines)
    except Exception as e:
        return f"❌ 舆情爬取失败: {str(e)}"


@server.tool()
async def export_sentiment_report(snapshot_id: str = "",
                                  file_path: str = "",
                                  fmt: str = "all") -> str:
    """🆕 v4.3 导出已有舆情快照。可直接传入快照ID或快照JSON文件路径。

    Args:
        snapshot_id: 快照ID（snapshot_id 形如 sn_xxxx），自动从 data/sentiment_snapshots/ 找到
        file_path: 快照JSON文件路径（与snapshot_id二选一）
        fmt: 输出格式 dialog/word/excel/csv/json/all/auto
    """
    try:
        if _export_sentiment is None:
            return _module_error("sentiment_exporter")

        snap_file: Optional[Path] = None
        if file_path and Path(file_path).is_file():
            snap_file = Path(file_path)
        elif snapshot_id:
            base = SCRIPT_DIR / "data" / "sentiment_snapshots"
            candidate = base / f"{snapshot_id}.json"
            if candidate.exists():
                snap_file = candidate
            else:
                index_file = base / "index.json"
                if index_file.exists():
                    for item in json.loads(index_file.read_text(encoding="utf-8")):
                        if item.get("snapshot_id") == snapshot_id:
                            snap_file = Path(item["path"])
                            break
        if not snap_file:
            return f"❌ 未找到快照: {snapshot_id or file_path}"

        # 加载快照
        from sentiment_crawler import SentimentSnapshot, SentimentArticle  # type: ignore
        raw = json.loads(snap_file.read_text(encoding="utf-8"))
        arts = [SentimentArticle(**a) for a in raw.get("articles", [])]
        snap = SentimentSnapshot(
            snapshot_id=raw["snapshot_id"], created_at=raw["created_at"],
            target_filter=raw.get("target_filter", {}),
            source_filter=raw.get("source_filter", []),
            articles=arts,
            stats=raw.get("stats", {}),
        )
        snap.extra_path = str(snap_file)  # type: ignore[attr-defined]

        outputs = _export_sentiment(snap, fmt=fmt)
        lines = []
        for k, v in outputs.items():
            if k == "dialog":
                lines.append(v)
            else:
                lines.append(f"📦 [{k.upper()}] {v}")
        return '\n'.join(lines) if lines else "无输出"
    except Exception as e:
        return f"❌ 导出失败: {str(e)}"


@server.tool()
async def list_sentiment_targets() -> str:
    """🆕 v4.3 查看全网舆情爬虫的目标库。
    返回的分类：fund_company / listed_company / securities /
                commercial_bank / insurance / trust_company / private_fund /
                foreign_institution / futures / wealth_management / leasing_consumer_finance
    """
    try:
        if _list_sentiment_targets is None:
            return _module_error("sentiment_crawler")
        items = _list_sentiment_targets()
        lines = ["🎯 全网舆情目标库", ""]
        for it in items:
            lines.append(f"  · {it.get('category','')} — {it.get('label','')} ({it.get('count',0)} 个)")
        lines.append("")
        lines.append("💡 用法：\n  · crawl_global_sentiment(targets='工银瑞信基金,贵州茅台')\n"
                     "  · crawl_global_sentiment(target_categories='fund_company,listed_company')")
        return '\n'.join(lines)
    except Exception as e:
        return f"查询失败: {str(e)}"


@server.tool()
async def list_sentiment_sources(category: str = "") -> str:
    """🆕 v4.3 查看全网舆情爬虫的媒体源库。

    Args:
        category: 媒体类别（authoritative/financial_vertical/self_media/international），留空=全部
    """
    try:
        if SentimentSourceLoader is None:
            return _module_error("sentiment_crawler")
        loader = SentimentSourceLoader()
        if category:
            items = loader.sources_by_category(category)
            items = [(category, x) for x in items]
        else:
            items = [(loader._data[k][0].get('category', k) if loader._data.get(k) else k, x)
                     for k in loader.list_categories()
                     for x in loader.sources_by_category(k)]
        lines = [f"📰 全网舆情媒体源 (类别: {category or '全部'})", ""]
        for cat, item in items:
            tags = ",".join(item.get("tags", []) or [])
            lines.append(f"  · [{cat}] {item.get('name','')} — {tags}")
        return '\n'.join(lines)
    except Exception as e:
        return f"查询失败: {str(e)}"


@server.tool()
async def add_sentiment_target(category: str, name: str, aliases: str = "") -> str:
    """🆕 v4.3 新增自定义舆情目标。

    Args:
        category: 目标类别（fund_company / listed_company / securities / commercial_bank / insurance / trust_company / private_fund / foreign_institution / futures / wealth_management / leasing_consumer_finance / custom）
        name: 目标名称
        aliases: 别名列表（逗号分隔，可选）
    """
    try:
        if _add_custom_target is None:
            return _module_error("sentiment_crawler")
        alias_list = [a.strip() for a in aliases.split(",") if a.strip()] or None
        result = _add_custom_target(category, name, alias_list)
        if result.get("ok"):
            return f"✅ 已新增 {category} - {name}（含 {len(alias_list or [])} 个别名）"
        return f"⚠️ {result.get('msg', '未知错误')}"
    except Exception as e:
        return f"❌ 新增失败: {str(e)}"


# ==================== v4.5 全页归档 + 搜索工具 ====================

try:
    from fullpage_archiver import FullPageArchiver as _FullPageArchiver, \
        quick_archive as _quick_archive
    _HAS_ARCHIVER = True
except ImportError:
    _HAS_ARCHIVER = False

try:
    from search_engine import MultiEngineSearch as _MultiEngineSearch, \
        search_and_fetch as _search_and_fetch
    _HAS_SEARCH = True
except ImportError:
    _HAS_SEARCH = False

# v4.7 新增数据源（东方财富数据中心 / 市场宏观 / 北交所 / 浏览器 / 监管政策 / 对比回测）
try:
    from eastmoney_scraper import (
        get_margin_trading as _get_margin_trading,
        get_block_trades as _get_block_trades,
        get_etf_fund_flow as _get_etf_fund_flow,
        get_dragon_tiger_seats as _get_dragon_tiger_seats,
        get_shareholder_changes as _get_shareholder_changes,
    )
except ImportError:
    _get_margin_trading = _get_block_trades = _get_etf_fund_flow = \
        _get_dragon_tiger_seats = _get_shareholder_changes = None

try:
    from market_data_scraper import get_lpr_rates as _get_lpr_rates, \
        get_bond_yield_curve as _get_bond_yield_curve
except ImportError:
    _get_lpr_rates = _get_bond_yield_curve = None

try:
    from exchange_scraper import get_bse_stocks as _get_bse_stocks
except ImportError:
    _get_bse_stocks = None

try:
    from data_comparator import compare_and_backtest as _compare_and_backtest, \
        format_comparison_report as _format_comparison_report
except ImportError:
    _compare_and_backtest = _format_comparison_report = None

try:
    from regulatory_scraper import analyze_policy_document as _analyze_policy_document, \
        search_policy_documents as _search_policy_documents, \
        crawl_policy_documents as _crawl_policy_documents
except ImportError:
    _analyze_policy_document = _search_policy_documents = _crawl_policy_documents = None


@server.tool()
async def archive_webpage(url: str, paginate: bool = False,
                          next_selector: str = "",
                          max_pages: int = 5,
                          output_mode: str = "both") -> str:
    """v4.5 全页归档：下载网页全部内容（文字+图片+图表+表格），生成自包含 HTML。

    支持翻页抓取、图片 base64 内嵌、Canvas 截图、表格提取。
    输出模式: "inline"(单文件内嵌) / "directory"(独立目录) / "both"(两者都要，默认)

    Args:
        url: 目标网页 URL
        paginate: 是否自动翻页
        next_selector: 下一页按钮 CSS 选择器（翻页时必填，如 ".next,a[rel=next]"）
        max_pages: 最大翻页数（默认 5）
        output_mode: 输出模式（默认 both）
    """
    if not _HAS_ARCHIVER:
        return _module_error("fullpage_archiver")
    try:
        result = _quick_archive(
            url, paginate=paginate,
            next_selector=next_selector or None,
            max_pages=max_pages,
            inline_images=output_mode in ("inline", "both"),
            save_assets=output_mode in ("directory", "both"),
        )
        return result.summary
    except Exception as e:
        return f"❌ 归档失败: {str(e)}"


@server.tool()
async def search_web(query: str, engines: str = "",
                     limit: int = 10) -> str:
    """v4.5 多引擎搜索：跨 DuckDuckGo/Bing/SearXNG 搜索，返回结构化结果。

    零 API Key 即可使用（优先 DuckDuckGo + SearXNG，Bing HTML 兜底）。

    Args:
        query: 搜索关键词（如 "贵州茅台 2026年报"）
        engines: 指定引擎（逗号分隔，如 "duckduckgo,bing_html"），空=自动选择
        limit: 返回条数（默认 10）
    """
    if not _HAS_SEARCH:
        return _module_error("search_engine")
    try:
        engine_list = [e.strip() for e in engines.split(",") if e.strip()] or None
        s = _MultiEngineSearch(engines=engine_list)
        results = s.search(query, limit=limit)
        if not results:
            return "🔍 未找到相关结果。可尝试换关键词或稍后重试。"
        lines = [f"🔍 搜索: {query} | 共 {len(results)} 条"]
        for i, r in enumerate(results, 1):
            lines.append(
                f"{i}. [{r.source_engine}] {r.title[:80]}\n"
                f"   {r.url[:100]}\n"
                f"   {r.snippet[:120]}"
            )
        return "\n\n".join(lines)
    except Exception as e:
        return f"❌ 搜索失败: {str(e)}"


@server.tool()
async def search_and_archive(query: str, engines: str = "",
                             limit: int = 5,
                             fetch_content: bool = True) -> str:
    """v4.5 搜索+归档一步完成：搜索 → 获取 URL → 爬取详情 → 返回结构化内容。

    Args:
        query: 搜索关键词
        engines: 指定引擎（逗号分隔），空=自动选择
        limit: 结果条数（默认 5）
        fetch_content: 是否自动爬取详情页文本（默认 True）
    """
    if not _HAS_SEARCH:
        return _module_error("search_engine")
    try:
        results = _search_and_fetch(query, limit=limit, fetch_content=fetch_content)
        if not results:
            return "🔍 未找到相关结果。"
        lines = [f"🔍 搜索+归档: {query} | 共 {len(results)} 条"]
        for i, r in enumerate(results, 1):
            content_preview = (r.get("content", "") or "")[:200]
            lines.append(
                f"{i}. {r['title'][:80]}\n"
                f"   来源: {r.get('source_engine', '?')} | "
                f"可信度: {r.get('credibility', '?')}/10\n"
                f"   URL: {r['url'][:100]}\n"
                f"   摘要: {r.get('snippet', '')[:120]}\n"
                f"   正文: {content_preview}..."
            )
        return "\n\n".join(lines)
    except Exception as e:
        return f"❌ 搜索归档失败: {str(e)}"


# ==================== v4.7 新增工具 ====================


def _format_data_rows(rows, limit: int = 20) -> str:
    """将数据行列表格式化为可读文本（默认最多 20 行）。"""
    if not rows:
        return "（无数据）"
    if isinstance(rows, dict):
        rows = [rows]
    lines = [f"共 {len(rows)} 条："]
    for row in rows[:limit]:
        if isinstance(row, dict):
            parts = []
            for k, v in row.items():
                if v is None or v == "" or v == 0.0:
                    continue
                parts.append(f"{k}={v}")
            lines.append("  " + " | ".join(parts))
        else:
            lines.append(f"  {row}")
    if len(rows) > limit:
        lines.append(f"  … 等 {len(rows)} 条（仅显示前 {limit}）")
    return "\n".join(lines)


def _dry_run_preview(data_type: str, params: dict) -> str:
    """数据爬取前的确认预览（不实际执行请求）。"""
    lines = [
        f"📋 爬取计划确认（{data_type}）",
        "=" * 40,
        "以下参数将用于本次爬取：",
    ]
    for k, v in params.items():
        if v not in (None, "", 0, False):
            lines.append(f"  • {k}: {v}")
    lines.append("")
    lines.append("⚠️ 如需执行请去掉 dry_run=True 后再次调用。")
    return "\n".join(lines)


@server.tool()
async def get_margin_trading(market: str = "all", limit: int = 50,
                             start_date: str = "", end_date: str = "",
                             dry_run: bool = False) -> str:
    """v4.7 融资融券（两融）余额数据：融资余额/融券余额/融资净买入等。

    Args:
        market: all / sz / sh / bj
        limit: 返回条数（默认 50）
        start_date: 起始日期 yyyy-MM-dd（严格过滤）
        end_date: 截止日期 yyyy-MM-dd
        dry_run: True=仅预览爬取计划不执行（默认 False）
    """
    if dry_run:
        return _dry_run_preview("两融数据", {"market": market, "limit": limit,
                                            "start_date": start_date, "end_date": end_date})
    if _get_margin_trading is None:
        return _module_error("eastmoney_scraper")
    try:
        rows = _get_margin_trading(market=market, limit=limit,
                                   start_date=start_date, end_date=end_date)
        return _format_data_rows(rows)
    except Exception as e:
        return f"❌ 获取两融数据失败: {str(e)}"


@server.tool()
async def get_block_trades(stock_code: str = "", start_date: str = "",
                           end_date: str = "", limit: int = 50,
                           dry_run: bool = False) -> str:
    """v4.7 大宗交易数据：成交价/折溢价率/买卖营业部。

    Args:
        stock_code: 股票代码（可选）
        start_date: 起始日期 yyyy-MM-dd
        end_date: 截止日期 yyyy-MM-dd
        limit: 返回条数（默认 50）
        dry_run: True=仅预览不执行
    """
    if dry_run:
        return _dry_run_preview("大宗交易", {"stock_code": stock_code, "limit": limit,
                                            "start_date": start_date, "end_date": end_date})
    if _get_block_trades is None:
        return _module_error("eastmoney_scraper")
    try:
        rows = _get_block_trades(stock_code=stock_code, start_date=start_date,
                                 end_date=end_date, limit=limit)
        return _format_data_rows(rows)
    except Exception as e:
        return f"❌ 获取大宗交易失败: {str(e)}"


@server.tool()
async def get_etf_fund_flow(sort_by: str = "net_inflow", limit: int = 50,
                            dry_run: bool = False) -> str:
    """v4.7 ETF 资金流向：主力/超大单/大单净流入排行。

    Args:
        sort_by: net_inflow(主力净流入) / change_pct(涨跌幅)
        limit: 返回条数（默认 50）
        dry_run: True=仅预览不执行
    """
    if dry_run:
        return _dry_run_preview("ETF资金流向", {"sort_by": sort_by, "limit": limit})
    if _get_etf_fund_flow is None:
        return _module_error("eastmoney_scraper")
    try:
        rows = _get_etf_fund_flow(sort_by=sort_by, limit=limit)
        return _format_data_rows(rows)
    except Exception as e:
        return f"❌ 获取 ETF 资金流失败: {str(e)}"


@server.tool()
async def get_dragon_tiger_seats(stock_code: str, date: str = "") -> str:
    """v4.7 龙虎榜席位详情：买卖营业部、总买/总卖/净买入。

    Args:
        stock_code: 股票代码（必填）
        date: 日期 yyyy-MM-dd（可选，默认最近）
    """
    if _get_dragon_tiger_seats is None:
        return _module_error("eastmoney_scraper")
    try:
        r = _get_dragon_tiger_seats(stock_code, date)
        return _format_data_rows(r)
    except Exception as e:
        return f"❌ 获取龙虎榜席位失败: {str(e)}"


@server.tool()
async def get_shareholder_changes(stock_code: str = "", change_type: str = "all",
                                  start_date: str = "", end_date: str = "",
                                  limit: int = 50, dry_run: bool = False) -> str:
    """v4.7 股东/高管增减持数据。

    Args:
        stock_code: 股票代码（可选）
        change_type: all / 增持 / 减持
        start_date: 起始日期 yyyy-MM-dd
        end_date: 截止日期 yyyy-MM-dd
        limit: 返回条数（默认 50）
        dry_run: True=仅预览不执行
    """
    if dry_run:
        return _dry_run_preview("股东增减持", {"stock_code": stock_code,
                                              "change_type": change_type, "limit": limit})
    if _get_shareholder_changes is None:
        return _module_error("eastmoney_scraper")
    try:
        rows = _get_shareholder_changes(stock_code=stock_code, change_type=change_type,
                                        start_date=start_date, end_date=end_date, limit=limit)
        return _format_data_rows(rows)
    except Exception as e:
        return f"❌ 获取股东增减持失败: {str(e)}"


@server.tool()
async def get_lpr_rates(days: int = 365, dry_run: bool = False) -> str:
    """v4.7 LPR 贷款市场报价利率（1年期/5年期）。

    Args:
        days: 返回最近 N 天内的报价（默认 365）
        dry_run: True=仅预览不执行
    """
    if dry_run:
        return _dry_run_preview("LPR利率", {"days": days})
    if _get_lpr_rates is None:
        return _module_error("market_data_scraper")
    try:
        rows = _get_lpr_rates(days=days)
        lines = ["📊 LPR 贷款市场报价利率："]
        for r in rows[:12]:
            lines.append(f"  {r['date']}: 1年期 {r['one_year']}% | 5年期 {r['five_year']}%")
        if len(rows) > 12:
            lines.append(f"  … 共 {len(rows)} 条")
        return "\n".join(lines) if rows else "（无数据）"
    except Exception as e:
        return f"❌ 获取 LPR 失败: {str(e)}"


@server.tool()
async def get_bond_yield_curve(dry_run: bool = False) -> str:
    """v4.7 国债收益率曲线（1Y-30Y 各期限收益率 + 利差）。

    Args:
        dry_run: True=仅预览不执行
    """
    if dry_run:
        return _dry_run_preview("国债收益率曲线", {"curve_type": "treasury"})
    if _get_bond_yield_curve is None:
        return _module_error("market_data_scraper")
    try:
        r = _get_bond_yield_curve()
        if "error" in r:
            return f"❌ {r.get('error')}（{r.get('warning', '')}）"
        terms = r.get("terms", {})
        lines = [f"📈 {r.get('curve_type')}（{r.get('date')}）"]
        for k in ("1Y", "2Y", "3Y", "5Y", "7Y", "10Y", "30Y"):
            if k in terms:
                lines.append(f"  {k}: {terms[k]}%")
        if "spread_10_2" in r:
            lines.append(f"  10Y-2Y 利差: {r['spread_10_2']}%")
        if "spread_30_10" in r:
            lines.append(f"  30Y-10Y 利差: {r['spread_30_10']}%")
        return "\n".join(lines)
    except Exception as e:
        return f"❌ 获取国债收益率失败: {str(e)}"


@server.tool()
async def get_bse_stocks(limit: int = 200, dry_run: bool = False) -> str:
    """v4.7 北交所上市公司列表。

    Args:
        limit: 返回条数（默认 200）
        dry_run: True=仅预览不执行
    """
    if dry_run:
        return _dry_run_preview("北交所上市公司列表", {"limit": limit})
    if _get_bse_stocks is None:
        return _module_error("exchange_scraper")
    try:
        rows = _get_bse_stocks(limit=limit)
        lines = [f"🏛 北交所上市公司（{len(rows)} 家）："]
        for r in rows[:20]:
            lines.append(f"  {r.get('code')} {r.get('name')} "
                         f"上市:{r.get('listing_date', '')} 行业:{r.get('industry', '')}")
        if len(rows) > 20:
            lines.append(f"  … 共 {len(rows)} 家")
        return "\n".join(lines)
    except Exception as e:
        return f"❌ 获取北交所列表失败: {str(e)}"


@server.tool()
async def browser_human_fetch(url: str, scroll: bool = True,
                              mouse_simulation: bool = True,
                              dwell: bool = True,
                              random_viewport: bool = True,
                              input_text: str = "",
                              input_selector: str = "",
                              think_time: bool = True,
                              hover: bool = True,
                              tab_switch: bool = True,
                              include_iframes: bool = False) -> str:
    """v6.0.0 类人浏览器抓取：模拟真实用户（鼠标移动+阅读停顿+悬停+多标签+随机滚动）获取高反爬网页。

    Args:
        url: 目标网页 URL
        scroll: 是否随机滚动（默认 True）
        mouse_simulation: 是否模拟贝塞尔鼠标移动（默认 True）
        dwell: 是否自适应停留阅读（默认 True）
        random_viewport: 是否随机 viewport（默认 True）
        input_text: 需要模拟输入的文本（如搜索关键词），空则不输入
        input_selector: 输入框 CSS 选择器（input_text 非空时建议提供）
        think_time: v6.0.0 阅读停顿（加载后停留 2-8s，默认 True）
        hover: v6.0.0 随机悬停菜单触发下拉内容（默认 True）
        tab_switch: v6.0.0 低频开新标签页再切回（默认 True）
        include_iframes: v6.0.0 合并内嵌 iframe 内容（默认 False）
    """
    try:
        from browser_scraper import BrowserScraper
        PLAYWRIGHT_AVAILABLE = True
    except ImportError:
        try:
            from scripts.browser_scraper import BrowserScraper
            PLAYWRIGHT_AVAILABLE = True
        except ImportError:
            PLAYWRIGHT_AVAILABLE = False

    if not PLAYWRIGHT_AVAILABLE:
        return ("❌ 浏览器模块不可用。请安装:\n"
                "  pip install playwright\n"
                "  playwright install chromium")
    try:
        with BrowserScraper(headless=True, stealth=True) as bs:
            html = bs.humanlike_fetch(
                url, scroll=scroll, dwell=dwell,
                random_viewport=random_viewport,
                mouse_simulation=mouse_simulation,
                input_selector=input_selector or None,
                input_text=input_text or None,
                think_time=think_time,
                hover=hover,
                tab_switch=tab_switch,
                include_iframes=include_iframes,
            )
        if not html:
            return "❌ 抓取失败：目标网站可能拒绝访问、超时，或触发验证码（截图已存 data/screenshots/）。"
        # 提取可见文本摘要
        import re
        text = re.sub(r"<[^>]+>", " ", html)
        text = re.sub(r"\s+", " ", text).strip()
        return f"✅ 类人抓取成功（{len(html)} 字符）\n\n{text[:3000]}{'…' if len(text) > 3000 else ''}"
    except Exception as e:
        return f"❌ 浏览器抓取异常: {str(e)}"


@server.tool()
async def analyze_policy_document(url_or_path: str, keyword: str = "") -> str:
    """v4.7 下载并分析监管政策文件/公告（标题/日期/关键词命中/摘要）。

    Args:
        url_or_path: 政策文件 URL 或本地文件路径
        keyword: 关注关键词（如 "利率"、"风险"），可多个用逗号分隔
    """
    if _analyze_policy_document is None:
        return _module_error("regulatory_scraper")
    try:
        r = _analyze_policy_document(url_or_path, keyword=keyword)
        if "error" in r:
            return f"❌ {r['error']}"
        lines = [f"📄 {r.get('title', '')}"]
        if r.get("date"):
            lines.append(f"  日期: {r['date']}")
        lines.append(f"  篇幅: {r.get('content_length', 0)} 字符")
        if r.get("keywords_hit"):
            lines.append(f"  命中关键词: {'、'.join(r['keywords_hit'])}")
        if r.get("summary"):
            lines.append(f"  摘要: {r['summary'][:400]}")
        if r.get("saved_path"):
            lines.append(f"  已保存: {r['saved_path']}")
        return "\n".join(lines)
    except Exception as e:
        return f"❌ 分析政策文件失败: {str(e)}"


@server.tool()
async def search_policy_documents(keyword: str, agency: str = "all",
                                  limit: int = 20) -> str:
    """v4.7 跨监管机构检索政策文件/公告。

    Args:
        keyword: 搜索关键词
        agency: all / pboc / csrc / nfra
        limit: 返回条数（默认 20）
    """
    if _search_policy_documents is None:
        return _module_error("regulatory_scraper")
    try:
        rows = _search_policy_documents(keyword, agency, limit)
        if not rows:
            return "🔍 未找到相关政策文件。"
        lines = [f"📋 监管政策文件检索「{keyword}」（{len(rows)} 条）："]
        for r in rows[:15]:
            doc_flag = "📎" if r.get("doc_url") else "🔗"
            lines.append(f"  {doc_flag} [{r.get('source', '')}] {r.get('title', '')[:70]}")
            lines.append(f"      {r.get('url', '')[:90]}")
        return "\n".join(lines)
    except Exception as e:
        return f"❌ 检索政策文件失败: {str(e)}"


@server.tool()
async def crawl_policy_documents(agency: str = "all", limit: int = 10,
                                 dry_run: bool = False) -> str:
    """v4.7 爬取并下载监管机构最新政策文件/公告（含内容分析）。

    Args:
        agency: all / pboc / csrc / nfra
        limit: 下载数量上限（默认 10）
        dry_run: True=仅预览不执行
    """
    if dry_run:
        return _dry_run_preview("监管政策爬取", {"agency": agency, "limit": limit})
    if _crawl_policy_documents is None:
        return _module_error("regulatory_scraper")
    try:
        r = _crawl_policy_documents(agency=agency, limit=limit)
        lines = [f"🏛 监管政策爬取完成：发现 {r['total_found']} 篇，下载 {r['downloaded']} 篇"]
        for doc in r["documents"][:10]:
            saved = "✅" if doc.get("saved_path") else "⏭"
            lines.append(f"  {saved} [{doc.get('source', '')}] {doc.get('title', '')[:60]}")
            if doc.get("saved_path"):
                lines.append(f"      {doc['saved_path']}")
        for w in r.get("warnings", [])[:3]:
            lines.append(f"  ⚠️ {w}")
        return "\n".join(lines)
    except Exception as e:
        return f"❌ 爬取政策文件失败: {str(e)}"


@server.tool()
async def compare_financial_data(series_json: str, date_key: str = "date",
                                 value_fields: str = "") -> str:
    """v4.7 同类金融数据横向对比 + 数据质量回测。

    传入 JSON 形式的 {"标签": [记录列表], ...}，对多组同类数据做对比排名与回测评分。

    Args:
        series_json: JSON 字符串，如 {"贵州茅台": [{"date":"2026-07-31","margin_balance":100}], "五粮液": [...]}
        date_key: 日期字段名（默认 date）
        value_fields: 指定数值字段（逗号分隔，空=自动识别）
    """
    if _compare_and_backtest is None or _format_comparison_report is None:
        return _module_error("data_comparator")
    try:
        import json as _json
        series = _json.loads(series_json)
        if not isinstance(series, dict) or not series:
            return "❌ series_json 需为非空 JSON 对象"
        fields = [f.strip() for f in value_fields.split(",") if f.strip()] or None
        result = _compare_and_backtest(series, date_key=date_key, value_fields=fields)
        return _format_comparison_report(result)
    except json.JSONDecodeError as e:
        return f"❌ series_json 解析失败: {e}"
    except Exception as e:
        return f"❌ 对比回测失败: {str(e)}"


# ==================== v5.0.0 新增分析工具 ====================


@server.tool()
async def analyze_risk_metrics(nav_json: str, risk_free_rate: float = 0.02) -> str:
    """v5.0.0 产品净值风险指标分析（真实计算：年化收益/年化波动/最大回撤/夏普/卡玛）。

    Args:
        nav_json: 净值时间序列 JSON（升序，≥30 点才计算真实指标），
            如 [{"date":"2026-01-01","nav":1.2}, {"date":"2026-01-02","nav":1.21}]
        risk_free_rate: 无风险利率（默认 0.02 = 2%）
    """
    try:
        from analyzer import calculate_risk_metrics  # type: ignore
    except ImportError:
        return _module_error("analyzer")
    try:
        navs = json.loads(nav_json)
        if not isinstance(navs, list) or not navs:
            return "❌ nav_json 需为非空 JSON 数组"
        metrics = calculate_risk_metrics(navs, risk_free_rate=risk_free_rate)
        return json.dumps(metrics, ensure_ascii=False, indent=2)
    except json.JSONDecodeError as e:
        return f"❌ nav_json 解析失败: {e}"
    except Exception as e:
        return f"❌ 风险指标计算失败: {str(e)}"


@server.tool()
async def analyze_product(url_or_data: str, product_type: str = "auto") -> str:
    """v5.0.0 完整产品分析报告（解析 + 真实风险指标 + 风格定位 + 复刻建议）。

    Args:
        url_or_data: 产品页面 URL，或产品数据 JSON 字符串（含 holdings/historical_nav 等）
        product_type: 产品类型提示（基金/ETF/股票/债券，auto 自动判断）
    """
    try:
        from analyzer import analyze_product as _analyze_product  # type: ignore
    except ImportError:
        return _module_error("analyzer")
    try:
        data = url_or_data.strip()
        if data.startswith("{"):
            product_info = json.loads(data)
        else:
            # 从 URL 解析（web_parser 动态导入）
            try:
                import importlib
                wp = importlib.import_module("web_parser")
                result = wp.parse_financial_product(data, product_type=product_type)
                if hasattr(result, "to_dict"):
                    result = result.to_dict()
                product_info = result if isinstance(result, dict) else {}
            except Exception as e:
                return f"❌ 页面解析失败: {e}"
        return _analyze_product(product_info)
    except json.JSONDecodeError as e:
        return f"❌ 产品数据解析失败: {e}"
    except Exception as e:
        return f"❌ 产品分析失败: {str(e)}"


@server.tool()
async def analyze_portfolio_replication(product_json: str, limit: int = 5) -> str:
    """v5.0.0 组合复刻建议（数据驱动替代标的 + 数值距离相似产品推荐）。

    Args:
        product_json: 产品数据 JSON（含 product_name/holdings/historical_nav 等）
        limit: 相似产品返回数量（默认 5）
    """
    try:
        from analyzer import generate_portfolio_replication  # type: ignore
    except ImportError:
        return _module_error("analyzer")
    try:
        product = json.loads(product_json)
        if not isinstance(product, dict):
            return "❌ product_json 需为 JSON 对象"
        rep = generate_portfolio_replication(product)
        if limit and isinstance(rep.get("similar_products"), list):
            rep["similar_products"] = rep["similar_products"][:limit]
        return json.dumps(rep, ensure_ascii=False, indent=2, default=str)
    except json.JSONDecodeError as e:
        return f"❌ product_json 解析失败: {e}"
    except Exception as e:
        return f"❌ 组合复刻失败: {str(e)}"


@server.tool()
async def get_structured_data(data_type: str, params_json: str = "{}") -> str:
    """v5.0.0 结构化市场数据（期货/宏观/同业/港股美股，akshare 低代码零 token）。

    Args:
        data_type: futures_spot / futures_daily / futures_holding / macro_gdp /
                   macro_cpi / macro_ppi / macro_pmi / macro_fx_reserves /
                   shibor / repo_rate / hk_spot / us_spot
        params_json: 可选参数 JSON（如 {"symbol":"V0"} / {"start_year":"2022"}）
    """
    try:
        from structured_market_data import get_structured_data as _get, list_data_types  # type: ignore
    except ImportError:
        return _module_error("structured_market_data")
    try:
        params = json.loads(params_json) if params_json else {}
        result = _get(data_type, **params)
        if result is None:
            types = [t["data_type"] for t in list_data_types()]
            return f"❌ 数据获取失败（akshare 未安装或接口不可用）。可用类型: {types}"
        if hasattr(result, "to_dict"):  # pandas.DataFrame
            result = result.to_dict("records")
        return _format_data_rows(result)
    except json.JSONDecodeError as e:
        return f"❌ params_json 解析失败: {e}"
    except Exception as e:
        return f"❌ 结构化数据获取失败: {str(e)}"


# ==================== v7.0.0 网页操作深度增强 + 机构名单扩充 ====================

@server.tool()
async def discover_api(url: str, wait_seconds: float = 6.0,
                       url_pattern: str = "") -> str:
    """v7.0.0 网络拦截 API 挖掘：打开页面并监听 XHR/fetch，捕获背后的 JSON 接口。

    自动识别分页规律并沉淀到 data/api_registry.json（去重累积，可复用）。
    对挖掘东财式"页面背后接口"价值最大——发现后无需浏览器即可高速直连。

    Args:
        url: 目标页面（SPA 动态页优先）
        wait_seconds: 页面就绪后的继续监听时长（默认 6s，触发懒加载请求）
        url_pattern: 只保留匹配该正则的请求（空=不过滤）
    """
    try:
        from browser_pro import discover_api as _disc  # type: ignore
    except ImportError:
        try:
            from scripts.browser_pro import discover_api as _disc  # type: ignore
        except ImportError:
            return _module_error("browser_pro")
    try:
        result = await asyncio.to_thread(_disc, url, wait_seconds, url_pattern)
        if not result:
            return ("❌ API 挖掘失败：Playwright 未安装或页面打不开。安装:\n"
                    "  pip install playwright && playwright install chromium")
        lines = [f"🔍 捕获 {result['captured_count']} 个 XHR/fetch 请求，"
                 f"新入库接口 {result['registry_saved']} 个"]
        for pg in result.get("pagination", [])[:5]:
            lines.append(f"  📄 分页: {pg['url_template']} 参数={pg['pagination_param']} "
                         f"步长={pg['step']} 出现{pg['times_seen']}次")
        for api in result.get("apis", [])[:15]:
            mark = "✓JSON" if "sample_response" in api else "    "
            lines.append(f"  {mark} [{api['status']}] {api['method']} {api['url'][:130]}")
        lines.append("完整结果已存 data/api_registry.json（ApiRegistry.search 可查询复用）")
        return "\n".join(lines)
    except Exception as e:
        return f"❌ API 挖掘异常: {str(e)}"


@server.tool()
async def play_macro(macro_name: str, params_json: str = "{}") -> str:
    """v7.0.0 回放网页操作宏（JSON 操作序列：goto/click/fill/press/upload/download/drag/extract…）。

    宏文件放 macros/ 目录；支持 {{参数}} 占位符替换。

    Args:
        macro_name: 宏名（macros/ 下的文件名，带不带 .json 均可）
        params_json: 参数 JSON（如 {"keyword":"贵州茅台"}）
    """
    try:
        from browser_pro import play_macro as _play  # type: ignore
    except ImportError:
        try:
            from scripts.browser_pro import play_macro as _play  # type: ignore
        except ImportError:
            return _module_error("browser_pro")
    try:
        params = json.loads(params_json) if params_json else {}
        result = await asyncio.to_thread(_play, macro_name, params)
        if not result.get("ok"):
            return f"❌ 宏回放失败: {result.get('error', '未知错误')}"
        outputs = {k: v for k, v in (result.get("outputs") or {}).items()
                   if k != "_done"}
        summary = []
        for k, v in outputs.items():
            text = v if isinstance(v, str) else json.dumps(v, ensure_ascii=False,
                                                           default=str)
            summary.append(f"  [{k}] {text[:400]}{'…' if len(text) > 400 else ''}")
        return (f"✅ 宏回放成功（{result['steps_done']}/{result['steps_total']} 步，"
                f"产出 {len(summary)} 项）\n" + "\n".join(summary))
    except json.JSONDecodeError as e:
        return f"❌ params_json 解析失败: {e}"
    except Exception as e:
        return f"❌ 宏回放异常: {str(e)}"


@server.tool()
async def browser_upload(url: str, selector: str, file_paths_json: str,
                         submit_selector: str = "") -> str:
    """v7.0.0 文件上传：打开页面 → 选择文件 → （可选）点提交。

    Args:
        url: 目标页面 URL
        selector: input[type=file] 的 CSS 选择器
        file_paths_json: 本地文件路径 JSON 数组，如 ["C:/tmp/a.pdf"]
        submit_selector: 提交按钮选择器（空=不点击）
    """
    try:
        from browser_pro import MacroPlayer  # type: ignore
    except ImportError:
        try:
            from scripts.browser_pro import MacroPlayer  # type: ignore
        except ImportError:
            return _module_error("browser_pro")
    try:
        files = json.loads(file_paths_json)
        macro = {"steps": [{"action": "goto", "url": url},
                           {"action": "upload", "selector": selector,
                            "files": files}]}
        if submit_selector:
            macro["steps"].append({"action": "click", "selector": submit_selector})
            macro["steps"].append({"action": "wait", "seconds": 2})
        macro["steps"].append({"action": "extract", "as": "html"})
        result = await asyncio.to_thread(
            MacroPlayer(headless=True).play, macro)
        ok = result.get("ok")
        html = (result.get("outputs") or {}).get("html", "")
        return (f"{'✅' if ok else '❌'} 上传{'成功' if ok else '失败'}"
                f"（{len(files)} 个文件），提交后页面 {len(html)} 字符")
    except Exception as e:
        return f"❌ 上传异常: {str(e)}"


@server.tool()
async def browser_download(url: str, click_selector: str = "",
                           filename: str = "") -> str:
    """v7.0.0 文件下载：打开页面 → 点击触发下载（或直接下载 url）→ 落盘到 data/downloads/。

    Args:
        url: 目标页面 URL（或直链文件地址）
        click_selector: 触发下载的按钮选择器（空=直接从 url 下载）
        filename: 保存文件名（空=用服务端建议名）
    """
    try:
        from browser_scraper import _get_browser_scraper  # type: ignore
    except ImportError:
        try:
            from scripts.browser_scraper import _get_browser_scraper  # type: ignore
        except ImportError:
            return _module_error("browser_scraper")
    try:
        def _do():
            bs = _get_browser_scraper(headless=True)
            return bs.download_file(url, click_selector=click_selector,
                                    filename=filename) if bs else None
        result = await asyncio.to_thread(_do)
        return (f"✅ 下载成功: {result['path']}" if result
                else "❌ 下载失败（选择器未触发下载或超时）")
    except Exception as e:
        return f"❌ 下载异常: {str(e)}"


@server.tool()
async def async_batch_fetch(urls_json: str, concurrency: int = 4) -> str:
    """v7.0.0 异步并发批量抓取：async Playwright 多标签页池，比逐个抓取快数倍。

    Args:
        urls_json: URL 列表 JSON，如 ["https://a.com", "https://b.com"]
        concurrency: 并发标签页数（默认 4，建议 ≤6）
    """
    try:
        from browser_pro import async_batch_fetch as _batch  # type: ignore
    except ImportError:
        try:
            from scripts.browser_pro import async_batch_fetch as _batch  # type: ignore
        except ImportError:
            return _module_error("browser_pro")
    try:
        urls = json.loads(urls_json)
        if not isinstance(urls, list) or not urls:
            return "❌ urls_json 必须是非空 URL 数组"
        results = await asyncio.to_thread(_batch, urls[:30], concurrency)
        if results is None:
            return ("❌ Playwright 未安装。安装:\n"
                    "  pip install playwright && playwright install chromium")
        ok = sum(1 for _, h in results if h)
        lines = [f"⚡ 并发抓取完成：{ok}/{len(results)} 成功（并发 {concurrency}）"]
        for u, h in results:
            lines.append(f"  {'✅' if h else '❌'} {u[:100]} "
                         f"({len(h) if h else 0} 字符)")
        return "\n".join(lines)
    except Exception as e:
        return f"❌ 并发抓取异常: {str(e)}"


@server.tool()
async def expand_institutions(only: str = "", dry_run: bool = False) -> str:
    """v7.0.0 机构名单扩充：多源合并（实时接口→精选名录→既有名单）补全金融机构名单。

    新增民营银行/外资法人银行/AMC/村镇银行/保险中介等类型，去重后并入
    data/institution_registry.json 并同步 *_list.json 与舆情目标计数。

    Args:
        only: 只处理某类别（如 fund_company，空=全部）
        dry_run: 只统计不写文件（默认 False）
    """
    try:
        from institution_expander import run as _run  # type: ignore
    except ImportError:
        try:
            from scripts.institution_expander import run as _run  # type: ignore
        except ImportError:
            return _module_error("institution_expander")
    try:
        report = await asyncio.to_thread(_run, dry_run, only)
        lines = [f"🏛 名单扩充{'（dry-run）' if dry_run else ''}完成："]
        for cat, info in report.items():
            lines.append(f"  {info['type']}: +{info['added']} → 共 {info['total']}"
                         f"（{info['source']}）")
        return "\n".join(lines) if report else "ℹ️ 无新增数据（精选名录未就绪或已全部合并）"
    except Exception as e:
        return f"❌ 名单扩充异常: {str(e)}"


@server.tool()
async def complete_institution_urls(limit: int = 30, type_name: str = "") -> str:
    """v7.0.0 机构官网补全：多引擎搜索+可达性验证，为无官网机构批量回填 URL（断点续跑）。

    Args:
        limit: 本次处理上限（默认 30，建议 ≤50 避免搜索引擎限流）
        type_name: 只处理该类型（如 期货公司，空=全部缺 URL 机构）
    """
    try:
        from url_completer import UrlCompleter  # type: ignore
    except ImportError:
        try:
            from scripts.url_completer import UrlCompleter  # type: ignore
        except ImportError:
            return _module_error("url_completer")
    try:
        def _do():
            return UrlCompleter().run(limit=limit, type_filter=type_name)
        stats = await asyncio.to_thread(_do)
        return (f"🔗 官网补全完成：尝试 {stats['tried']}，成功 {stats['filled']}，"
                f"验证失败 {stats['unverified']}，过滤聚合/敏感域 "
                f"{stats['skipped_blocked']}（进度已存档，支持断点续跑）")
    except Exception as e:
        return f"❌ 官网补全异常: {str(e)}"


@server.tool()
async def update_stock_list() -> str:
    """v7.0.0 更新 A 股全量上市公司名单（东财多镜像→新浪双源容灾），
    输出 data/listed_companies.json，供舆情 listed_company 目标匹配。"""
    try:
        from stock_list_updater import StockListUpdater  # type: ignore
    except ImportError:
        try:
            from scripts.stock_list_updater import StockListUpdater  # type: ignore
        except ImportError:
            return _module_error("stock_list_updater")
    try:
        result = await asyncio.to_thread(StockListUpdater().run)
        if result.get("success"):
            return f"✅ 名单更新成功：{result['count']} 只（来源: {result['source']}）"
        return "❌ 更新失败：双源均不可用（东财限流且新浪失败），请稍后重试"
    except Exception as e:
        return f"❌ 名单更新异常: {str(e)}"


# ==================== v7.1.0 定期报告分析解读 ====================

@server.tool()
async def interpret_financial_report(stock_code: str,
                                     period_limit: int = 8) -> str:
    """v7.1.0 定期报告分析解读：自动拉取上市公司财务数据并生成自然语言解读。

    覆盖业绩概览（营收/净利/同比/环比/EPS）、盈利能力（ROE/毛利率）、
    现金流质量、亮点信号、风险提示、综合评分与评级建议（纯规则引擎）。

    Args:
        stock_code: 股票代码（600519 / 000001.SZ / 832566.BJ）
        period_limit: 拉取报告期数（默认 8，用于趋势判断）
    """
    try:
        from report_interpreter import interpret_stock  # type: ignore
    except ImportError:
        try:
            from scripts.report_interpreter import interpret_stock  # type: ignore
        except ImportError:
            return _module_error("report_interpreter")
    try:
        result = await asyncio.to_thread(interpret_stock, stock_code, period_limit)
        text = result.get("text")
        if text:
            return text
        return f"❌ 解读失败：{result.get('error', '未知错误')}"
    except Exception as e:
        return f"❌ 解读异常: {str(e)}"


# ==================== v7.3.0 爬取增强 / 内容识别 / 汇总分析 ====================

@server.tool()
async def smart_crawl_batch(urls: list, concurrency: int = 4,
                            use_browser: bool = False) -> str:
    """v7.3.0 智能批量爬取：多级降级（API直连→HTTP→浏览器）+ 域名限速 +
    失败分类（软拦截/超时/空页）+ 成功率统计。

    Args:
        urls: 待爬 URL 列表（建议单次 ≤30，并发 ≤5）
        concurrency: 并发数（默认 4）
        use_browser: 是否允许浏览器兜底（需 Playwright，默认关）
    """
    try:
        from crawl_enhancer import batch_smart_crawl  # type: ignore
    except ImportError:
        try:
            from scripts.crawl_enhancer import batch_smart_crawl  # type: ignore
        except ImportError:
            return _module_error("crawl_enhancer")
    try:
        out = await asyncio.to_thread(batch_smart_crawl, list(urls),
                                      concurrency, None, None, use_browser)
        lines = []
        for fr in out["results"][:50]:
            lines.append(f"[{fr.status}] {fr.source or '-'} "
                         f"{fr.text_len}B {fr.url}")
        import json as _json
        lines.append("")
        lines.append("统计: " + _json.dumps(out["stats"], ensure_ascii=False))
        return "\n".join(lines)
    except Exception as e:
        return f"❌ 批量爬取异常: {str(e)}"


@server.tool()
async def recognize_webpage(url: str) -> str:
    """v7.3.0 网页内容识别：自动判别页面类型（列表/文章/数据页）并抽取
    结构化内容（文章条目/表格/键值数据块/分页），供后续定向爬取。

    Args:
        url: 目标页面 URL（HTTP 抓取；动态页建议先用 discover_api 挖接口）
    """
    try:
        from web_recognizer import smart_extract  # type: ignore
        from http_utils import http_get  # type: ignore
    except ImportError:
        try:
            from scripts.web_recognizer import smart_extract  # type: ignore
            from scripts.http_utils import http_get  # type: ignore
        except ImportError:
            return _module_error("web_recognizer")
    try:
        resp = await asyncio.to_thread(http_get, url)
        if resp is None:
            return f"❌ 页面抓取失败: {url}"
        import json as _json
        result = smart_extract(resp.text, url=url)
        result.pop("text", None)
        for k in ("articles", "tables", "numbers"):
            if isinstance(result.get(k), list):
                result[k] = result[k][:20]
        return _json.dumps(result, ensure_ascii=False, indent=1)[:6000]
    except Exception as e:
        return f"❌ 识别异常: {str(e)}"


@server.tool()
async def aggregate_sentiment(days: int = 0,
                              output_path: str = "") -> str:
    """v7.3.0 舆情汇总分析：聚合 data/sentiment_snapshots/ 全部快照，
    跨源模糊去重，输出情感/严重度/时间/来源分布 + 热词 + 机构截面，
    可选导出 Markdown 汇总报告。

    Args:
        days: 仅统计最近 N 天（0=全部）
        output_path: Markdown 报告落盘路径（空则仅返回摘要）
    """
    try:
        from data_aggregator import aggregate_snapshots, export_markdown  # type: ignore
    except ImportError:
        try:
            from scripts.data_aggregator import (aggregate_snapshots,  # type: ignore
                                                 export_markdown)
        except ImportError:
            return _module_error("data_aggregator")
    try:
        agg = await asyncio.to_thread(aggregate_snapshots, None, days)
        md = export_markdown(agg, output_path or None)
        if agg["raw_count"] == 0:
            return "ℹ️ 暂无舆情快照可汇总（先跑 crawl_global_sentiment 生成快照）"
        return md[:6000]
    except Exception as e:
        return f"❌ 汇总异常: {str(e)}"


# ==================== v9.0 新增：金融数据 MCP 桥接 ====================

@server.tool()
async def get_financial_data(
    query_type: str,
    code: str = "",
    symbol: str = "",
    preferred_source: str = "",
    period: str = "",
    limit: int = 0,
) -> str:
    """v9.0 统一金融数据查询：自动选择最佳数据源（akshare/东方财富API/yfinance/RSS），
    多源降级，结果标准化。覆盖A股行情/基金净值/期货/宏观经济/港股美股/Shibor等30+种查询。

    Args:
        query_type: 查询类型，可选值:
            stock_quote(股票行情), fund_nav(基金净值), fund_list(基金列表),
            fund_manager(基金经理), bond_yield(国债收益率), futures(期货),
            macro_gdp/macro_cpi/macro_ppi/macro_pmi(宏观经济),
            shibor(同业拆借), repo_rate(回购利率), forex(外汇),
            etf_flow(ETF资金流), margin_trading(融资融券),
            block_trade(大宗交易), dragon_tiger(龙虎榜),
            hk_stock(港股), us_stock(美股), convertible_bond(可转债),
            industry_board(行业板块), concept_board(概念板块),
            stock_financial(财务数据), etf_list(ETF列表),
            index_quote(指数行情), news(新闻), announcements(公告)
        code: 股票/基金代码（如 600519, 110011）
        symbol: 品种标识（如 美元兑人民币, 全部）
        preferred_source: 优先数据源（AKSHARE/EASTMONEY_API/YAHOO_FINANCE/RSS_FEEDS）
        period: 时间周期（如 1mo, 3mo, 1y）
        limit: 返回条数限制
    """
    try:
        from financial_data_mcp import query_financial_data as _query_fd  # type: ignore
    except ImportError:
        try:
            from scripts.financial_data_mcp import query_financial_data as _query_fd  # type: ignore
        except ImportError:
            return _module_error("financial_data_mcp", "pip install akshare")
    try:
        params = {}
        if code:
            params["code"] = code
        if symbol:
            params["symbol"] = symbol
        if period:
            params["period"] = period
        if limit:
            params["limit"] = limit

        result = await asyncio.to_thread(
            _query_fd, query_type,
            preferred_source=preferred_source or None,
            **params,
        )

        import json as _json
        output = {
            "success": result.success,
            "source": result.source,
            "query_type": result.query_type,
            "latency_ms": result.latency_ms,
            "fallback_used": result.fallback_used,
            "timestamp": result.timestamp,
        }
        if result.success and result.data is not None:
            # 限制输出大小
            data_str = _json.dumps(result.data, ensure_ascii=False, default=str)
            if len(data_str) > 8000:
                output["data"] = f"[数据过大，已截断] {data_str[:8000]}..."
            else:
                output["data"] = result.data
        else:
            output["error"] = result.error

        return _json.dumps(output, ensure_ascii=False, indent=1, default=str)[:10000]
    except Exception as e:
        return f"❌ 金融数据查询异常: {str(e)}"


@server.tool()
async def list_data_sources() -> str:
    """v9.0 列出所有可用金融数据源状态（名称/可用性/成功率/延迟/支持的查询类型）。

    无需参数。返回所有已注册数据源的健康状态和能力列表。
    """
    try:
        from financial_data_mcp import list_available_sources, list_query_types  # type: ignore
    except ImportError:
        try:
            from scripts.financial_data_mcp import (  # type: ignore
                list_available_sources, list_query_types,
            )
        except ImportError:
            return _module_error("financial_data_mcp", "pip install akshare")
    try:
        sources = await asyncio.to_thread(list_available_sources)
        capabilities = list_query_types()

        import json as _json
        output = {
            "sources": sources,
            "query_types": capabilities,
            "total_sources": len(sources),
            "total_query_types": len(capabilities),
        }
        return _json.dumps(output, ensure_ascii=False, indent=1)[:8000]
    except Exception as e:
        return f"❌ 查询数据源状态异常: {str(e)}"


@server.tool()
async def get_anti_block_status() -> str:
    """v9.0 获取反爬增强系统状态报告：代理池/行为画像/TLS指纹/预算控制/IP信誉/验证码检测等。

    无需参数。返回当前反爬系统的运行状态和配置信息。
    """
    try:
        from anti_block_enhanced import self_check  # type: ignore
    except ImportError:
        try:
            from scripts.anti_block_enhanced import self_check  # type: ignore
        except ImportError:
            return _module_error("anti_block_enhanced")
    try:
        import json as _json
        report = await asyncio.to_thread(self_check)
        return _json.dumps(report, ensure_ascii=False, indent=2)
    except Exception as e:
        return f"❌ 反爬状态查询异常: {str(e)}"


@server.tool()
async def discover_proxies(max_proxies: int = 20) -> str:
    """v9.0 发现免费代理并验证可用性。从公开代理池获取代理地址，
    逐一验证后返回可用代理列表（可用于配置代理池降低IP被封风险）。

    Args:
        max_proxies: 最大搜索代理数（默认20，验证耗时约1-3分钟）
    """
    try:
        from anti_block_enhanced import FreeProxyDiscovery, ProxyPool  # type: ignore
    except ImportError:
        try:
            from scripts.anti_block_enhanced import FreeProxyDiscovery, ProxyPool  # type: ignore
        except ImportError:
            return _module_error("anti_block_enhanced")
    try:
        import json as _json

        def _discover():
            discovery = FreeProxyDiscovery()
            proxies = discovery.discover(max_proxies=max_proxies)
            valid = []
            for proxy in proxies[:max_proxies]:
                ok, latency = discovery.validate_proxy(proxy)
                if ok:
                    valid.append({
                        "url": proxy,
                        "latency_ms": round(latency, 0),
                    })
            return valid

        result = await asyncio.to_thread(_discover)
        output = {
            "discovered": max_proxies,
            "validated": len(result),
            "proxies": result[:20],
            "tip": "使用 ProxyPool(proxies=[...]) 将可用代理加入代理池",
        }
        return _json.dumps(output, ensure_ascii=False, indent=1)[:6000]
    except Exception as e:
        return f"❌ 代理发现异常: {str(e)}"


# ==================== v10.0 新增：调度器 / 网页操作 / 数据源扩充 ====================

@server.tool()
async def crawl_with_dispatcher(
    url: str,
    scraper_name: str = "",
    enable_incremental: bool = True,
    enable_resume: bool = False,
) -> str:
    """v10.0 智能调度爬取单个 URL：自动按数据源健康度选源 + 增量判断 + 反爬保护。

    Args:
        url: 目标 URL
        scraper_name: 指定爬虫名（留空则按 URL 域名自动选择）
        enable_incremental: 是否启用增量（ETag/Last-Modified/hash 跳过未变更）
        enable_resume: 失败时是否入队断点续爬
    """
    try:
        from crawler_dispatcher import (  # type: ignore
            CrawlerDispatcher, DispatchConfig, register_default_scrapers,
            get_default_registry,
        )
    except ImportError:
        try:
            from scripts.crawler_dispatcher import (
                CrawlerDispatcher, DispatchConfig, register_default_scrapers,
                get_default_registry,
            )
        except ImportError:
            return _module_error("crawler_dispatcher")
    try:
        import json as _json

        def _run():
            reg = get_default_registry()
            if not reg.all_names():
                n = register_default_scrapers(reg)
            cfg = DispatchConfig(enable_incremental=enable_incremental)
            d = CrawlerDispatcher(reg, cfg)
            result = d.dispatch(url, scraper_name or None)
            return result.to_dict()

        result = await asyncio.to_thread(_run)
        if enable_resume and result.get("status") not in ("SUCCESS", "NOT_MODIFIED"):
            try:
                d = CrawlerDispatcher(get_default_registry(), DispatchConfig())
                d.queue_resume([url], scraper_name=scraper_name or "default")
                result["queued_for_retry"] = True
            except Exception:  # noqa: BLE001
                pass
        return _json.dumps(result, ensure_ascii=False, indent=1, default=str)[:4000]
    except Exception as e:
        return f"❌ 调度爬取异常: {str(e)}"


@server.tool()
async def query_crypto_markets(
    coin_id: str = "bitcoin",
    vs_currency: str = "usd",
    per_page: int = 50,
) -> str:
    """v10.0 查询数字货币行情（CoinGecko 公开 API，无需 key）。

    Args:
        coin_id: 币种 ID（bitcoin/ethereum/solana，多个用逗号分隔；留空返回市值榜）
        vs_currency: 计价货币（usd/cny/eur）
        per_page: 返回条数
    """
    try:
        from mcp_data_expansion import CoinGeckoAdapter  # type: ignore
    except ImportError:
        try:
            from scripts.mcp_data_expansion import CoinGeckoAdapter
        except ImportError:
            return _module_error("mcp_data_expansion")
    try:
        import json as _json

        def _run():
            cg = CoinGeckoAdapter()
            if coin_id:
                data = cg.query("coin_markets", coin_id=coin_id,
                                vs_currency=vs_currency, per_page=per_page)
            else:
                data = cg.query("coin_markets", coin_id="bitcoin",
                                vs_currency=vs_currency, per_page=per_page)
            return {"source": "coingecko", "coin_id": coin_id,
                    "vs_currency": vs_currency, "count": len(data) if data else 0,
                    "data": data}

        result = await asyncio.to_thread(_run)
        return _json.dumps(result, ensure_ascii=False, indent=1, default=str)[:8000]
    except Exception as e:
        return f"❌ 数字货币行情查询异常: {str(e)}"


@server.tool()
async def query_forex_realtime(
    base: str = "USD",
    target: str = "CNY",
) -> str:
    """v10.0 实时外汇汇率查询（ExchangeRate-API 公开数据，无需 key）。

    Args:
        base: 基础货币（如 USD / EUR / JPY）
        target: 目标货币（多个用逗号分隔；留空返回全部）
    """
    try:
        from mcp_data_expansion import ExchangeRateAdapter  # type: ignore
    except ImportError:
        try:
            from scripts.mcp_data_expansion import ExchangeRateAdapter
        except ImportError:
            return _module_error("mcp_data_expansion")
    try:
        import json as _json

        def _run():
            er = ExchangeRateAdapter()
            if target and target != "*":
                qt = "pair" if "," not in target else "all_rates"
                data = er.query(qt, base=base, target=target)
                if qt == "all_rates":
                    rates = data.get("rates", {}) if data else {}
                    selected = {t.strip(): rates.get(t.strip())
                                for t in target.split(",")}
                    return {"base": base, "targets": selected,
                            "timestamp": data.get("time_last_update_unix") if data else None}
                return data
            data = er.query("all_rates", base=base)
            return {"source": "exchangerate-api", "base": base,
                    "total_currencies": len(data.get("rates", {})) if data else 0,
                    "rates_preview": dict(list((data or {}).get("rates", {}).items())[:20])}

        result = await asyncio.to_thread(_run)
        return _json.dumps(result, ensure_ascii=False, indent=1, default=str)[:4000]
    except Exception as e:
        return f"❌ 外汇汇率查询异常: {str(e)}"


@server.tool()
async def tushare_query(
    api_name: str,
    ts_code: str = "",
    extra_params: str = "",
) -> str:
    """v10.0 Tushare Pro 通用查询（需 TUSHARE_TOKEN 环境变量；未设置时返回空列表+提示）。

    Args:
        api_name: Tushare API 名（如 stock_basic / daily / daily_basic / income / fund_nav）
        ts_code: 标的代码（如 600519.SH）
        extra_params: 额外 JSON 参数（如 {"limit": 100, "start_date": "20260101"}）
    """
    try:
        from mcp_data_expansion import TushareAdapter  # type: ignore
    except ImportError:
        try:
            from scripts.mcp_data_expansion import TushareAdapter
        except ImportError:
            return _module_error("mcp_data_expansion")
    try:
        import json as _json
        params: Dict[str, Any] = {}
        if ts_code:
            params["ts_code"] = ts_code
        if extra_params:
            try:
                params.update(_json.loads(extra_params))
            except Exception:  # noqa: BLE001
                pass

        def _run():
            t = TushareAdapter()
            if not t.available:
                return {"available": False,
                        "tip": "请设置 TUSHARE_TOKEN 环境变量后再调用"}
            data = t.query(api_name, **params)
            return {"available": True, "api": api_name, "params": params,
                    "count": len(data), "data": data}

        result = await asyncio.to_thread(_run)
        return _json.dumps(result, ensure_ascii=False, indent=1, default=str)[:8000]
    except Exception as e:
        return f"❌ Tushare 查询异常: {str(e)}"


@server.tool()
async def get_data_source_health() -> str:
    """v10.0 获取 v9 5 + v10 4 共 9 个数据源的实时健康状态。"""
    try:
        from mcp_data_expansion import ExpansionAdapterSet  # type: ignore
    except ImportError:
        try:
            from scripts.mcp_data_expansion import ExpansionAdapterSet
        except ImportError:
            return _module_error("mcp_data_expansion")
    try:
        import json as _json

        def _run():
            es = ExpansionAdapterSet()
            return es.all_health()

        result = await asyncio.to_thread(_run)
        # 加上 v9 数据源
        try:
            from financial_data_mcp import FinancialDataRouter  # type: ignore
            router = FinancialDataRouter()
            v9 = router.list_sources()
            result["v9_router_sources"] = v9
        except Exception:  # noqa: BLE001
            try:
                from scripts.financial_data_mcp import FinancialDataRouter
                router = FinancialDataRouter()
                v9 = router.list_sources()
                result["v9_router_sources"] = v9
            except Exception:  # noqa: BLE001
                result["v9_router_sources"] = {}
        return _json.dumps(result, ensure_ascii=False, indent=1, default=str)[:6000]
    except Exception as e:
        return f"❌ 数据源健康查询异常: {str(e)}"


@server.tool()
async def list_web_operations() -> str:
    """v10.0 列出网页操作增强工具清单（Shadow DOM/iframe/Cookie Banner/验证码/行为轨迹/WebSocket）。

    无需参数。返回各组件的能力描述。
    """
    try:
        import json as _json
        info = {
            "shadow_dom_piercer": {
                "module": "web_operations_enhanced.ShadowDOMPiercer",
                "capability": "链式 CSS selector 穿透 shadowRoot（div#app >> div.foo）",
            },
            "iframe_navigator": {
                "module": "web_operations_enhanced.IframeNavigator",
                "capability": "多层 iframe 跨域穿透 + 提取 + 点击",
            },
            "cookie_banner_handler": {
                "module": "web_operations_enhanced.CookieBannerHandler",
                "capability": "GDPR/CCPA 弹窗自动接受/拒绝（多语言 30+ 关键词）",
            },
            "captcha_pipeline": {
                "module": "web_operations_enhanced.CaptchaPipeline",
                "capability": "验证码 OCR（stdlib/ddddocr/tesseract 可选识别器）",
            },
            "human_behavior_simulator": {
                "module": "web_operations_enhanced.HumanBehaviorSimulator",
                "capability": "贝塞尔鼠标轨迹 + 阅读停顿 + 滚动节奏 + 随机键盘",
            },
            "websocket_recorder": {
                "module": "web_operations_enhanced.WebSocketRecorder",
                "capability": "捕获页面 WebSocket 推送数据（最大 1000 条滑动窗口）",
            },
        }
        return _json.dumps(info, ensure_ascii=False, indent=1)
    except Exception as e:
        return f"❌ 工具列表异常: {str(e)}"


@server.tool()
async def get_query_cache_stats() -> str:
    """v10.0 查询高频缓存统计：命中率/淘汰次数/缓存 key 列表。"""
    try:
        from mcp_data_expansion import get_default_cache  # type: ignore
    except ImportError:
        try:
            from scripts.mcp_data_expansion import get_default_cache
        except ImportError:
            return _module_error("mcp_data_expansion")
    try:
        import json as _json
        cache = get_default_cache()
        stats = cache.stats()
        stats["sample_keys"] = cache.keys()[:30]
        return _json.dumps(stats, ensure_ascii=False, indent=1)
    except Exception as e:
        return f"❌ 缓存统计查询异常: {str(e)}"


@server.tool()
async def dispatcher_queue_resume(
    urls: str,
    scraper_name: str = "default",
    auto_run: bool = False,
) -> str:
    """v10.0 把 URL 列表入队断点续爬。后续可手动调用 dispatcher_run_resume 或自动跑。

    Args:
        urls: 多个 URL 用换行或逗号分隔
        scraper_name: 爬虫名
        auto_run: 是否立即执行（True=立即，False=仅入队）
    """
    try:
        from crawler_dispatcher import (  # type: ignore
            CrawlerDispatcher, DispatchConfig,
            get_default_registry, register_default_scrapers,
        )
    except ImportError:
        try:
            from scripts.crawler_dispatcher import (
                CrawlerDispatcher, DispatchConfig,
                get_default_registry, register_default_scrapers,
            )
        except ImportError:
            return _module_error("crawler_dispatcher")
    try:
        import json as _json
        url_list = [u.strip() for u in urls.replace("\n", ",").split(",")
                    if u.strip()]
        if not url_list:
            return "❌ URL 列表为空"

        def _run():
            reg = get_default_registry()
            if not reg.all_names():
                register_default_scrapers(reg)
            d = CrawlerDispatcher(reg, DispatchConfig())
            ids = d.queue_resume(url_list, scraper_name=scraper_name)
            if auto_run:
                report = d.run_resume(scraper_name=scraper_name)
                return {"queued": len(ids), "auto_run_report": report,
                        "progress": d.resume.progress()}
            return {"queued": len(ids), "auto_run_report": None,
                    "progress": d.resume.progress()}

        result = await asyncio.to_thread(_run)
        return _json.dumps(result, ensure_ascii=False, indent=1, default=str)[:4000]
    except Exception as e:
        return f"❌ 续爬入队异常: {str(e)}"


# ==================== v11.0 新增：实时数据 / 指标引擎 / 证据融合 ====================

def _v11_realtime_hub():
    """返回 (get_hub, 错误文本)；get_hub 为 None 时调用方直接回错误。"""
    try:
        from realtime_data_hub import get_hub  # type: ignore
    except ImportError:
        try:
            from scripts.realtime_data_hub import get_hub  # type: ignore
        except ImportError:
            return None, _module_error("realtime_data_hub")
    return get_hub, ""


def _v11_indicator_engine():
    try:
        import indicator_engine as ie  # type: ignore
    except ImportError:
        try:
            from scripts import indicator_engine as ie  # type: ignore
        except ImportError:
            return None, _module_error("indicator_engine")
    return ie, ""


def _v11_fusion():
    try:
        import evidence_fusion as ef  # type: ignore
    except ImportError:
        try:
            from scripts import evidence_fusion as ef  # type: ignore
        except ImportError:
            return None, _module_error("evidence_fusion")
    return ef, ""


def _v11_enhanced():
    try:
        import analysis_enhanced as ae  # type: ignore
    except ImportError:
        try:
            from scripts import analysis_enhanced as ae  # type: ignore
        except ImportError:
            return None, _module_error("analysis_enhanced")
    return ae, ""


def _v11_shrink(node, max_items: int, max_str: int):
    """递归瘦身：列表截前 N 项、长字符串截断，用来换回可解析的 JSON。"""
    if isinstance(node, list):
        kept = [_v11_shrink(x, max_items, max_str) for x in node[:max_items]]
        if len(node) > max_items:
            kept.append({"…": f"其余 {len(node) - max_items} 项已省略"})
        return kept
    if isinstance(node, dict):
        return {k: _v11_shrink(v, max_items, max_str) for k, v in node.items()}
    if isinstance(node, str) and len(node) > max_str:
        return node[:max_str] + "…"
    return node


def _v11_json(payload, limit: int = 5000) -> str:
    """序列化工具输出。超限时必须继续是合法 JSON——直接切片会让调用方 parse 失败。"""
    text = json.dumps(payload, ensure_ascii=False, indent=1, default=str)
    if len(text) <= limit:
        return text
    text = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), default=str)
    if len(text) <= limit:
        return text
    for max_items, max_str in ((40, 400), (16, 200), (6, 120), (2, 60)):
        text = json.dumps(_v11_shrink(payload, max_items, max_str),
                          ensure_ascii=False, separators=(",", ":"), default=str)
        if len(text) <= limit:
            return text[:limit] if len(text) > limit else text
    return json.dumps({"truncated": True, "note": "输出过大已省略，请缩小 count/top 或分批查询",
                       "preview": str(payload)[:limit // 3]},
                      ensure_ascii=False, default=str)



def _v11_codes(raw: str) -> list:
    return [c.strip() for c in re.split(r"[,;，；\s\n]+", raw or "") if c.strip()]


def _v11_load_json_arg(raw: str, name: str):
    """返回 (对象, 错误文本)。既接受 JSON 文本，也接受指向 JSON 文件的绝对路径。"""
    if not (raw or "").strip():
        return None, f"❌ {name} 不能为空"
    try:
        return json.loads(raw), ""
    except ValueError:
        pass
    if len(raw) < 500 and os.path.isfile(raw):
        try:
            with open(raw, "r", encoding="utf-8") as f:
                return json.load(f), ""
        except (OSError, ValueError) as exc:
            return None, f"❌ {name} 文件读取失败: {exc}"
    return None, f"❌ {name} 既不是合法 JSON，也不是可读的 JSON 文件路径"


def _v11_watchlist_symbols(hub, name: str) -> list:
    for wl in hub.watchlists():
        if wl.get("name") == name:
            return list(wl.get("symbols") or [])
    return []


@server.tool()
async def rt_realtime_quote(codes: str, record: bool = True) -> str:
    """v11.0 实时行情快照：腾讯/新浪/东财三源逐只降级补齐，带时间戳与新鲜度。

    强制绕过 30 分钟 HTTP 缓存——这是"实时"成立的前提。attempts 字段逐源记录
    命中情况（如 ["tencent:2", "sina:EMPTY"]），哪只票是哪个源给的、哪个源空了都能查；
    missing 字段列出三源都没答上的代码（停牌行不算 missing，它带 suspended=True）。

    Args:
        codes: 逗号/空格分隔的股票、指数、基金代码或名称，如 "600519,宁德时代,上证指数"
        record: 是否写入滚动仓（供分钟聚合与指标引擎消费）
    """
    get_hub, err = _v11_realtime_hub()
    if get_hub is None:
        return err
    try:
        def _run():
            hub = get_hub()
            res = hub.quotes(_v11_codes(codes), record=record)
            return {
                "ok": res.ok, "source": res.source, "attempts": res.attempts,
                "missing": res.missing,
                "error": res.error, "count": len(res.data or []),
                "market_clock": hub.clock.describe(),
                "quotes": res.data or [],
            }
        return _v11_json(await asyncio.to_thread(_run), 6000)
    except Exception as e:
        return f"❌ 实时行情异常: {str(e)}"


@server.tool()
async def rt_realtime_kline(code: str, period: str = "day",
                            count: int = 120, adjust: str = "qfq") -> str:
    """v11.0 实时 K 线：东方财富历史/分钟线，支持复权与多周期。

    Args:
        code: 股票/指数代码或名称
        period: 周期 — day/1min/5min/15min/30min/60min/week/month
        count: 条数（1-1000）
        adjust: 复权 — qfq(前复权)/hfq(后复权)/none(不复权)
    """
    get_hub, err = _v11_realtime_hub()
    if get_hub is None:
        return err
    try:
        def _run():
            hub = get_hub()
            try:
                bars = hub.bars(code, period=period, count=count, adjust=adjust)
            except ValueError as ve:
                return {"error": str(ve)}
            return {"code": code, "period": period, "adjust": adjust,
                    "count": len(bars), "bars": bars[-120:]}
        return _v11_json(await asyncio.to_thread(_run), 8000)
    except Exception as e:
        return f"❌ K线获取异常: {str(e)}"


@server.tool()
async def rt_market_overview() -> str:
    """v11.0 市场概览：宽基指数涨跌 + 板块涨幅前 8/跌幅前 5 + 交易时段状态。

    用于回答"现在市场怎么样"这类起手问题，一次拿齐不必逐只查询。
    """
    get_hub, err = _v11_realtime_hub()
    if get_hub is None:
        return err
    try:
        return _v11_json(await asyncio.to_thread(get_hub().market_overview), 6000)
    except Exception as e:
        return f"❌ 市场概览异常: {str(e)}"


@server.tool()
async def rt_sector_board(kind: str = "industry", order: str = "desc",
                          count: int = 20) -> str:
    """v11.0 实时板块榜：行业/概念板块涨跌幅排行（东财 ulist.np）。

    Args:
        kind: industry(行业) / concept(概念)
        order: desc(涨幅榜) / asc(跌幅榜)
        count: 条数
    """
    get_hub, err = _v11_realtime_hub()
    if get_hub is None:
        return err
    try:
        def _run():
            hub = get_hub()
            rows = hub.board(kind=kind, order=order, count=count)
            return {"kind": kind, "order": order, "count": len(rows),
                    "boards": rows, "clock": hub.clock.state()}
        return _v11_json(await asyncio.to_thread(_run), 6000)
    except Exception as e:
        return f"❌ 板块数据异常: {str(e)}"


@server.tool()
async def rt_fund_valuation(fund_code: str) -> str:
    """v11.0 基金净值/估值：优先盘中估算，估算域不可用时回退披露净值。

    返回的 kind 明确区分 fund_estimate(盘中估算) 与 fund_nav(已披露净值)，
    两者不可混用——估值只是估算。
    """
    get_hub, err = _v11_realtime_hub()
    if get_hub is None:
        return err
    try:
        return _v11_json(await asyncio.to_thread(get_hub().fund_nav, fund_code), 3000)
    except Exception as e:
        return f"❌ 基金净值异常: {str(e)}"


@server.tool()
async def rt_watchlist(action: str = "list", codes: str = "",
                       name: str = "default", note: str = "") -> str:
    """v11.0 自选表管理：list / add / remove / poll。

    订阅表持久化在 data/realtime/，poll 一次即写入滚动仓，
    配合 rt_live_stream 与指标引擎可以做连续观察。

    Args:
        action: list(列出) / add(新建或覆盖订阅) / remove(删除) / poll(取一次快照)
        codes: 逗号分隔代码，add/poll 时使用
        name: 订阅表名称
        note: 备注（add 时记录为什么订阅这组）
    """
    get_hub, err = _v11_realtime_hub()
    if get_hub is None:
        return err
    try:
        def _run():
            hub = get_hub()
            act = (action or "list").lower()
            if act == "add":
                target = _v11_codes(codes)
                if not target:
                    return {"error": "add 需要至少一个代码"}
                return {"added": hub.subscribe(target, name=name, note=note).to_dict()}
            if act == "remove":
                return {"removed": hub.unsubscribe(name), "watchlists": hub.watchlists()}
            if act == "poll":
                target = _v11_codes(codes) or _v11_watchlist_symbols(hub, name)
                return hub.poll(target)
            return {"watchlists": hub.watchlists(), "status": hub.status()}
        return _v11_json(await asyncio.to_thread(_run), 6000)
    except Exception as e:
        return f"❌ 自选表操作异常: {str(e)}"


@server.tool()
async def rt_live_stream(codes: str, seconds: float = 30,
                         interval: float = 5) -> str:
    """v11.0 有界实时轮询：在 seconds 秒内按 interval 间隔采样，返回帧与分钟条。

    MCP 只能同步返回，所以"流"实现为"有限时间内采若干帧"，
    帧数受 MAX_STREAM_SECONDS=120 / MAX_STREAM_POINTS=400 约束，绝不死循环。

    Args:
        codes: 逗号分隔代码（必填）
        seconds: 采样总时长（秒，上限 120）
        interval: 采样间隔（秒）
    """
    get_hub, err = _v11_realtime_hub()
    if get_hub is None:
        return err
    target = _v11_codes(codes)
    if not target:
        return "❌ codes 不能为空"
    try:
        res = await asyncio.to_thread(
            get_hub().stream, target, seconds, interval)
        polls = res.pop("polls", [])
        res["poll_brief"] = [{"at": p.get("at"), "count": p.get("count")}
                             for p in polls][-20:]
        return _v11_json(res, 8000)
    except Exception as e:
        return f"❌ 实时流异常: {str(e)}"


@server.tool()
async def rt_technical_snapshot(code: str, period: str = "day", count: int = 120,
                                benchmark_code: str = "") -> str:
    """v11.0 实时行情 + 技术诊断一步到位：取 K 线后直接跑指标、信号、异常与基准对比。

    这是"实时获取数据并加以分析"的主入口。输出刻意只给事实和可核验数字，
    结论性文案留给上层模型，这样每句判断都能回溯到具体指标值。

    Args:
        code: 股票/指数代码或名称
        period: K 线周期 — day/1min/5min/15min/30min/60min/week/month
        count: K 线条数（建议 >=60，否则部分指标因样本不足为空）
        benchmark_code: 基准代码（如 000300 沪深300），提供时输出 beta/相关性/超额收益
    """
    get_hub, err = _v11_realtime_hub()
    if get_hub is None:
        return err
    ie, err2 = _v11_indicator_engine()
    if ie is None:
        return err2
    try:
        def _run():
            hub = get_hub()
            bars = hub.bars(code, period=period, count=count)
            if not bars:
                return {"error": f"{code} 未取到 K 线，无法做技术诊断"}
            bench = None
            if benchmark_code:
                bench = hub.bars(benchmark_code, period=period, count=count) or None
            out = ie.analyze_bars(bars, benchmark=bench)
            out["subject"] = {"code": code, "period": period, "bars": len(bars),
                              "last_bar": bars[-1]}
            latest = hub.quote_one(code)
            if latest:
                out["live_quote"] = {k: latest.get(k) for k in
                                     ("name", "price", "change_pct", "asof",
                                      "freshness_seconds", "source", "suspended")}
            if bench:
                out["benchmark_code"] = benchmark_code
            return out
        return _v11_json(await asyncio.to_thread(_run), 8000)
    except Exception as e:
        return f"❌ 技术快照异常: {str(e)}"


@server.tool()
async def indicator_catalog() -> str:
    """v11.0 指标清单：全部可用技术指标的名称、中文名、所需字段、参数与最小样本量。"""
    ie, err = _v11_indicator_engine()
    if ie is None:
        return err
    try:
        rows = await asyncio.to_thread(ie.available_indicators)
        return _v11_json({"count": len(rows), "indicators": rows}, 7000)
    except Exception as e:
        return f"❌ 指标清单异常: {str(e)}"


@server.tool()
async def indicator_compute(name: str, series_json: str,
                            params_json: str = "{}", last_only: bool = True) -> str:
    """v11.0 单指标计算：按名计算任意已注册指标，输入支持收盘价/K线/dict 列表。

    Args:
        name: 指标名，如 sma / ema / macd / rsi / kdj / bollinger / atr / adx / obv /
            mfi / sharpe_ratio / sortino_ratio / max_drawdown / rolling_beta / spearman
        series_json: JSON 数组。可为数字列表 [1,2,3]、K线对象列表、或 JSON 文件绝对路径
        params_json: 参数字典 JSON，如 '{"period": 20}'
        last_only: 只取最新值（多线指标返回各分量的最新值）
    """
    ie, err = _v11_indicator_engine()
    if ie is None:
        return err
    series, err3 = _v11_load_json_arg(series_json, "series_json")
    if series is None:
        return err3
    try:
        params = json.loads(params_json or "{}")
        if not isinstance(params, dict):
            return "❌ params_json 必须是 JSON 对象"
    except ValueError:
        return "❌ params_json 不是合法 JSON 对象"
    try:
        def _run():
            fn = ie.compute_last if last_only else ie.compute
            return {"name": name, "params": params, "points": len(series or []),
                    "value": fn(name, series, **params)}
        return _v11_json(await asyncio.to_thread(_run), 4000)
    except ie.InsufficientData as e:
        return f"⚠ 样本不足：{e}"
    except Exception as e:
        return f"❌ 指标计算失败: {str(e)}"


@server.tool()
async def series_diagnose(bars_json: str, benchmark_json: str = "",
                          anomaly_threshold: float = 3.0) -> str:
    """v11.0 离线序列技术诊断：指标 + 信号 + 综合分 + 异常点，可选基准对比。

    与 rt_technical_snapshot 的区别：这里吃的是你手上已有的数据
    （爬到的净值序列、Excel 导出的收盘价），不联网。

    Args:
        bars_json: JSON 数组 — 数字列表 / 含 close 的 dict 列表 / JSON 文件绝对路径
        benchmark_json: 基准序列，同上（可选）
        anomaly_threshold: 异常判定阈值（MAD 修正 z）
    """
    ie, err = _v11_indicator_engine()
    if ie is None:
        return err
    bars, err3 = _v11_load_json_arg(bars_json, "bars_json")
    if bars is None:
        return err3
    bench = None
    if (benchmark_json or "").strip():
        bench, err4 = _v11_load_json_arg(benchmark_json, "benchmark_json")
        if bench is None:
            return err4
    try:
        out = await asyncio.to_thread(
            ie.analyze_bars, bars, bench, anomaly_threshold)
        return _v11_json(out, 8000)
    except Exception as e:
        return f"❌ 序列诊断异常: {str(e)}"


@server.tool()
async def series_anomalies(values_json: str, method: str = "robust",
                           threshold: float = 3.0, tail: int = 0) -> str:
    """v11.0 时序异常检测：zscore / iqr / ewma / robust / vote 五法可选。

    ewma 走 RiskMetrics 波动率锥，对"波动聚集"更合适；
    vote 需四种方法中至少两票才报，适合只想看硬异常的场景。

    Args:
        values_json: JSON 数字数组，或 JSON 文件绝对路径
        method: zscore / iqr / ewma / robust(默认) / vote
        threshold: 阈值（robust 为修正 z 分数，vote 为票数门槛）
        tail: 只检查最近 N 个点（0 = 全部）
    """
    ie, err = _v11_indicator_engine()
    if ie is None:
        return err
    vals, err3 = _v11_load_json_arg(values_json, "values_json")
    if vals is None:
        return err3
    if not isinstance(vals, list):
        return "❌ values_json 应为数组"
    try:
        flags = await asyncio.to_thread(
            ie.detect_anomalies, vals, method, threshold, tail)
        return _v11_json({"method": method, "threshold": threshold,
                          "points": len(vals), "flags": [f.to_dict() for f in flags]},
                         6000)
    except Exception as e:
        return f"❌ 异常检测失败: {str(e)}"


@server.tool()
async def fuse_multi_source(records_json: str, top: int = 20, max_items: int = 5,
                            style: str = "brief", persist_ledger: bool = False,
                            persist_history: bool = False) -> str:
    """v11.0 多源证据融合汇总：把异构记录聚成事件簇，产出"有出处、有分歧、有缺口"的摘要。

    做的事：字段别名归一 → 内容哈希去重 → 多键分块 + 并查集聚类 →
    按"权威度 × 时间衰减"加权中位数取共识值 → 量化跨源分歧 →
    每个结论附可回溯引用与明确缺口。单一来源的置信度会被封顶。

    Args:
        records_json: JSON 数组，元素含 title/content/source/url/published_at 任一组合；
            也接受 JSON 文件绝对路径
        top: 返回证据包数量上限
        max_items: 摘要条数
        style: brief(结论级) / full(含证据与异常)
        persist_ledger: 是否把分歧写入冲突台账（默认不写，只读调用不该悄悄落盘）
        persist_history: 是否把内容哈希写入历史索引（用于跨批次识别"这条是不是新出现"）
    """
    ef, err = _v11_fusion()
    if ef is None:
        return err
    records, err3 = _v11_load_json_arg(records_json, "records_json")
    if records is None:
        return err3
    if isinstance(records, dict):
        records = records.get("records") or records.get("items") or records.get("articles") or [records]
    if not isinstance(records, list) or not records:
        return "❌ records_json 应为非空数组"
    try:
        def _run():
            hub_kwargs = {"use_ledger": persist_ledger, "use_history": persist_history,
                          "top": top}
            if persist_ledger or persist_history:
                hub_kwargs["ledger"] = ef.ConflictLedger() if persist_ledger else None
                hub_kwargs["history"] = ef.HistoryIndex() if persist_history else None
            res = ef.fusion_aggregate(records, **hub_kwargs)
            return {"digest": ef.render_digest(res, max_items=max_items, style=style),
                    "stats": {k: v for k, v in res.items() if k != "top_evidence"},
                    "top_evidence": res["top_evidence"][:3]}
        return _v11_json(await asyncio.to_thread(_run), 9000)
    except Exception as e:
        return f"❌ 证据融合异常: {str(e)}"


@server.tool()
async def fuse_evidence_report(records_json: str, index: int = 0) -> str:
    """v11.0 单事件证据报告（Markdown）：结论 + 数值共识 + 分歧 + 缺口 + 编号引用。

    Args:
        records_json: 同 fuse_multi_source，JSON 数组或 JSON 文件绝对路径
        index: 取置信度排序后的第几个证据包（0 = 最可信的那个）
    """
    ef, err = _v11_fusion()
    if ef is None:
        return err
    records, err3 = _v11_load_json_arg(records_json, "records_json")
    if records is None:
        return err3
    if isinstance(records, dict):
        records = records.get("records") or records.get("items") or records.get("articles") or [records]
    try:
        def _run():
            res = ef.fusion_aggregate(records, top=max(index + 1, 5))
            bundles = res.get("top_evidence") or []
            if not bundles:
                return "ℹ️ 这批记录聚不出事件簇（可能字段缺失或记录过少）"
            i = max(0, min(index, len(bundles) - 1))
            head = (f"> 共 {res['input_records']} 条记录、{res['clusters']} 个事件簇、"
                    f"{res['unique_sources']} 个独立来源；展示第 {i + 1} 个证据包。\n\n")
            return head + ef.render_evidence_markdown(bundles[i])
        return (await asyncio.to_thread(_run))[:9000]
    except Exception as e:
        return f"❌ 证据报告异常: {str(e)}"


@server.tool()
async def conflict_ledger(action: str = "list", key: str = "",
                          resolution: str = "", field: str = "") -> str:
    """v11.0 冲突台账：查/结/统计跨源数值分歧，让"谁和谁数字打架"可复盘。

    Args:
        action: list(未决冲突) / resolve(标记已核实) / stats(统计) / clear(清空)
        key: resolve 时的冲突键（来自 list 结果，如 "c1f3...#amount"）
        resolution: resolve 时的结论说明，如 "以监管公示为准"
        field: list 时按字段过滤，如 amount / pct
    """
    ef, err = _v11_fusion()
    if ef is None:
        return err
    try:
        def _run():
            led = ef.ConflictLedger()
            act = (action or "list").lower()
            if act == "resolve":
                if not key:
                    return {"error": "resolve 需要 key"}
                return {"resolved": led.resolve(key, resolution or "已人工核实"),
                        "stats": led.stats()}
            if act == "stats":
                return led.stats()
            if act == "clear":
                led.clear()
                return {"cleared": True, "path": str(led.path)}
            return {"count": len(led.open_conflicts(field)),
                    "conflicts": led.open_conflicts(field), "path": str(led.path)}
        return _v11_json(await asyncio.to_thread(_run), 7000)
    except Exception as e:
        return f"❌ 冲突台账异常: {str(e)}"


@server.tool()
async def news_deep_analysis(days: int = 0, limit: int = 300,
                             min_sources: int = 2) -> str:
    """v11.0 舆情深度分析：在快照聚合之上加交叉验证、趋势、关键词与新词、洞察。

    数据来自 data/sentiment_snapshots/（先跑 crawl_global_sentiment 或
    aggregate_sentiment 生成快照）。输出为 JSON：统计 + 已验证事件（含置信度）+
    趋势与异常 + 关键词/新词 + N 条可读洞察。

    Args:
        days: 仅分析最近 N 天（0 = 全部）
        limit: 参与分析的条目上限
        min_sources: 判定"已验证"所需的最少独立来源数
    """
    ae, err = _v11_enhanced()
    if ae is None:
        return err
    try:
        from data_aggregator import aggregate_snapshots  # type: ignore
    except ImportError:
        try:
            from scripts.data_aggregator import aggregate_snapshots  # type: ignore
        except ImportError:
            return _module_error("data_aggregator")
    try:
        def _run():
            agg = aggregate_snapshots(None, days)
            articles = (agg.get("articles") or [])[:max(1, limit)]
            if not articles:
                return {"note": "暂无舆情条目可分析，先跑 crawl_global_sentiment 生成快照",
                        "snapshot_count": agg.get("snapshot_count", 0)}
            out = ae.enhanced_aggregate(articles, days=days)
            out["verified_min_sources"] = min_sources
            return out
        return _v11_json(await asyncio.to_thread(_run), 9000)
    except Exception as e:
        return f"❌ 深度分析异常: {str(e)}"


@server.tool()
async def peer_benchmark(target_name: str, metrics_json: str,
                         group_key: str = "", group_filter_json: str = "{}") -> str:
    """v11.0 同业对标：目标在各指标上的组内百分位、偏离度与排名。

    Args:
        target_name: 目标名称，必须出现在 metrics_json 的键里
        metrics_json: {名称: {指标名: 数值}} 的 JSON，或 JSON 文件绝对路径
        group_key: 分组字段名（该字段与其余同值对象构成同类）
        group_filter_json: {组名: [成员名]} 的 JSON，用于手工指定同类集合
    """
    ae, err = _v11_enhanced()
    if ae is None:
        return err
    metrics, err3 = _v11_load_json_arg(metrics_json, "metrics_json")
    if metrics is None:
        return err3
    if not isinstance(metrics, dict):
        return "❌ metrics_json 应为 {名称: {指标: 数值}} 对象"
    try:
        group_filter = json.loads(group_filter_json or "{}")
        if not isinstance(group_filter, dict):
            return "❌ group_filter_json 应为 JSON 对象"
    except ValueError:
        return "❌ group_filter_json 不是合法 JSON 对象"
    try:
        import dataclasses
        res = await asyncio.to_thread(
            ae.benchmark_against_peers, target_name, metrics, group_key, group_filter)
        return _v11_json(dataclasses.asdict(res), 6000)
    except Exception as e:
        return f"❌ 同业对标异常: {str(e)}"


# ==================== v12.0 新增：信息获取编排 / 交付包自证 / 登录态 ====================

# 序列化与超限瘦身沿用 v11 的实现，不再重复一份。
_v12_json = _v11_json


def _v12_pipeline():
    try:
        import pipeline_runner as pr  # type: ignore
    except ImportError:
        try:
            from scripts import pipeline_runner as pr  # type: ignore
        except ImportError:
            return None, _module_error("pipeline_runner")
    return pr, ""


def _v12_login():
    try:
        import login_session as ls  # type: ignore
    except ImportError:
        try:
            from scripts import login_session as ls  # type: ignore
        except ImportError:
            return None, _module_error("login_session")
    return ls, ""


def _v12_packager():
    try:
        import crawl_packager as cp  # type: ignore
    except ImportError:
        try:
            from scripts import crawl_packager as cp  # type: ignore
        except ImportError:
            return None, _module_error("crawl_packager")
    return cp, ""


def _v12_quiet(func, *args, **kwargs):
    """把被调模块内部的 print 挪到 stderr——stdio 传输下 stdout 只能有协议帧。

    打包/下载这些模块本来是给 CLI 用的，成功路径直接 print；在 MCP 里不管的话
    这些行会插进 JSON-RPC 输出流，把客户端解析打断。
    """
    old = sys.stdout
    sys.stdout = sys.stderr
    try:
        return func(*args, **kwargs)
    finally:
        sys.stdout = old


def _v12_error(exc) -> str:
    """LoginError / PipelineError 自带 code+cause/reason+next_step，原样回传。

    这两个模块的错误契约就是"告诉用户下一步做什么"，压成一行 ❌ 文本会把
    已经算好的处置建议丢掉。
    """
    to_dict = getattr(exc, "to_dict", None)
    if callable(to_dict):
        try:
            return _v12_json({"ok": False, **to_dict()}, 3000)
        except Exception:
            pass
    return f"❌ {type(exc).__name__}: {exc}"


def _v12_list(raw: str) -> list:
    """按逗号/分号/换行切分路径或 URL 清单。

    不复用 _v11_codes：那个还按空格切，而 Windows 路径与 "url|文件名" 里都有空格。
    """
    return [p.strip() for p in re.split(r"[,;\n]+", raw or "") if p.strip()]


def _v12_markers(raw: str) -> list:
    return _v12_list(raw)


@server.tool()
async def list_info_intents(keyword: str = "", intent: str = "") -> str:
    """v12.0 信息获取意图目录：每类能问什么、要什么输入、给什么输出、做不了什么。

    解决"触发词几十上百个，先试哪个"的问题：给 keyword 就直接告诉你按现有词表
    会路由到哪一类、置信度多少、还有哪些备选；不给 keyword 则返回全部意图供挑选。
    每类都带 3 个真实问法（ask）、典型耗时和 not_for（明确做不了的场景）。

    Args:
        keyword: 一句自然语言问法，如 "贵州茅台最近三天负面舆情"（可选）
        intent: 只看某一类的完整契约，如 sentiment / announcement / login_page（可选）
    """
    pr, err = _v12_pipeline()
    if pr is None:
        return err
    try:
        def _run():
            if intent:
                return {"mode": "detail", "intent": pr.describe(intent)}
            out = {"mode": "catalog",
                   "intents": pr.list_intents(),
                   "how_to_pick": "先用 ask 里的例句对齐你的问法；"
                                  "拿不准就把整句话填到 keyword 让路由判断"}
            if keyword:
                out["route_for_keyword"] = pr.route(keyword)
            return out
        return _v12_json(await asyncio.to_thread(_run), 8000)
    except Exception as exc:
        return _v12_error(exc)


@server.tool()
async def gather_info(topic: str = "", intent: str = "auto", days: int = 7,
                      limit: int = 60, profile: str = "", analyze: bool = True,
                      fuse: bool = True, package: bool = False, out_dir: str = "",
                      dry_run: bool = False, records_json: str = "") -> str:
    """v12.0 信息获取一键编排：路由 → 抓取 → 去占位 → 质量分 → 分析 → 证据融合 → 交付。

    一次调用跑完整条链路，不必人工衔接。返回里 plan 逐步标真实执行状态
    （done/planned/skipped/failed），quality 给 A–D 分档，answer_ability 明说这份数据
    "能答什么 / 不能答什么 / 置信度"。反爬产生的 "[未能直接抓取]" 占位条目会被剔除并
    计入 meta，不会把"没抓到"粉饰成"抓到了 15 条"。

    Args:
        topic: 自然语言主题 / 机构名 / URL / 代码，按 intent 解释
        intent: auto，或 sentiment/web/search_crawl/institution/market/announcement/document/login_page
        days: 时间窗（天），过期数据会被时间过滤掉
        limit: 记录条数上限
        profile: 登录档案名（intent=login_page 时使用）
        analyze: 是否跑增强聚合（关键词/趋势/跨源/洞察）
        fuse: 是否跑多源证据融合（共识值 + 冲突台账）
        package: 是否连同 markdown/json 打成带 sha256 清单的 zip 交付包
        out_dir: 产物落盘目录（绝对路径）；留空则只在内存返回，不写盘
        dry_run: 只出计划与契约，不发任何请求（先确认源与时间窗再实跑）
        records_json: 直接喂现成记录（JSON 数组或其文件绝对路径），跳过抓取环节
    """
    pr, err = _v12_pipeline()
    if pr is None:
        return err
    records = None
    if (records_json or "").strip():
        records, err2 = _v11_load_json_arg(records_json, "records_json")
        if records is None:
            return err2
        if not isinstance(records, list):
            return "❌ records_json 应为记录对象组成的 JSON 数组"
    try:
        def _run():
            return _v12_quiet(pr.gather_info, topic=topic, intent=intent,
                              records=records, days=days, limit=limit,
                              profile=profile, analyze=analyze, fuse=fuse,
                              package=package, out_dir=out_dir, dry_run=dry_run)
        result = await asyncio.to_thread(_run)
        if not result.get("ok", True):
            log.warning(f"gather_info not ok: intent={result.get('intent')} "
                        f"errors={[e.get('code') for e in result.get('errors') or []]}")
        return _v12_json(result, 9000)
    except Exception as exc:
        return _v12_error(exc)


@server.tool()
async def render_info_receipt(result_json: str) -> str:
    """v12.0 把 gather_info 的结果渲染成 Markdown 回执，直接贴给用户看。

    回执含：标题与路由结论、共识事件与来源、洞察、质量分档、采集执行轨迹、
    缺口与"下一步该做什么"。这是"看不到爬取结果长什么样"的补齐环节。

    Args:
        result_json: gather_info 返回的 JSON 文本，或该 JSON 的文件绝对路径；
                     也可以直接传记录数组（会就地跑一遍编排再渲染）
    """
    pr, err = _v12_pipeline()
    if pr is None:
        return err
    data, err2 = _v11_load_json_arg(result_json, "result_json")
    if data is None:
        return err2
    try:
        def _run():
            if isinstance(data, list):
                return pr.render_pipeline_markdown(pr.gather_info(records=data))
            if not isinstance(data, dict):
                return None
            return pr.render_pipeline_markdown(data)
        md = await asyncio.to_thread(_run)
        if md is None:
            return "❌ result_json 应为 gather_info 的返回对象或记录数组"
        return md
    except Exception as exc:
        return _v12_error(exc)


@server.tool()
async def package_delivery(files: str, zip_name: str = "", output_dir: str = "",
                           manifest_json: str = "", failed_json: str = "") -> str:
    """v12.0 生成交付 ZIP：数据文件 + manifest.json（逐条 sha256/字节数）+ README.md。

    与 package_files 的区别是这份包能自证——接收方跑 verify_package 就能判断包有没有
    被截断、替换或漏文件。不存在的文件跳过并写进 manifest.skipped。

    Args:
        files: 待打包文件的绝对路径，逗号/分号/换行分隔（路径内空格保留）
        zip_name: ZIP 名（不含扩展名），留空按时间戳生成
        output_dir: 输出目录（绝对路径）；留空用 data/packages
        manifest_json: 业务摘要 JSON（主题/条数/质量分等），原样写入 manifest
        failed_json: 上游失败清单 JSON 数组（[{url, reason}]），供重下排查
    """
    cp, err = _v12_packager()
    if cp is None:
        return err
    paths = _v12_list(files)
    if not paths:
        return "❌ files 为空，没有可打包的文件"
    try:
        payload = json.loads(manifest_json) if manifest_json.strip() else None
        failed = json.loads(failed_json) if failed_json.strip() else None
    except ValueError as exc:
        return f"❌ manifest_json / failed_json 不是合法 JSON: {exc}"
    try:
        def _run():
            return _v12_quiet(cp.build_delivery_package, paths, zip_name=zip_name,
                              output_dir=output_dir, manifest_payload=payload,
                              failed=failed)
        out = await asyncio.to_thread(_run)
        out["verify_next_step"] = "把 zip 交给接收方时用 verify_package(zip_path) 复验"
        return _v12_json(out, 4000)
    except Exception as exc:
        return _v12_error(exc)


@server.tool()
async def verify_package(zip_path: str) -> str:
    """v12.0 校验交付 ZIP：逐条比对 manifest 里的 sha256 与字节数。

    返回 ok / checked / mismatched（内容被改）/ missing（清单有而包里没有）/
    extra（包里有而清单没有），用于确认下载或传输过程中文件没有损坏。

    Args:
        zip_path: 交付 ZIP 的绝对路径（package_delivery 或 package_remote_files 的输出）
    """
    cp, err = _v12_packager()
    if cp is None:
        return err
    if not (zip_path or "").strip():
        return "❌ zip_path 不能为空"
    try:
        report = await asyncio.to_thread(cp.verify_delivery_package, zip_path)
        if not report.get("ok"):
            return _v12_json({"ok": False, **report,
                              "next_step": "mismatched/missing 用 package_remote_files "
                                           "重下失败项；E_ZIP_MISSING 先确认路径是绝对路径"},
                             4000)
        return _v12_json(report, 4000)
    except Exception as exc:
        return _v12_error(exc)


@server.tool()
async def package_remote_files(urls: str, zip_name: str = "",
                               output_dir: str = "", retries: int = 2,
                               timeout: int = 20) -> str:
    """v12.0 URL 清单 → 逐条下载（失败自动重下）→ 带 sha256 清单的交付包。

    单条失败不中断整批；最终失败项进 manifest.failed（含原因与重试次数），
    下一轮只重下这几条。用于"这批公告 PDF 全给我抓下来打包"。

    Args:
        urls: 链接清单，逗号/分号/换行分隔；元素可写 "url|文件名" 指定落盘名
        zip_name: 交付包名（不含扩展名）
        output_dir: 输出目录（绝对路径）；留空用 data/packages
        retries: 单条失败重试次数（退避 0.5s/1.0s）
        timeout: 单条下载超时（秒）
    """
    cp, err = _v12_packager()
    if cp is None:
        return err
    items = _v12_list(urls)
    if not items:
        return "❌ urls 为空，没有可下载的链接"
    bad = [u for u in items if not re.match(r"^https?://", u, re.I)]
    if bad:
        return f"❌ 这些条目不是 http(s) 链接: {bad[:5]}"
    try:
        def _run():
            return _v12_quiet(cp.package_remote_files, items, zip_name=zip_name,
                              output_dir=output_dir, retries=retries, timeout=timeout)
        out = await asyncio.to_thread(_run)
        out["note"] = ("downloaded < requested 时看 failed 清单，只重下失败项即可；"
                       "verify_package(zip_path) 可复验完整性")
        return _v12_json(out, 5000)
    except Exception as exc:
        return _v12_error(exc)


@server.tool()
async def add_login_profile(name: str, domain: str, cookie_file: str = "",
                            cookie_text: str = "", probe_url: str = "",
                            notes: str = "", success_markers: str = "",
                            failure_markers: str = "") -> str:
    """v12.0 登记站点登录档案：将用户自己的 Cookie 存成本地档案，供登录态抓取复用。

    Cookie 来源四种格式自动识别（storage_state JSON / JSON 数组 / Netscape
    cookies.txt / 请求头串）。**任何返回都不含 Cookie 值**，只给脱敏摘要与健康度。
    Cookie 由用户自己从浏览器导出，本工具不托管账号密码、不破解验证码。

    Args:
        name: 档案名（字母数字与 ._-.，如 eastmoney / xueqiu-trial）
        domain: 站点主域，如 quote.eastmoney.com（带 scheme 也能用）
        cookie_file: Cookie 文件绝对路径（推荐，支持 ~）
        cookie_text: Cookie 文本（内容会原样解析，但不会回显）
        probe_url: 探测登录态用的页面，留空则用 https://<domain>/
        notes: 备注，如 "扫码登录，7 天有效"
        success_markers: 判定"仍登录"的正文关键词，逗号分隔，留空用内置词表
        failure_markers: 判定"已掉登录"的正文关键词，逗号分隔，留空用内置词表
    """
    ls, err = _v12_login()
    if ls is None:
        return err
    if not (name or "").strip() or not (domain or "").strip():
        return "❌ name 与 domain 都必填"
    if not (cookie_file.strip() or cookie_text.strip()):
        return ("❌ 至少给一个 Cookie 来源：cookie_file（推荐）或 cookie_text；"
                "不传则不会创建空档案")
    try:
        def _run():
            return ls.add_profile(name=name, domain=domain,
                                  cookie_file=cookie_file, cookie_text=cookie_text,
                                  probe_url=probe_url, notes=notes,
                                  success_markers=_v12_markers(success_markers),
                                  failure_markers=_v12_markers(failure_markers))
        out = await asyncio.to_thread(_run)
        return _v12_json(out, 4000)
    except Exception as exc:
        return _v12_error(exc)


@server.tool()
async def list_login_profiles(domain: str = "") -> str:
    """v12.0 列出登录档案（脱敏摘要 + 健康度分布）。

    只出名字/域名/Cookie 条数/最早过期时间/状态（fresh、expiring、expired、
    session_only），不出任何 Cookie 名对应的值。用于回答"我登着的那些站还能用吗"。

    Args:
        domain: 按站点后缀过滤，如 eastmoney.com；留空列全部
    """
    ls, err = _v12_login()
    if ls is None:
        return err
    try:
        def _run():
            profiles = ls.list_profiles(domain)
            report = ls.login_status_report()
            return {"domain_filter": domain, "total": len(profiles),
                    "profiles": profiles,
                    "by_status": report["by_status"],
                    "summary": report["summary"],
                    "next_step": report["next_step"]}
        return _v12_json(await asyncio.to_thread(_run), 6000)
    except Exception as exc:
        return _v12_error(exc)


@server.tool()
async def update_login_profile(name: str, probe_url: str = "", notes: str = "",
                               success_markers: str = "", failure_markers: str = "",
                               domain: str = "") -> str:
    """v12.0 改登录档案的元信息（探测页/备注/判定词/域名）。

    不接受直接改 Cookie 值——换 Cookie 请重新 add_login_profile，避免把凭据
    在多个工具之间传来传去。

    Args:
        name: 档案名
        probe_url: 新的探测页面
        notes: 新备注
        success_markers: 判定"仍登录"的关键词，逗号分隔
        failure_markers: 判定"已掉登录"的关键词，逗号分隔
        domain: 新的站点主域
    """
    ls, err = _v12_login()
    if ls is None:
        return err
    fields: dict = {}
    if probe_url.strip():
        fields["probe_url"] = probe_url.strip()
    if notes.strip():
        fields["notes"] = notes
    if success_markers.strip():
        fields["success_markers"] = _v12_markers(success_markers)
    if failure_markers.strip():
        fields["failure_markers"] = _v12_markers(failure_markers)
    if domain.strip():
        fields["domain"] = domain.strip()
    if not fields:
        return "❌ 没有要改的字段（probe_url/notes/success_markers/failure_markers/domain）"
    try:
        out = await asyncio.to_thread(lambda: ls.update_profile(name, **fields))
        return _v12_json(out, 4000)
    except Exception as exc:
        return _v12_error(exc)


@server.tool()
async def remove_login_profile(name: str) -> str:
    """v12.0 删除登录档案（连同落盘的 Cookie 文件）。

    Args:
        name: 档案名；先跑 list_login_profiles 确认
    """
    ls, err = _v12_login()
    if ls is None:
        return err
    if not (name or "").strip():
        return "❌ name 不能为空"
    try:
        return _v12_json(await asyncio.to_thread(ls.remove_profile, name), 2000)
    except Exception as exc:
        return _v12_error(exc)


@server.tool()
async def check_login_status(name: str = "", url: str = "", timeout: int = 15) -> str:
    """v12.0 探测"现在到底还登着吗"，给结论 + 证据 + 下一步，不猜。

    判定顺序：档案健康度（Cookie 已过期则直接结论，不发请求）→ 带 Cookie 抓探测页
    → 命中登录标记词判已登录 → 只命中登录墙判未登录 → 网络失败或词表都没命中判
    unknown 并附 HTTP 诊断。unknown 是诚实结论，不会硬说成功。

    Args:
        name: 登录档案名（推荐）
        url: 临时探测的页面 URL（不给则用档案 probe_url）
        timeout: 探测超时（秒）
    """
    ls, err = _v12_login()
    if ls is None:
        return err
    if not (name or "").strip() and not (url or "").strip():
        return "❌ name 与 url 至少给一个，否则无从探测"
    try:
        out = await asyncio.to_thread(lambda: ls.check_login(name=name, url=url,
                                                             timeout=timeout))
        return _v12_json(out, 5000)
    except Exception as exc:
        return _v12_error(exc)


@server.tool()
async def export_login_storage_state(name: str, out_path: str = "") -> str:
    """v12.0 把登录档案导出为 Playwright storage_state 文件，供浏览器抓取复用。

    返回路径与条数，**不回显内容**。导出的是真实会话凭据：只应落到包外目录，
    不要提交进仓库、不要转发给他人。

    Args:
        name: 登录档案名
        out_path: 导出目标绝对路径（强烈建议指定到技能包外）；留空落在档案目录内
    """
    ls, err = _v12_login()
    if ls is None:
        return err
    if not (name or "").strip():
        return "❌ name 不能为空"
    try:
        def _run():
            state = ls.storage_state(name)
            path = ls.write_storage_state(name, out_path)
            return {"ok": True, "path": path,
                    "cookie_count": len(state.get("cookies") or []),
                    "origins": len(state.get("origins") or []),
                    "warning": "文件含真实会话凭据：放包外、勿提交、勿转发",
                    "next_step": "browser_* 类工具用 storage_state 路径加载登录态"}
        return _v12_json(await asyncio.to_thread(_run), 2500)
    except Exception as exc:
        return _v12_error(exc)


@server.tool()
async def wait_for_login(name: str, login_url: str, target_domain: str = "",
                         timeout: int = 180, headless: bool = False) -> str:
    """v12.0 扫码登录接管：起一个可见浏览器等你登录，成功后自动把会话存成档案。

    这是唯一的人工登录通道——脚本不输密码、不破解验证码，只等你在浏览器里完成
    登录，然后检测到已登录并把 Cookie 收成档案。需要 Playwright（未安装时返回
    安装指引而不是崩掉）。

    Args:
        name: 要创建/覆盖的档案名，如 xiaohongshu
        login_url: 登录入口页，如 https://www.xiaohongshu.com/explore
        target_domain: 目标主域；留空从 login_url 推导
        timeout: 最多等你多少秒（默认 180）
        headless: 是否无头（默认 False，必须可见你才能扫码）
    """
    ls, err = _v12_login()
    if ls is None:
        return err
    if not (name or "").strip() or not (login_url or "").strip():
        return "❌ name 与 login_url 都必填"
    try:
        def _run():
            return _v12_quiet(ls.wait_for_manual_login, name, login_url,
                              target_domain=target_domain, timeout=timeout,
                              headless=headless)
        out = await asyncio.to_thread(_run)
        return _v12_json(out, 3000)
    except Exception as exc:
        return _v12_error(exc)


@server.tool()
async def capability_self_check() -> str:
    """v12.0 离线自检：编排层 / 登录层 / 交付层三条线各自跑一遍不联网的断言。

    交付层会现场 build 一个含 sha256 清单的包再 verify，证明"打包下载"这条链
    在当前环境真的可用（缺库、目录不可写、编码问题都会在这里暴露）。装完技能或
    换了机器先跑这个，再决定要不要联网。

    返回 {"ok", "pipeline", "login", "delivery"}；任一项为 False 都说明某条线断了。
    """
    pr, err1 = _v12_pipeline()
    ls, err2 = _v12_login()
    cp, err3 = _v12_packager()
    modules = {"pipeline_runner": pr is not None, "login_session": ls is not None,
               "crawl_packager": cp is not None}
    try:
        import tempfile
        import shutil

        def _run():
            out: dict = {"ok": all(modules), "modules": modules,
                         "skill_version": cp._skill_version() if cp else "unknown"}
            if err1:
                out["pipeline"] = err1
            if err2:
                out["login"] = err2
            if err3:
                out["delivery"] = err3
            if pr is not None:
                out["pipeline"] = pr.self_check()
            if ls is not None:
                out["login"] = ls.self_check()
            if cp is not None:
                base = tempfile.mkdtemp(prefix="cnfs_selfcheck_")
                try:
                    src = Path(base) / "sample.json"
                    src.write_text(json.dumps({"rows": [1, 2, 3]}, ensure_ascii=False),
                                   encoding="utf-8")
                    built = _v12_quiet(cp.build_delivery_package, [str(src)],
                                       zip_name="selfcheck", output_dir=base)
                    report = cp.verify_delivery_package(built["zip_path"])
                    out["delivery"] = {"ok": bool(built.get("zip_path"))
                                       and bool(report.get("ok")),
                                       "file_count": built.get("file_count"),
                                       "zip_size_kb": built.get("zip_size_kb"),
                                       "verify": report}
                finally:
                    shutil.rmtree(base, ignore_errors=True)
            for key in ("pipeline", "login", "delivery"):
                val = out.get(key)
                if isinstance(val, dict) and val.get("ok") is False:
                    out["ok"] = False
            out["next_step"] = ("三条线自检通过，可直接联网实跑" if out["ok"] else
                                 "看 out 里哪一项 ok=false，按其 error/failed 字段处置后再跑")
            return out
        return _v12_json(await asyncio.to_thread(_run), 6000)
    except Exception as exc:
        return _v12_error(exc)


# ==================== v10.0 Resource + Prompt 注册 ====================

try:
    from mcp_resources_prompts import register_resources_and_prompts  # type: ignore
except ImportError:
    try:
        from scripts.mcp_resources_prompts import register_resources_and_prompts
    except ImportError:
        register_resources_and_prompts = None  # type: ignore

if register_resources_and_prompts is not None:
    try:
        _r_p_result = register_resources_and_prompts(server)
        log.info(f"v10.0 Resources+Prompts registered: {_r_p_result}")
    except Exception as _exc:
        log.warning(f"v10.0 Resource/Prompt registration failed: {_exc}")


# ==================== 主入口 ====================

def main():
    """MCP Server 入口 — 走 stdio 协议"""
    server.run(transport="stdio")


if __name__ == '__main__':
    main()
