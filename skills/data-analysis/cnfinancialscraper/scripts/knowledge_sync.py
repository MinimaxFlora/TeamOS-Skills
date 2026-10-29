# -*- coding: utf-8 -*-
"""
知识库总结与同步引擎 v7.7.0 (knowledge_sync.py)

把爬取/汇总的金融数据「按需求总结」为知识笔记，并同步到主流知识库
（IMA / Dify / Obsidian / 本地兜底），同时支持从知识库反向检索
校对爬取结果的数字/事实一致性。零 pip 依赖（HTTP 走 http_utils）。

能力：
  - summarize_to_notes      : 聚合数据 → 知识笔记（overall/target/negative 三种需求模式）
  - ObsidianAdapter         : 本地 Vault 目录，Markdown + YAML frontmatter
  - ImaAdapter              : 腾讯 IMA OpenAPI（import_doc + add_knowledge + search）
  - DifyAdapter             : Dify Knowledge API（create-by-text + retrieve + 文档更新）
  - LocalKnowledgeAdapter   : data/knowledge_base/ 本地兜底（无凭证即可用）
  - sync_to_knowledge_base  : 汇总 → 总结 → 推送指定适配器，写同步日志
  - verify_against_knowledge_base : 用知识库条目反向校对爬取数字/事实

配置（data/knowledge_config.json，均可用环境变量覆盖）：
  {
    "target": "ima",                       # 默认适配器
    "obsidian": {"vault_path": "D:/vault"},
    "ima":  {"knowledge_base_id": "..."},  # client_id/api_key 走环境变量
    "dify": {"base_url": "https://api.dify.ai", "dataset_id": "...",
             "dataset_name": "cn-financial"}  # api_key 走环境变量
  }
环境变量：
  IMA_OPENAPI_CLIENTID / IMA_OPENAPI_APIKEY（或 ~/.config/ima/client_id、api_key）
  DIFY_API_KEY / DIFY_BASE_URL / DIFY_DATASET_ID
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

try:  # 包导入（from scripts import ...）
    from .data_aggregator import (aggregate_snapshots, sentiment_statistics,
                                  title_similarity)
    from .http_utils import http_get, http_post
except ImportError:  # 直接导入（scripts/ 已在 sys.path）
    from data_aggregator import (aggregate_snapshots, sentiment_statistics,
                                 title_similarity)
    from http_utils import http_get, http_post

SKILL_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = SKILL_DIR / "data"
CONFIG_FILE = DATA_DIR / "knowledge_config.json"
KB_DIR = DATA_DIR / "knowledge_base"          # 本地兜底库
SYNC_LOG = DATA_DIR / "knowledge_sync_log.json"


class KnowledgeBaseError(Exception):
    """知识库操作失败（凭证缺失 / 接口报错 / 路径无效）。"""


@dataclass
class KnowledgeNote:
    """一篇知识笔记（适配器无关的统一载体）。"""
    title: str
    content: str                                   # Markdown 正文
    tags: List[str] = field(default_factory=list)
    source_refs: List[str] = field(default_factory=list)   # 来源 URL
    category: str = "金融数据汇总"

    def to_frontmatter_markdown(self) -> str:
        """带 YAML frontmatter 的 Markdown（Obsidian 风格）。"""
        fm = ["---"]
        fm.append(f"title: {self.title}")
        fm.append(f"category: {self.category}")
        if self.tags:
            fm.append("tags: [" + ", ".join(self.tags) + "]")
        fm.append(f"created: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        fm.append("---")
        return "\n".join(fm) + "\n\n" + self.content


# ════════════════ 1. 按需总结器 ════════════════

def _fmt_kv(pairs) -> str:
    return "、".join(f"{k}({v})" for k, v in pairs) if pairs else "—"


def _render_overview_md(agg: Dict[str, Any], requirement: str = "") -> str:
    st = agg.get("statistics") or {}
    lines = ["## 概览", ""]
    if requirement:
        lines += [f"> 总结需求：{requirement}", ""]
    lines += [
        f"- 快照数：{agg.get('snapshot_count', 0)}，原始条目 "
        f"{agg.get('raw_count', 0)}，去重后 {agg.get('dedup_count', 0)}",
        f"- 负面占比 {st.get('neg_ratio', 0):.1%}，"
        f"正面占比 {st.get('pos_ratio', 0):.1%}，平均分 {st.get('avg_score', 0)}",
        "",
    ]
    senti = st.get("sentiment") or {}
    if senti:
        lines += ["## 情感分布", "",
                  " | ".join(f"{k}: {v}" for k, v in senti.items()), ""]
    hot = st.get("hot_keywords") or []
    if hot:
        lines += ["## 高频关键词", "", _fmt_kv(hot[:15]), ""]
    by_target = agg.get("by_target") or []
    if by_target:
        lines += ["## 目标机构截面", "",
                  "| 机构 | 条数 | 负面 | 负面占比 | 最高等级 |",
                  "|---|---|---|---|---|"]
        for row in by_target[:20]:
            lines.append(f"| {row.get('target_name', '')} | {row['count']} | "
                         f"{row['negative']} | {row['neg_ratio']:.1%} | "
                         f"{row['severity_top']} |")
        lines.append("")
    return "\n".join(lines)


def _render_articles_md(articles: List[Dict[str, Any]], limit: int = 20,
                        heading: str = "## 重点条目") -> str:
    if not articles:
        return ""
    lines = [heading, ""]
    for a in articles[:limit]:
        ref = a.get("url") or ""
        lines.append(f"- **[{a.get('sentiment', '')}/{a.get('severity', '')}]** "
                     f"{a.get('title', '')}  \n"
                     f"  来源：{a.get('source', '')}  "
                     f"时间：{a.get('publish_time', '')}  "
                     + (f"[原文]({ref})" if ref else ""))
    lines.append("")
    return "\n".join(lines)


def summarize_to_notes(agg: Optional[Dict[str, Any]] = None,
                       mode: str = "overall",
                       target: str = "",
                       requirement: str = "",
                       top_articles: int = 20,
                       snapshot_dir: Optional[Path] = None) -> List[KnowledgeNote]:
    """按需求把聚合结果总结为知识笔记列表。

    Args:
        agg: 聚合结果（默认现场聚合 data/sentiment_snapshots/）
        mode: overall=总体汇总 / target=按目标机构（需 target）/
              negative=仅负面风险
        requirement: 用户自定义总结需求（写入笔记头部，指导后续阅读）
        top_articles: 每篇笔记附带的重点条目上限
    """
    if agg is None:
        agg = aggregate_snapshots(snapshot_dir)
    if not agg.get("articles"):
        return []

    articles = agg["articles"]
    tags = ["金融舆情", "cn-financial-scraper"]
    refs = [a["url"] for a in articles[:30] if a.get("url")]

    if mode == "target":
        if not target:
            raise ValueError("mode='target' 时必须指定 target")
        related = [a for a in articles
                   if target in (a.get("target_name") or "")
                   or target in (a.get("title") or "")]
        if not related:
            return []
        st = sentiment_statistics(related)
        content = [f"# {target} 舆情总结", ""]
        if requirement:
            content += [f"> 总结需求：{requirement}", ""]
        content += [
            f"- 相关条目 {len(related)} 条，负面 {st['sentiment'].get('negative', 0)} 条"
            f"（{st['neg_ratio']:.1%}），正面 {st['sentiment'].get('positive', 0)} 条",
            "",
            f"高频关键词：{_fmt_kv(st.get('hot_keywords') or [])}",
            "",
            _render_articles_md(related, top_articles),
        ]
        return [KnowledgeNote(
            title=f"{target} 舆情总结 {datetime.now():%Y%m%d}",
            content="\n".join(content), tags=tags + [target],
            source_refs=[a["url"] for a in related[:30] if a.get("url")],
            category="机构舆情")]

    if mode == "negative":
        neg = [a for a in articles if a.get("sentiment") == "negative"]
        if not neg:
            return []
        neg.sort(key=lambda a: float(a.get("sentiment_score") or 0))  # 分数越低越严重
        st = sentiment_statistics(neg)
        content = [
            "# 负面舆情风险总结", "",
            f"> 总结需求：{requirement}" if requirement else "> 仅汇总负面条目", "",
            f"- 负面条目 {len(neg)} 条",
            f"- 高频风险词：{_fmt_kv(st.get('hot_keywords') or [])}",
            "",
            _render_articles_md(neg, top_articles, heading="## 负面条目"),
        ]
        return [KnowledgeNote(
            title=f"负面舆情风险总结 {datetime.now():%Y%m%d}",
            content="\n".join(content), tags=tags + ["风险"],
            source_refs=[a["url"] for a in neg[:30] if a.get("url")],
            category="风险提示")]

    # mode == "overall"
    content = "# 金融舆情总体汇总\n\n"
    content += _render_overview_md(agg, requirement)
    content += _render_articles_md(articles, top_articles)
    return [KnowledgeNote(
        title=f"金融舆情总体汇总 {datetime.now():%Y%m%d}",
        content=content, tags=tags, source_refs=refs,
        category="金融数据汇总")]


# ════════════════ 2. 配置加载 ════════════════

def load_kb_config(path: Optional[Path] = None) -> Dict[str, Any]:
    """加载 data/knowledge_config.json（不存在返回空）。"""
    p = Path(path) if path else CONFIG_FILE
    if not p.exists():
        return {}
    try:
        doc = json.loads(p.read_text(encoding="utf-8"))
        return doc if isinstance(doc, dict) else {}
    except Exception:
        return {}


def save_kb_config(cfg: Dict[str, Any], path: Optional[Path] = None) -> Path:
    p = Path(path) if path else CONFIG_FILE
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    return p


def _ima_credentials() -> Dict[str, str]:
    """IMA 凭证：环境变量优先，其次 ~/.config/ima/client_id|api_key。"""
    cid = os.environ.get("IMA_OPENAPI_CLIENTID", "").strip()
    key = os.environ.get("IMA_OPENAPI_APIKEY", "").strip()
    if not cid or not key:
        home = Path.home() / ".config" / "ima"
        if not cid and (home / "client_id").exists():
            cid = (home / "client_id").read_text(encoding="utf-8").strip()
        if not key and (home / "api_key").exists():
            key = (home / "api_key").read_text(encoding="utf-8").strip()
    return {"client_id": cid, "api_key": key}


# ════════════════ 3. 适配器 ════════════════

class KnowledgeAdapter:
    """知识库适配器基类：push 写入、search 检索（校对用）。"""

    name = "base"

    def available(self) -> bool:          # pragma: no cover - 子类覆盖
        raise NotImplementedError

    def push(self, note: KnowledgeNote) -> Dict[str, Any]:
        raise NotImplementedError

    def search(self, query: str, limit: int = 5) -> List[Dict[str, Any]]:
        raise NotImplementedError

    # -- 通用：同步历史（本地所有适配器共享，用于幂等/审计） --
    def note_digest(self, note: KnowledgeNote) -> str:
        return hashlib.md5((note.title + "\n" + note.content).encode("utf-8")
                           ).hexdigest()[:16]


class LocalKnowledgeAdapter(KnowledgeAdapter):
    """本地兜底知识库：data/knowledge_base/<category>/<title>.md + index.json。"""

    name = "local"

    def __init__(self, base_dir: Optional[Path] = None):
        self.base_dir = Path(base_dir) if base_dir else KB_DIR
        self.index_file = self.base_dir / "_index.json"

    def available(self) -> bool:
        return True

    def _load_index(self) -> Dict[str, Any]:
        if self.index_file.exists():
            try:
                return json.loads(self.index_file.read_text(encoding="utf-8"))
            except Exception:
                pass
        return {"entries": []}

    def _save_index(self, idx: Dict[str, Any]) -> None:
        self.index_file.parent.mkdir(parents=True, exist_ok=True)
        self.index_file.write_text(
            json.dumps(idx, ensure_ascii=False, indent=1), encoding="utf-8")

    @staticmethod
    def _safe_name(title: str) -> str:
        return re.sub(r'[\\/:*?"<>|\r\n]+', "_", title).strip()[:80] or "untitled"

    def push(self, note: KnowledgeNote) -> Dict[str, Any]:
        cat_dir = self.base_dir / self._safe_name(note.category)
        cat_dir.mkdir(parents=True, exist_ok=True)
        digest = self.note_digest(note)
        idx = self._load_index()
        # 幂等：同 digest 已存在则跳过
        for e in idx["entries"]:
            if e.get("digest") == digest:
                return {"adapter": self.name, "status": "skipped",
                        "path": e.get("path"), "digest": digest}
        fp = cat_dir / (self._safe_name(note.title) + ".md")
        suffix = 1
        while fp.exists():
            fp = cat_dir / f"{self._safe_name(note.title)}_{suffix}.md"
            suffix += 1
        fp.write_text(note.to_frontmatter_markdown(), encoding="utf-8")
        idx["entries"].append({
            "title": note.title, "category": note.category,
            "tags": note.tags, "path": str(fp),
            "digest": digest,
            "created": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        })
        self._save_index(idx)
        return {"adapter": self.name, "status": "pushed",
                "path": str(fp), "digest": digest}

    def search(self, query: str, limit: int = 5) -> List[Dict[str, Any]]:
        """标题/正文关键词检索（离线，按关键词命中数排序）。"""
        idx = self._load_index()
        kws = [w for w in re.split(r"[\s,，]+", query) if w]
        scored = []
        for e in idx["entries"]:
            path = Path(e.get("path", ""))
            text = path.read_text(encoding="utf-8") if path.exists() else ""
            hay = (e.get("title", "") + "\n" + text).lower()
            hits = sum(hay.count(k.lower()) for k in kws)
            if hits:
                scored.append((hits, {"title": e.get("title", ""),
                                      "path": str(path),
                                      "snippet": text[:600],
                                      "category": e.get("category", "")}))
        scored.sort(key=lambda x: -x[0])
        return [s[1] for s in scored[:limit]]


class ObsidianAdapter(LocalKnowledgeAdapter):
    """Obsidian Vault：直接写 Markdown 到 vault 目录（Obsidian 自动索引）。"""

    name = "obsidian"

    def __init__(self, vault_path: Optional[Path] = None):
        if vault_path is None:
            cfg = load_kb_config().get("obsidian") or {}
            vault_path = cfg.get("vault_path") or os.environ.get("OBSIDIAN_VAULT_PATH")
        super().__init__(vault_path)

    def available(self) -> bool:
        return bool(self.base_dir) and Path(self.base_dir).exists()


class _HttpJsonAdapter(KnowledgeAdapter):
    """远程 JSON API 适配器公共层：http_post + session 注入（便于测试）。"""

    def __init__(self, session=None):
        self.session = session

    def _post(self, url: str, json_body: Dict, headers: Dict,
              timeout: int = 30) -> Dict[str, Any]:
        resp = http_post(url, json_body=json_body, headers=headers,
                         timeout=timeout, retries=2, session=self.session,
                         use_cache=False, scheme_fallback=False)
        if resp is None:
            raise KnowledgeBaseError(f"请求失败（无响应）: {url[:100]}")
        try:
            return resp.json()
        except Exception as exc:
            raise KnowledgeBaseError(
                f"响应非 JSON: {url[:100]} body={getattr(resp, 'text', '')[:200]}") from exc


class ImaAdapter(_HttpJsonAdapter):
    """腾讯 IMA OpenAPI：创建笔记 → 关联知识库；搜索用于校对。

    端点（POST，Header: ima-openapi-clientid / ima-openapi-apikey）：
      /openapi/note/v1/import_doc        创建笔记（Markdown）
      /openapi/wiki/v1/add_knowledge     关联笔记到知识库（media_type=11）
      /openapi/wiki/v1/search_knowledge  知识库检索
    """

    name = "ima"
    BASE = "https://ima.qq.com"

    def __init__(self, client_id: str = "", api_key: str = "",
                 knowledge_base_id: str = "", session=None):
        super().__init__(session)
        creds = _ima_credentials()
        self.client_id = client_id or creds["client_id"]
        self.api_key = api_key or creds["api_key"]
        self.knowledge_base_id = (
            knowledge_base_id
            or os.environ.get("IMA_KNOWLEDGE_BASE_ID", "")
            or (load_kb_config().get("ima") or {}).get("knowledge_base_id", ""))

    def available(self) -> bool:
        return bool(self.client_id and self.api_key)

    def _headers(self) -> Dict[str, str]:
        return {"ima-openapi-clientid": self.client_id,
                "ima-openapi-apikey": self.api_key,
                "Content-Type": "application/json"}

    def _ensure_creds(self) -> None:
        if not self.available():
            raise KnowledgeBaseError(
                "IMA 凭证缺失：请设置 IMA_OPENAPI_CLIENTID / IMA_OPENAPI_APIKEY "
                "或 ~/.config/ima/client_id、api_key（https://ima.qq.com/agent-interface 获取）")

    def push(self, note: KnowledgeNote) -> Dict[str, Any]:
        self._ensure_creds()
        # 1) 创建笔记（content_format=1 → Markdown）
        doc = self._post(self.BASE + "/openapi/note/v1/import_doc",
                         {"content_format": 1, "title": note.title,
                          "content": note.to_frontmatter_markdown()},
                         self._headers())
        if doc.get("code") not in (0, None):
            raise KnowledgeBaseError(f"IMA import_doc 失败: {doc}")
        note_id = (doc.get("data") or {}).get("note_id") or doc.get("note_id", "")
        result: Dict[str, Any] = {"adapter": self.name, "status": "pushed",
                                  "note_id": note_id}
        # 2) 若配置了知识库则关联
        if self.knowledge_base_id and note_id:
            add = self._post(self.BASE + "/openapi/wiki/v1/add_knowledge",
                             {"media_type": 11, "title": note.title,
                              "knowledge_base_id": self.knowledge_base_id,
                              "note_id": note_id},
                             self._headers())
            result["linked"] = add.get("code") == 0
        return result

    def search(self, query: str, limit: int = 5) -> List[Dict[str, Any]]:
        self._ensure_creds()
        body: Dict[str, Any] = {"query": query, "cursor": "", "limit": limit}
        if self.knowledge_base_id:
            body["knowledge_base_id"] = self.knowledge_base_id
        doc = self._post(self.BASE + "/openapi/wiki/v1/search_knowledge",
                         body, self._headers())
        if doc.get("code") not in (0, None):
            raise KnowledgeBaseError(f"IMA search_knowledge 失败: {doc}")
        data = doc.get("data") or doc
        items = data.get("items") or data.get("list") or []
        return [{"title": it.get("title", ""),
                 "snippet": it.get("content", "") or it.get("summary", ""),
                 "source": "ima"} for it in items[:limit]]


class DifyAdapter(_HttpJsonAdapter):
    """Dify Knowledge API：create-by-text 写入 / retrieve 检索 / 同名文档更新。

    端点（Bearer Token）：
      GET  /v1/datasets                                      知识库列表（定位 dataset）
      POST /v1/datasets                                      创建空知识库
      GET  /v1/datasets/{id}/documents                       文档列表（同名检测）
      POST /v1/datasets/{id}/document/create-by-text         新增文档
      POST /v1/datasets/{id}/documents/{doc}/update-by-text  更新已有文档
      POST /v1/datasets/{id}/retrieve                        语义检索（校对用）
    """

    name = "dify"

    def __init__(self, api_key: str = "", base_url: str = "",
                 dataset_id: str = "", dataset_name: str = "cn-financial-scraper",
                 session=None):
        super().__init__(session)
        cfg = load_kb_config().get("dify") or {}
        self.api_key = api_key or os.environ.get("DIFY_API_KEY", "")
        self.base_url = (base_url or os.environ.get("DIFY_BASE_URL")
                         or cfg.get("base_url") or "https://api.dify.ai").rstrip("/")
        self.dataset_id = dataset_id or os.environ.get("DIFY_DATASET_ID") \
            or cfg.get("dataset_id", "")
        self.dataset_name = dataset_name or cfg.get("dataset_name",
                                                    "cn-financial-scraper")

    def available(self) -> bool:
        return bool(self.api_key)

    def _headers(self) -> Dict[str, str]:
        return {"Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json"}

    def _ensure_key(self) -> None:
        if not self.available():
            raise KnowledgeBaseError(
                "Dify API Key 缺失：请设置 DIFY_API_KEY 环境变量 "
                "（Dify 控制台 → 知识库 → API 访问）")

    def _get(self, url: str) -> Dict[str, Any]:
        resp = http_get(url, headers=self._headers(), timeout=30,
                        retries=2, session=self.session, use_cache=False,
                        detect_soft_block=False, scheme_fallback=False)
        if resp is None:
            raise KnowledgeBaseError(f"请求失败（无响应）: {url[:100]}")
        try:
            return resp.json()
        except Exception as exc:
            raise KnowledgeBaseError(f"响应非 JSON: {url[:100]}") from exc

    def _resolve_dataset(self) -> str:
        """dataset_id 未配置时按名称查找，找不到则创建。"""
        if self.dataset_id:
            return self.dataset_id
        doc = self._get(f"{self.base_url}/v1/datasets?limit=100")
        for ds in (doc.get("data") or []):
            if ds.get("name") == self.dataset_name:
                self.dataset_id = ds["id"]
                return self.dataset_id
        created = self._post(f"{self.base_url}/v1/datasets",
                             {"name": self.dataset_name,
                              "indexing_technique": "high_quality"},
                             self._headers())
        self.dataset_id = created.get("id", "")
        if not self.dataset_id:
            raise KnowledgeBaseError(f"Dify 创建知识库失败: {created}")
        return self.dataset_id

    def _find_document(self, dataset_id: str, name: str) -> str:
        doc = self._get(f"{self.base_url}/v1/datasets/{dataset_id}"
                        f"/documents?keyword={name}&limit=20")
        for d in (doc.get("data") or []):
            if d.get("name") == name:
                return d.get("id", "")
        return ""

    def push(self, note: KnowledgeNote) -> Dict[str, Any]:
        self._ensure_key()
        ds = self._resolve_dataset()
        name = note.title[:80]
        existing = self._find_document(ds, name)
        body = {
            "name": name,
            "text": note.to_frontmatter_markdown(),
            "indexing_technique": "high_quality",
            "doc_language": "Chinese",
            "process_rule": {"mode": "automatic"},
        }
        if existing:
            doc = self._post(f"{self.base_url}/v1/datasets/{ds}"
                             f"/documents/{existing}/update-by-text",
                             {"name": name,
                              "text": body["text"],
                              "doc_language": "Chinese",
                              "process_rule": {"mode": "automatic"}},
                             self._headers())
            status = "updated"
        else:
            doc = self._post(f"{self.base_url}/v1/datasets/{ds}"
                             "/document/create-by-text", body, self._headers())
            status = "pushed"
        if "document" not in doc and "result" not in doc and doc.get("code"):
            raise KnowledgeBaseError(f"Dify 写入失败: {doc}")
        return {"adapter": self.name, "status": status,
                "dataset_id": ds,
                "document_id": (doc.get("document") or {}).get("id", "")}

    def search(self, query: str, limit: int = 5) -> List[Dict[str, Any]]:
        self._ensure_key()
        ds = self._resolve_dataset()
        doc = self._post(f"{self.base_url}/v1/datasets/{ds}/retrieve",
                         {"query": query,
                          "retrieval_model": {
                              "search_method": "semantic_search",
                              "reranking_enable": False,
                              "top_k": limit,
                              "score_threshold_enabled": False}},
                         self._headers())
        records = doc.get("records") or []
        return [{"title": (r.get("segment") or {}).get("document", {}).get("name", ""),
                 "snippet": (r.get("segment") or {}).get("content", ""),
                 "score": r.get("score"),
                 "source": "dify"} for r in records[:limit]]


ADAPTERS: Dict[str, type] = {
    "local": LocalKnowledgeAdapter,
    "obsidian": ObsidianAdapter,
    "ima": ImaAdapter,
    "dify": DifyAdapter,
}


def get_adapter(name: Optional[str] = None, **kwargs) -> KnowledgeAdapter:
    """按名称取适配器（默认读配置 target，未配置兜底 local）。"""
    name = (name or load_kb_config().get("target") or "local").lower()
    cls = ADAPTERS.get(name)
    if cls is None:
        raise KnowledgeBaseError(f"未知知识库适配器: {name}（支持 "
                                 + "/".join(ADAPTERS) + "）")
    return cls(**kwargs)


# ════════════════ 4. 同步引擎 ════════════════

def sync_to_knowledge_base(target: Optional[str] = None,
                           mode: str = "overall",
                           requirement: str = "",
                           agg: Optional[Dict[str, Any]] = None,
                           top_articles: int = 20,
                           target_name: str = "",
                           adapter: Optional[KnowledgeAdapter] = None) -> Dict[str, Any]:
    """汇总 → 按需求总结 → 推送到知识库，返回同步结果（含失败明细）。

    target_name: mode='target' 时的机构/目标名（v7.7 补齐透传）。
    """
    notes = summarize_to_notes(agg, mode=mode, target=target_name,
                               requirement=requirement,
                               top_articles=top_articles)
    if not notes:
        return {"target": (target or "local"), "pushed": 0, "skipped": 0,
                "failed": [], "message": "无可总结的数据（快照为空）"}
    ad = adapter or get_adapter(target)
    results, failed = [], []
    for note in notes:
        try:
            results.append(ad.push(note))
        except (KnowledgeBaseError, Exception) as exc:  # 单篇失败不阻断其余
            failed.append({"title": note.title, "error": str(exc)})
    # 同步日志（追加）
    entry = {"time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
             "target": ad.name, "mode": mode,
             "pushed": sum(1 for r in results if r.get("status") == "pushed"),
             "updated": sum(1 for r in results if r.get("status") == "updated"),
             "skipped": sum(1 for r in results if r.get("status") == "skipped"),
             "failed": failed}
    try:
        log = (json.loads(SYNC_LOG.read_text(encoding="utf-8"))
               if SYNC_LOG.exists() else [])
    except Exception:
        log = []
    log.append(entry)
    SYNC_LOG.parent.mkdir(parents=True, exist_ok=True)
    SYNC_LOG.write_text(json.dumps(log[-200:], ensure_ascii=False, indent=1),
                        encoding="utf-8")
    return {"target": ad.name, "pushed": entry["pushed"],
            "updated": entry["updated"], "skipped": entry["skipped"],
            "failed": failed, "results": results}


# ════════════════ 5. 知识库校对 ════════════════

_NUM_RE = re.compile(r"\d+(?:,\d{3})*(?:\.\d+)?")
_FACT_LABELS = ("营收", "净利润", "同比", "环比", "处罚", "罚款", "评级", "净值")


def extract_facts(text: str) -> List[Dict[str, str]]:
    """从文本抽取「标签+数字」事实（用于双向比对）。"""
    facts = []
    for label in _FACT_LABELS:
        for m in re.finditer(re.escape(label) + r"[^\n。；;]{0,24}?"
                             r"(" + _NUM_RE.pattern + r")", text):
            facts.append({"label": label, "value": m.group(1),
                          "context": m.group(0).strip()})
    return facts


def verify_against_knowledge_base(items: List[Dict[str, Any]],
                                  adapter: Optional[KnowledgeAdapter] = None,
                                  top_k: int = 3,
                                  title_threshold: float = 0.85) -> Dict[str, Any]:
    """用知识库条目反向校对爬取结果。

    对每条爬取条目：检索知识库 → 标题相似匹配 → 数字/事实比对，
    输出 verified（一致）/ conflict（数字冲突）/ no_match（库中无记录）。
    """
    ad = adapter or get_adapter()
    results = []
    for item in items:
        title = item.get("title") or ""
        if not title:
            continue
        try:
            hits = ad.search(title, limit=top_k)
        except KnowledgeBaseError as exc:
            results.append({"title": title, "status": "error",
                            "error": str(exc)})
            continue
        matched = [h for h in hits
                   if title_similarity(title, h.get("title") or h.get("snippet", "")[:40])
                   >= title_threshold or (h.get("snippet") and title[:8] in h["snippet"])]
        if not matched:
            results.append({"title": title, "status": "no_match",
                            "hits": len(hits)})
            continue
        # 数字事实比对
        src_facts = {f["label"] + "=" + f["value"]
                     for f in extract_facts(item.get("summary") or item.get("content") or "")}
        conflicts = []
        for h in matched:
            kb_text = h.get("snippet") or h.get("content") or ""
            kb_facts = {f["label"] + "=" + f["value"] for f in extract_facts(kb_text)}
            for sf in src_facts:
                label, val = sf.split("=", 1)
                kb_same_label = {kf.split("=", 1)[1] for kf in kb_facts
                                 if kf.startswith(label + "=")}
                if kb_same_label and val not in kb_same_label:
                    conflicts.append({"label": label, "crawled": val,
                                      "knowledge": sorted(kb_same_label)})
        results.append({"title": title,
                        "status": "conflict" if conflicts else "verified",
                        "matched": [h.get("title", "") for h in matched],
                        "conflicts": conflicts})
    ok = sum(1 for r in results if r["status"] == "verified")
    return {"total": len(results),
            "verified": ok,
            "conflicts": sum(1 for r in results if r["status"] == "conflict"),
            "no_match": sum(1 for r in results if r["status"] == "no_match"),
            "results": results}


__all__ = [
    "KnowledgeBaseError", "KnowledgeNote",
    "summarize_to_notes",
    "load_kb_config", "save_kb_config",
    "KnowledgeAdapter", "LocalKnowledgeAdapter", "ObsidianAdapter",
    "ImaAdapter", "DifyAdapter", "ADAPTERS", "get_adapter",
    "sync_to_knowledge_base",
    "extract_facts", "verify_against_knowledge_base",
]

if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="v7.7.0 知识库总结与同步引擎")
    ap.add_argument("--target", default="", help="local/obsidian/ima/dify")
    ap.add_argument("--mode", default="overall",
                    choices=["overall", "target", "negative"])
    ap.add_argument("--requirement", default="", help="自定义总结需求")
    ap.add_argument("--target-name", default="", help="mode=target 时的机构名")
    ap.add_argument("--verify", default="", help="校对：传入 JSON 文件路径")
    args = ap.parse_args()
    if args.verify:
        data = json.loads(Path(args.verify).read_text(encoding="utf-8"))
        report = verify_against_knowledge_base(
            data if isinstance(data, list) else data.get("articles", []),
            adapter=get_adapter(args.target or None))
        print(json.dumps(report, ensure_ascii=False, indent=1))
    else:
        res = sync_to_knowledge_base(target=args.target or None,
                                     mode=args.mode,
                                     requirement=args.requirement)
        print(json.dumps(res, ensure_ascii=False, indent=1))
