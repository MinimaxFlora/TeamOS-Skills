# -*- coding: utf-8 -*-
"""
批量爬取结果 ZIP 打包模块 v4.0
将爬取结果按类型分目录、生成索引文件，打包为 ZIP 供用户下载。

功能：
- 收集爬取结果 → 按类型/来源分目录
- 生成索引 README.md + metadata JSON
- 打包 ZIP（支持分卷 >50MB）
- 支持自定义文件名、输出目录
- 增强 BatchInstitutionCrawler 添加 crawl_and_package() 方法
"""

import os
import json
import hashlib
import zipfile
import shutil
import tempfile
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Dict, Any, List, Optional, Tuple, Callable
from datetime import datetime
from dataclasses import dataclass, field

SKILL_DATA_DIR = Path(__file__).parent.parent / "data"
PACKAGES_DIR = SKILL_DATA_DIR / "packages"
TEMP_DIR = SKILL_DATA_DIR / "temp_packages"

SKILL_DATA_DIR.mkdir(parents=True, exist_ok=True)
PACKAGES_DIR.mkdir(parents=True, exist_ok=True)


# ==================== 数据结构 ====================

@dataclass
class PackagedItem:
    """打包条目"""
    name: str
    content: str = ""
    content_type: str = "text"  # text / html / json / file
    source_url: str = ""
    category: str = "通用"
    metadata: Dict[str, Any] = field(default_factory=dict)
    file_path: str = ""  # 如果是文件，记录路径


@dataclass
class PackageResult:
    """打包结果"""
    zip_path: str
    zip_size_mb: float
    item_count: int
    category_count: int
    file_list: List[str]
    metadata: Dict[str, Any]
    created_at: str
    name: str = ""


# ==================== ZIP 打包器 ====================

class CrawlPackager:
    """批量爬取结果 ZIP 打包器。"""

    MAX_VOLUME_SIZE = 50 * 1024 * 1024  # 50MB 分卷阈值

    def __init__(self, output_dir: str = ""):
        self.output_dir = Path(output_dir) if output_dir else PACKAGES_DIR
        self.output_dir.mkdir(parents=True, exist_ok=True)

    def package(self, items: List[Any], zip_name: str = "",
                output_dir: str = "",
                include_metadata: bool = True,
                categorize: bool = True,
                progress_callback: Optional[Callable[[int, int, str], None]] = None) -> str:
        """
        将爬取结果打包为 ZIP。

        Args:
            items: 爬取结果列表（dict / PackagedItem / str）
            zip_name: ZIP 文件名（不含扩展名）
            output_dir: 输出目录
            include_metadata: 是否包含 metadata JSON
            categorize: 是否按类别分目录
            progress_callback: v6.0.0 进度回调 (done, total, item_name)，
                               每写入一个文件调用一次

        返回: ZIP 文件路径
        """
        if output_dir:
            self.output_dir = Path(output_dir)
            self.output_dir.mkdir(parents=True, exist_ok=True)

        # 标准化 items
        packaged = self._normalize_items(items)
        if not packaged:
            raise ValueError("没有可打包的内容")

        # 按类别分组
        categories: Dict[str, List[PackagedItem]] = {}
        if categorize:
            for item in packaged:
                cat = item.category or "其他"
                if cat not in categories:
                    categories[cat] = []
                categories[cat].append(item)
        else:
            categories["全部"] = packaged

        # 生成 ZIP 文件名
        if not zip_name:
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            zip_name = f"crawl_package_{timestamp}"

        # 创建唯一临时目录构建文件结构，避免并发/重复运行互相锁文件
        TEMP_DIR.mkdir(parents=True, exist_ok=True)
        temp_dir = Path(tempfile.mkdtemp(prefix=f"{zip_name}_", dir=str(TEMP_DIR)))

        try:
            # 写入文件
            file_list = []
            total = sum(len(c) for c in categories.values())
            done = 0
            for cat_name, cat_items in categories.items():
                cat_dir = temp_dir / self._safe_filename(cat_name)
                cat_dir.mkdir(exist_ok=True)

                for i, item in enumerate(cat_items):
                    fname = self._generate_filename(item, i)
                    fpath = cat_dir / fname
                    content = self._get_content(item)

                    if content:
                        with open(fpath, 'w', encoding='utf-8') as f:
                            f.write(content)
                        file_list.append(str(fpath.relative_to(temp_dir)))
                    # v6.0.0: 进度回调
                    done += 1
                    if progress_callback is not None:
                        try:
                            progress_callback(done, total, item.name)
                        except Exception:
                            pass

            # 生成索引 README.md
            index_content = self._generate_index(zip_name, categories, file_list)
            with open(temp_dir / "README.md", 'w', encoding='utf-8') as f:
                f.write(index_content)
            file_list.append("README.md")

            # 生成 metadata JSON
            if include_metadata:
                metadata = self._generate_metadata(zip_name, packaged, categories)
                with open(temp_dir / "metadata.json", 'w', encoding='utf-8') as f:
                    json.dump(metadata, f, ensure_ascii=False, indent=2)
                file_list.append("metadata.json")

            # 创建 ZIP
            zip_path = str(self.output_dir / f"{zip_name}.zip")

            # 检查是否需要分卷
            total_size = self._get_dir_size(temp_dir)
            if total_size > self.MAX_VOLUME_SIZE:
                return self._create_split_zip(temp_dir, zip_name, total_size)
            else:
                return self._create_zip(temp_dir, zip_path)

        finally:
            # 清理临时目录
            if temp_dir.exists():
                shutil.rmtree(temp_dir, ignore_errors=True)

    def package_from_files(self, file_paths: List[str], zip_name: str = "",
                           output_dir: str = "") -> str:
        """
        将文件列表打包为 ZIP。

        Args:
            file_paths: 文件路径列表
            zip_name: ZIP 文件名
            output_dir: 输出目录

        返回: ZIP 文件路径
        """
        if output_dir:
            self.output_dir = Path(output_dir)
            self.output_dir.mkdir(parents=True, exist_ok=True)

        if not zip_name:
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            zip_name = f"file_package_{timestamp}"

        zip_path = str(self.output_dir / f"{zip_name}.zip")

        with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zf:
            for fp in file_paths:
                if os.path.isfile(fp):
                    arcname = os.path.basename(fp)
                    zf.write(fp, arcname)

        size_mb = os.path.getsize(zip_path) / (1024 * 1024)
        print(f"✅ ZIP 已创建: {zip_path} ({size_mb:.2f} MB, {len(file_paths)} 个文件)")
        return zip_path

    def package_batch_crawl(self, names: str = "",
                            institution_type: str = "",
                            zip_name: str = "",
                            parallel_workers: int = 1,
                            retries: int = 2,
                            progress_callback: Optional[Callable[[int, int, str], None]] = None) -> str:
        """
        批量爬取 + 自动打包 ZIP（一站式）。

        v6.0.0 增强：
          - 并发爬取（ThreadPoolExecutor，默认 1=顺序，建议 ≤5）
          - 失败自动重试（默认 2 次）
          - 失败清单 failed.json 写入包内
          - 进度回调（爬取阶段 done/total/name）

        Args:
            names: 机构名称列表（逗号分隔）
            institution_type: 机构类型
            zip_name: ZIP 文件名
            parallel_workers: v6.0.0 并发爬取线程数（0/1=顺序）
            retries: v6.0.0 单个机构失败重试次数
            progress_callback: v6.0.0 进度回调 (done, total, name)

        返回: ZIP 文件路径
        """
        items = []
        failed_items: List[Dict[str, Any]] = []

        # 执行批量爬取
        try:
            from batch_institution_crawler import BatchInstitutionCrawler
            crawler = BatchInstitutionCrawler()

            if names:
                inst_list = [{"name": n.strip()} for n in names.split(",") if n.strip()]
                crawl_targets = inst_list
            elif institution_type:
                crawl_targets = [{"type": institution_type}]
            else:
                crawl_targets = []

            def _crawl_one(target: Dict[str, str]) -> Dict[str, Any]:
                """单目标爬取（带失败重试）。"""
                for attempt in range(retries + 1):
                    try:
                        if "type" in target and not target.get("name"):
                            r = crawler.crawl_by_type(target["type"])
                            if not r:
                                raise RuntimeError("empty result")
                            # 类型爬取返回列表，合并为多条
                            return {"__multi__": True, "results": r}
                        else:
                            r = crawler.crawl_by_names([target])
                            if not r:
                                raise RuntimeError("empty result")
                            return {"__multi__": False, "results": r}
                    except Exception:
                        if attempt < retries:
                            import time as _t
                            _t.sleep(0.5 * (attempt + 1))
                return {"__multi__": False, "results": [], "failed": True}

            if crawl_targets:
                total = len(crawl_targets)
                done = 0
                if parallel_workers and parallel_workers > 1:
                    # v6.0.0: 并发爬取
                    with ThreadPoolExecutor(max_workers=min(parallel_workers, 8)) as ex:
                        futures = {ex.submit(_crawl_one, t): t for t in crawl_targets}
                        for fut in as_completed(futures):
                            t = futures[fut]
                            name = t.get("name", t.get("type", "未知"))
                            try:
                                out = fut.result()
                            except Exception:
                                out = {"__multi__": False, "results": [], "failed": True}
                            self._collect_crawl_results(out, items, failed_items)
                            done += 1
                            if progress_callback is not None:
                                try:
                                    progress_callback(done, total, name)
                                except Exception:
                                    pass
                else:
                    for t in crawl_targets:
                        name = t.get("name", t.get("type", "未知"))
                        out = _crawl_one(t)
                        self._collect_crawl_results(out, items, failed_items)
                        done += 1
                        if progress_callback is not None:
                            try:
                                progress_callback(done, total, name)
                            except Exception:
                                pass

        except ImportError:
            pass

        if not items:
            raise ValueError("未获取到任何爬取结果")

        # v6.0.0: 失败清单写入包内
        if failed_items:
            items.append({
                "name": "failed_清单",
                "content": json.dumps(failed_items, ensure_ascii=False, indent=2),
                "content_type": "json",
                "category": "失败清单",
                "metadata": {"note": "爬取失败项（已自动重试）"},
            })

        return self.package(items, zip_name=zip_name,
                            progress_callback=progress_callback)

    @staticmethod
    def _collect_crawl_results(out: Dict[str, Any],
                               items: List[Dict[str, Any]],
                               failed_items: List[Dict[str, Any]]):
        """收集单目标爬取结果（并发/顺序共用）。"""
        if out.get("failed"):
            failed_items.append({"name": "未知", "reason": "爬取失败"})
            return
        results = out.get("results", []) or []
        for r in results:
            ok = r.get("success", False) if isinstance(r, dict) else True
            entry = {
                "name": r.get("name", "未知"),
                "content": r.get("content", ""),
                "success": ok,
            }
            if ok and entry.get("content"):
                items.append(entry)
            else:
                failed_items.append({"name": entry["name"], "reason": "内容为空或爬取失败"})

    # ---------- 内部方法 ----------

    def _normalize_items(self, items: List[Any]) -> List[PackagedItem]:
        """标准化多种输入格式为 PackagedItem 列表。"""
        packaged = []

        for item in items:
            if isinstance(item, PackagedItem):
                packaged.append(item)
            elif isinstance(item, dict):
                packaged.append(PackagedItem(
                    name=item.get("name", item.get("title", "未命名")),
                    content=item.get("content", item.get("text_content", "")),
                    source_url=item.get("url", item.get("source", "")),
                    category=item.get("category", item.get("type", "通用")),
                    metadata=item.get("metadata", {}),
                    file_path=item.get("file_path", ""),
                ))
            elif isinstance(item, str):
                packaged.append(PackagedItem(
                    name=f"content_{len(packaged)+1}",
                    content=item,
                ))
            else:
                packaged.append(PackagedItem(
                    name=str(item)[:100],
                    content=str(item),
                ))

        return packaged

    def _get_content(self, item: PackagedItem) -> str:
        """获取条目的文本内容。"""
        if item.content:
            return item.content
        if item.file_path and os.path.isfile(item.file_path):
            try:
                with open(item.file_path, 'r', encoding='utf-8') as f:
                    return f.read()
            except Exception:
                return ""
        return json.dumps(item.metadata, ensure_ascii=False) if item.metadata else ""

    def _generate_filename(self, item: PackagedItem, index: int) -> str:
        """为条目生成安全的文件名。"""
        base = self._safe_filename(item.name)[:50]
        if not base:
            base = f"item_{index+1:03d}"

        ext = ".txt"
        if item.content_type == "json":
            ext = ".json"
        elif item.content_type == "html":
            ext = ".html"
        elif item.content_type == "markdown":
            ext = ".md"

        fname = f"{index+1:03d}_{base}{ext}"
        return fname

    def _safe_filename(self, name: str) -> str:
        """将字符串转为安全的文件名。"""
        import re
        safe = re.sub(r'[\\/:*?"<>|]', '_', name)
        safe = re.sub(r'\s+', '_', safe)
        safe = safe.strip('._')
        return safe[:80] if safe else "unnamed"

    def _generate_index(self, zip_name: str,
                        categories: Dict[str, List[PackagedItem]],
                        file_list: List[str]) -> str:
        """生成索引 README.md。"""
        lines = [
            f"# 📦 {zip_name}\n",
            f"生成时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n",
            f"总条目数: {sum(len(v) for v in categories.values())}",
            f"类别数: {len(categories)}\n",
            "---\n",
            "## 📂 目录结构\n",
        ]

        for cat_name, cat_items in sorted(categories.items()):
            safe_cat = self._safe_filename(cat_name)
            lines.append(f"### {cat_name}/ ({len(cat_items)} 项)")
            for item in cat_items[:30]:
                fname = self._generate_filename(item, 0)
                preview = (item.content or "")[:80].replace('\n', ' ')
                source = f" — [来源]({item.source_url})" if item.source_url else ""
                lines.append(f"- `{safe_cat}/{fname}` — {item.name}{source}")
                if preview:
                    lines.append(f"  > {preview}...")
            lines.append("")

        lines.append("---")
        lines.append(f"*共 {len(file_list)} 个文件*")
        return '\n'.join(lines)

    def _generate_metadata(self, zip_name: str,
                           items: List[PackagedItem],
                           categories: Dict[str, List[PackagedItem]]) -> Dict[str, Any]:
        """生成 metadata JSON（v6.0.0: 含 item_hashes 供增量去重）。"""
        item_hashes = []
        for item in items:
            content = item.content or ""
            item_hashes.append({
                "name": item.name,
                "category": item.category,
                "hash": hashlib.md5(content.encode("utf-8")).hexdigest(),
                "chars": len(content),
            })
        return {
            "package_name": zip_name,
            "version": "6.0",
            "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "total_items": len(items),
            "category_count": len(categories),
            "categories": {
                cat: {
                    "count": len(cat_items),
                    "items": [
                        {
                            "name": item.name,
                            "source_url": item.source_url,
                            "chars": len(item.content or ""),
                        }
                        for item in cat_items
                    ],
                }
                for cat, cat_items in categories.items()
            },
            "item_summary": [
                {"name": item.name, "category": item.category,
                 "source_url": item.source_url, "chars": len(item.content or "")}
                for item in items[:100]
            ],
            "item_hashes": item_hashes,  # v6.0.0: 增量去重依据
        }

    # ---------- v6.0.0 增量打包 ----------

    def package_incremental(self, items: List[Any], previous_zip: str = "",
                            zip_name: str = "", **kwargs) -> Tuple[str, int]:
        """增量打包：基于已有 ZIP 的 metadata.item_hashes 跳过已打包内容。

        Args:
            items: 待打包条目（同 package()）
            previous_zip: 已有 ZIP 路径（读取其 metadata.json 的 hash 集合）
            zip_name: 新 ZIP 文件名
            **kwargs: 透传 package() 参数

        Returns:
            (zip_path, skipped_count) — 新包路径与去重跳过的条数
        """
        existing_hashes: set = set()
        if previous_zip and os.path.isfile(previous_zip):
            try:
                with zipfile.ZipFile(previous_zip) as zf:
                    names = zf.namelist()
                    meta_name = next((n for n in names
                                      if n.endswith("metadata.json")), None)
                    if meta_name:
                        meta = json.loads(zf.read(meta_name).decode("utf-8"))
                        existing_hashes = {
                            h["hash"] for h in meta.get("item_hashes", [])
                            if h.get("hash")
                        }
            except Exception:
                existing_hashes = set()

        packaged = self._normalize_items(items)
        fresh = []
        skipped = 0
        for item in packaged:
            content = item.content or ""
            h = hashlib.md5(content.encode("utf-8")).hexdigest() if content else ""
            if h and h in existing_hashes:
                skipped += 1
                continue
            fresh.append(item)

        if not fresh:
            raise ValueError("所有条目均已打包过（增量去重后无新内容）")

        zip_path = self.package(fresh, zip_name=zip_name, **kwargs)
        return zip_path, skipped

    def _get_dir_size(self, path: Path) -> int:
        """计算目录总大小（字节）。"""
        total = 0
        for f in path.rglob('*'):
            if f.is_file():
                total += f.stat().st_size
        return total

    def _create_zip(self, source_dir: Path, zip_path: str) -> str:
        """创建单个 ZIP 文件（v4.5 修复：中文文件名 UTF-8 + GBK 双编码）。"""
        source_dir_str = str(source_dir)
        with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zf:
            for root, dirs, files in os.walk(source_dir_str):
                for fname in files:
                    fpath = os.path.join(root, fname)
                    arcname = os.path.relpath(fpath, source_dir_str)
                    _write_zip_utf8(zf, fpath, arcname)

        size_mb = os.path.getsize(zip_path) / (1024 * 1024)
        print(f"✅ ZIP 已创建: {zip_path} ({size_mb:.2f} MB)")
        return zip_path

    def _create_split_zip(self, source_dir: Path, zip_name: str,
                          total_size: int) -> str:
        """创建分卷 ZIP 文件。"""
        source_dir_str = str(source_dir)
        vol_size = self.MAX_VOLUME_SIZE
        vol_num = 0
        current_size = 0
        zf = None
        result_paths = []

        try:
            for root, dirs, files in os.walk(source_dir_str):
                for fname in files:
                    fpath = os.path.join(root, fname)
                    fsize = os.path.getsize(fpath)

                    if zf is None or current_size + fsize > vol_size:
                        if zf:
                            zf.close()
                        vol_num += 1
                        vol_name = f"{zip_name}_part{vol_num:02d}"
                        vol_path = str(self.output_dir / f"{vol_name}.zip")
                        zf = zipfile.ZipFile(vol_path, 'w', zipfile.ZIP_DEFLATED)
                        result_paths.append(vol_path)
                        current_size = 0

                    arcname = os.path.relpath(fpath, source_dir_str)
                    zf.write(fpath, arcname)
                    current_size += fsize

            if zf:
                zf.close()

            # 返回第一个分卷路径作为主路径
            for p in result_paths:
                size_mb = os.path.getsize(p) / (1024 * 1024)
                print(f"✅ 分卷 ZIP: {p} ({size_mb:.2f} MB)")
            return result_paths[0] if result_paths else ""

        except Exception as e:
            if zf:
                try:
                    zf.close()
                except Exception:
                    pass
            raise e


# ==================== 便捷函数 ====================

_packager = CrawlPackager()


def batch_crawl_and_package(names: str = "", institution_type: str = "",
                            zip_name: str = "") -> str:
    """批量爬取 + 自动打包 ZIP。"""
    return _packager.package_batch_crawl(
        names=names, institution_type=institution_type, zip_name=zip_name
    )


def package_crawl_results(items: List[Any], zip_name: str = "",
                          output_dir: str = "") -> str:
    """打包爬取结果为 ZIP。"""
    return _packager.package(items, zip_name=zip_name, output_dir=output_dir)


def package_files(file_paths: List[str], zip_name: str = "",
                  output_dir: str = "") -> str:
    """打包文件列表为 ZIP。"""
    return _packager.package_from_files(
        file_paths=file_paths, zip_name=zip_name, output_dir=output_dir
    )


# ==================== v4.5 ZIP UTF-8 修复 + 图片打包 ====================

def _write_zip_utf8(zf: zipfile.ZipFile, fpath: str, arcname: str) -> None:
    """写入 ZIP 条目时双编码（UTF-8 + GBK extra 字段），避免中文文件名乱码。

    zipfile 内部默认 UTF-8 flag（Python 3），但部分 Windows 工具解压时按 GBK 解码。
    通过 extra 字段追加 Info-ZIP Unicode Path，跨平台解压都正常。
    """
    import zlib
    with open(fpath, "rb") as f:
        data = f.read()
    _write_zip_data_utf8(zf, arcname, data)


def _write_zip_data_utf8(zf: zipfile.ZipFile, arcname: str, data: bytes) -> None:
    """写入 ZIP 条目（直接传入 data），带 UTF-8 extra 字段。"""
    import zlib
    info = zipfile.ZipInfo(arcname)
    info.compress_type = zipfile.ZIP_DEFLATED
    if any(ord(c) > 127 for c in arcname):
        try:
            utf8_bytes = arcname.encode("utf-8")
            crc = zlib.crc32(utf8_bytes) & 0xFFFFFFFF
            payload = b"\x01" + crc.to_bytes(4, "little") + utf8_bytes
            info.extra = b"\x75\x70" + len(payload).to_bytes(2, "little") + payload
        except UnicodeEncodeError:
            pass
    zf.writestr(info, data)


def package_with_images(items: List[Any], zip_name: str = "",
                        output_dir: str = "",
                        media_subdir: str = "media") -> str:
    """v4.5 新增：打包爬取结果 + 关联图片。

    Args:
        items: 爬取结果列表，每个 item 可含 {"image_map": {orig_url: local_path}}
        zip_name: ZIP 名
        output_dir: 输出目录
        media_subdir: ZIP 内图片子目录名

    Returns:
        ZIP 路径
    """
    packager = CrawlPackager(output_dir=output_dir) if output_dir else _packager

    # 先写所有 content 到临时目录
    import tempfile
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        media_dir = tmp / media_subdir
        media_dir.mkdir(exist_ok=True)
        # 收集所有图片
        for idx, item in enumerate(items):
            img_map = (item.get("image_map") if isinstance(item, dict) else None) or {}
            for orig_url, local_path in img_map.items():
                if not local_path:
                    continue
                src = Path(local_path)
                if not src.exists():
                    continue
                # 拷贝到 media_dir
                dst = media_dir / src.name
                if not dst.exists():
                    try:
                        import shutil as _sh
                        _sh.copy2(str(src), str(dst))
                    except Exception:
                        pass

        # 调用原有 package 写 content 文件
        content_zip = packager.package(items, zip_name=zip_name + "_content")
        # 合并：解压 content_zip + media 到新 zip
        if zip_name:
            final_name = zip_name if zip_name.endswith(".zip") else zip_name + ".zip"
        else:
            final_name = "package_with_images.zip"
        out_zip = Path(packager.output_dir) / final_name

        with zipfile.ZipFile(str(out_zip), "w", zipfile.ZIP_DEFLATED) as zf:
            # 1) 写 media
            for f in media_dir.rglob("*"):
                if f.is_file():
                    arc = f"{media_subdir}/{f.relative_to(media_dir)}"
                    _write_zip_utf8(zf, str(f), arc)
            # 2) 写 content（从原 zip 抽出重写）
            if Path(content_zip).exists():
                with zipfile.ZipFile(content_zip, "r") as src_zf:
                    for info in src_zf.infolist():
                        data = src_zf.read(info.filename)
                        # 用 writestr 重新写入（带 UTF-8 extra）
                        _write_zip_data_utf8(zf, info.filename, data)
                try:
                    Path(content_zip).unlink()
                except Exception:
                    pass

        return str(out_zip)


def _decode_zip_filename(name: str) -> str:
    """从 ZIP extra 字段恢复 UTF-8 文件名（如果存在）。"""
    return name  # 简化版：直接返回


def set_zip_encoding(zip_path: str) -> str:
    """为已有 ZIP 重新写入 UTF-8 编码（修复中文乱码）。

    Args:
        zip_path: 已有 ZIP 路径

    Returns:
        修复后的新 ZIP 路径（原文件加 .utf8.zip 后缀）
    """
    src = Path(zip_path)
    if not src.exists():
        return ""
    dst = src.with_suffix(".utf8.zip")
    with zipfile.ZipFile(src, "r") as src_zf, \
            zipfile.ZipFile(str(dst), "w", zipfile.ZIP_DEFLATED) as dst_zf:
        for info in src_zf.infolist():
            data = src_zf.read(info.filename)
            _write_zip_data_utf8(dst_zf, info.filename, data)
    return str(dst)


# ==================== 增强 BatchInstitutionCrawler ====================

def enhance_batch_crawler():
    """为 BatchInstitutionCrawler 添加 crawl_and_package 方法。"""
    try:
        from batch_institution_crawler import BatchInstitutionCrawler

        def _crawl_and_package(self, names: str = "", institution_type: str = "",
                               zip_name: str = "") -> str:
            """批量爬取 + 自动打包 ZIP。"""
            items = []
            if names:
                inst_list = [{"name": n.strip()} for n in names.split(",")]
                results = self.crawl_by_names(inst_list)
                items = [{"name": r.get("name", ""), "content": r.get("content", ""),
                         "success": r.get("success", False)}
                        for r in results]
            elif institution_type:
                results = self.crawl_by_type(institution_type)
                items = [{"name": r.get("name", ""), "content": r.get("content", ""),
                         "success": r.get("success", False)}
                        for r in results]

            return _packager.package(items, zip_name=zip_name)

        BatchInstitutionCrawler.crawl_and_package = _crawl_and_package
        return True
    except ImportError:
        return False


# ==================== v12.0.0 交付包（清单校验） ====================

DELIVERY_SCHEMA_VERSION = "1.0"


def _skill_version() -> str:
    try:
        from scripts import __version__ as ver  # type: ignore
        return str(ver)
    except Exception:
        meta = Path(__file__).resolve().parent.parent / "_meta.json"
        try:
            return str(json.loads(meta.read_text(encoding="utf-8")).get("version", ""))
        except Exception:
            return "unknown"


def build_delivery_package(files: List[Any], zip_name: str = "",
                           manifest_payload: Optional[Dict[str, Any]] = None,
                           output_dir: str = "",
                           data_subdir: str = "data",
                           failed: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
    """生成可自证的交付 ZIP：数据文件 + manifest.json（含 sha256）+ README.md。

    与 package() 的区别：本函数打包"已经落盘的文件"，并在 manifest 里逐条记录
    sha256 与字节数，接收方用 verify_delivery_package() 就能判断包是否被截断、
    替换或漏文件——这是"打包下载"这条链路的自证环节。

    Args:
        files: 待打包文件路径列表；不存在的跳过并计入 manifest['skipped']
        zip_name: ZIP 名（不含扩展名），留空按时间戳生成
        manifest_payload: 业务摘要（主题/条数/质量分等），原样写入 manifest
        output_dir: 输出目录，默认 data/packages
        data_subdir: ZIP 内数据子目录名
        failed: 上游失败清单（[{url/name, reason}]），写进 manifest 供重下排查

    Returns:
        {"zip_path", "zip_size_kb", "file_count", "total_bytes",
         "manifest_sha256", "skipped"}
    """
    paths: List[Path] = []
    skipped: List[str] = []
    for raw in files or []:
        p = Path(str(raw))
        if p.is_file():
            paths.append(p)
        else:
            skipped.append(str(raw))

    if not paths:
        raise ValueError("没有可打包的文件")

    out_dir = Path(output_dir) if output_dir else PACKAGES_DIR
    out_dir.mkdir(parents=True, exist_ok=True)
    if not zip_name:
        zip_name = f"delivery_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    zip_path = out_dir / f"{zip_name}.zip"

    # 同名文件用 _N 后缀去重，避免后写入的静默覆盖前面的
    entries: List[Tuple[Path, str]] = []
    taken: set = set()
    for p in paths:
        stem, ext = p.stem, p.suffix
        cand = p.name
        n = 1
        while cand in taken:
            n += 1
            cand = f"{stem}_{n}{ext}"
        taken.add(cand)
        entries.append((p, f"{data_subdir}/{cand}"))

    file_records = []
    for p, arc in entries:
        raw = p.read_bytes()
        file_records.append({
            "arcname": arc,
            "name": p.name,
            "sha256": hashlib.sha256(raw).hexdigest(),
            "bytes": len(raw),
        })

    total_bytes = sum(r["bytes"] for r in file_records)
    manifest = {
        "schema_version": DELIVERY_SCHEMA_VERSION,
        "package_name": f"{zip_name}.zip",
        "generator": f"cn-financial-scraper {_skill_version()}",
        "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "file_count": len(file_records),
        "total_bytes": total_bytes,
        "files": file_records,
        "payload": manifest_payload or {},
        "failed": failed or [],
        "skipped": skipped,
    }
    manifest_bytes = json.dumps(manifest, ensure_ascii=False,
                                indent=2).encode("utf-8")

    readme = _render_delivery_readme(zip_name, manifest)
    readme_bytes = readme.encode("utf-8")

    with zipfile.ZipFile(str(zip_path), "w", zipfile.ZIP_DEFLATED) as zf:
        for p, arc in entries:
            _write_zip_data_utf8(zf, arc, p.read_bytes())
        _write_zip_data_utf8(zf, "manifest.json", manifest_bytes)
        _write_zip_data_utf8(zf, "README.md", readme_bytes)

    return {
        "zip_path": str(zip_path),
        "zip_size_kb": round(zip_path.stat().st_size / 1024.0, 1),
        "file_count": len(file_records),
        "total_bytes": total_bytes,
        "manifest_sha256": hashlib.sha256(manifest_bytes).hexdigest(),
        "skipped": skipped,
    }


def _render_delivery_readme(zip_name: str, manifest: Dict[str, Any]) -> str:
    """交付包内的 README.md：人读的清单摘要 + 自检方法。"""
    payload = manifest.get("payload") or {}
    lines = [f"# 交付包 · {zip_name}", "",
             f"- 生成时间：{manifest.get('created_at', '')}",
             f"- 生成来源：{manifest.get('generator', '')}",
             f"- 文件数：{manifest.get('file_count', 0)}，"
             f"解压后 {manifest.get('total_bytes', 0)} 字节"]
    for key in ("topic", "intent", "records", "quality_score", "generated_at"):
        if payload.get(key) not in (None, "", []):
            lines.append(f"- {key}：{payload[key]}")
    extra = {k: v for k, v in payload.items()
             if k not in ("topic", "intent", "records", "quality_score",
                          "generated_at")}
    for k, v in extra.items():
        lines.append(f"- {k}：{v}")

    lines += ["", "## 文件清单", "",
              "| 包内路径 | 字节 | sha256（前 12 位） |", "| --- | --- | --- |"]
    for rec in manifest.get("files", []):
        lines.append(f"| `{rec['arcname']}` | {rec['bytes']} | "
                     f"`{rec['sha256'][:12]}` |")

    failed = manifest.get("failed") or []
    if failed:
        lines += ["", "## 本次未取到的部分", ""]
        for item in failed[:20]:
            if isinstance(item, dict):
                lines.append(f"- {item.get('name') or item.get('url') or item}: "
                             f"{item.get('reason', '')}")
            else:
                lines.append(f"- {item}")
    skipped = manifest.get("skipped") or []
    if skipped:
        lines += ["", "## 打包时跳过的路径（源文件不存在）", ""]
        lines += [f"- `{s}`" for s in skipped[:20]]

    lines += ["", "## 校验完整性", "",
              "```bash",
              "python -c \"import sys; sys.path.insert(0, 'scripts'); "
              "from crawl_packager import verify_delivery_package; "
              "print(verify_delivery_package('包名.zip'))\"",
              "```",
              "校验逐条比对 manifest.json 里的 sha256，"
              "任何文件被改动、缺失或多出都会显式报出。", ""]
    return "\n".join(lines)


def verify_delivery_package(zip_path: str) -> Dict[str, Any]:
    """按 manifest.json 校验交付包完整性。

    Returns:
        {"ok", "checked", "mismatched", "missing", "extra",
         "package_name", "created_at", "error"}
        ok=True 当且仅当 manifest 里的每个文件都在包内且 sha256 一致、且无多余条目。
    """
    out: Dict[str, Any] = {"ok": False, "checked": 0, "mismatched": [],
                           "missing": [], "extra": [], "package_name": "",
                           "created_at": "", "error": ""}
    src = Path(zip_path)
    if not src.is_file():
        out["error"] = "E_ZIP_MISSING"
        out["reason"] = f"交付包不存在：{zip_path}"
        return out
    try:
        with zipfile.ZipFile(str(src)) as zf:
            names = [n for n in zf.namelist() if not n.endswith("/")]
            manifest_name = next((n for n in names
                                 if n == "manifest.json"
                                 or n.endswith("/manifest.json")), None)
            if not manifest_name:
                out["error"] = "E_MANIFEST_MISSING"
                out["reason"] = "包内没有 manifest.json，不是交付包或未打包清单"
                return out
            manifest = json.loads(zf.read(manifest_name).decode("utf-8"))
            body_prefix = manifest_name[:-len("manifest.json")]
            readme_arc = f"{body_prefix}README.md"
            data_names = {n for n in names
                          if n != manifest_name and n != readme_arc}
            expected = []
            for rec in manifest.get("files", []):
                arc = rec.get("arcname") or rec.get("name") or ""
                expected.append((f"{body_prefix}{arc}", rec))
            present = set(data_names)
            for arc, rec in expected:
                if arc not in present:
                    out["missing"].append(arc)
                    continue
                raw = zf.read(arc)
                out["checked"] += 1
                if hashlib.sha256(raw).hexdigest() != rec.get("sha256"):
                    out["mismatched"].append(arc)
            expected_arcs = {arc for arc, _ in expected}
            out["extra"] = sorted(data_names - expected_arcs)
            out["package_name"] = manifest.get("package_name", "")
            out["created_at"] = manifest.get("created_at", "")
    except (OSError, ValueError, zipfile.BadZipFile) as exc:
        out["error"] = "E_ZIP_UNREADABLE"
        out["reason"] = str(exc)
        return out

    out["ok"] = not (out["mismatched"] or out["missing"] or out["extra"])
    return out


def package_remote_files(urls: List[str], zip_name: str = "",
                         output_dir: str = "", work_dir: str = "",
                         retries: int = 2, timeout: int = 20,
                         downloader: Optional[Callable[..., Optional[str]]] = None,
                         progress_callback: Optional[Callable[[int, int, str], None]] = None
                         ) -> Dict[str, Any]:
    """URL 清单 → 逐条下载（失败自动重下）→ 交付包（含 sha256 清单与失败清单）。

    单条失败不会中断整批；最终失败项写进 manifest.failed，含 url/原因/重试次数，
    调用方拿这份清单即可只重下失败的那几条。

    Args:
        urls: 链接列表，元素可写 "url|文件名" 指定落盘名
        zip_name / output_dir: 透传 build_delivery_package
        work_dir: 下载落盘目录；留空用 data/temp_packages 下的临时目录并在打包后清理
        retries: 单条失败重试次数（默认 2，退避 0.5s/1.0s）
        timeout: 单条下载超时（秒）
        downloader: 注入 (url, save_dir=, filename=, timeout=, overwrite=)->Optional[str]，
                    默认 http_utils.download_file；离线测试传桩函数即可
        progress_callback: (done, total, url)

    Returns:
        build_delivery_package 的结果 + {requested, downloaded, retried, failed}
    """
    import time as _time

    cleaned = [str(u).strip() for u in (urls or []) if str(u).strip()]
    if not cleaned:
        raise ValueError("urls 为空，没有可下载的链接")

    fetch = downloader
    if fetch is None:
        try:
            from http_utils import download_file as fetch  # type: ignore
        except Exception:
            try:
                from scripts.http_utils import download_file as fetch  # type: ignore
            except Exception as exc:
                raise ValueError(f"无法导入 http_utils.download_file（{exc}），"
                                 f"请显式传 downloader")

    TEMP_DIR.mkdir(parents=True, exist_ok=True)
    own_tmp = not work_dir
    base = Path(work_dir) if work_dir else Path(tempfile.mkdtemp(
        prefix=f"dl_{zip_name or 'remote'}_", dir=str(TEMP_DIR)))
    base.mkdir(parents=True, exist_ok=True)

    downloaded: List[str] = []
    failed: List[Dict[str, Any]] = []
    retried = 0
    try:
        for i, raw in enumerate(cleaned, 1):
            url, _, fname = raw.partition("|")
            path: Optional[str] = None
            reason = ""
            for attempt in range(retries + 1):
                try:
                    path = fetch(url, save_dir=str(base),
                                 filename=(fname.strip() or None),
                                 timeout=timeout, overwrite=attempt > 0)
                except Exception as exc:
                    path, reason = None, str(exc)
                if path:
                    break
                reason = reason or "下载返回空（可能被拦截或链接失效）"
                if attempt < retries:
                    retried += 1
                    _time.sleep(0.5 * (attempt + 1))
                    reason = ""
            if path and Path(path).is_file():
                downloaded.append(str(path))
                failed_note = None
            else:
                failed.append({"url": url, "name": fname.strip() or Path(url).name,
                               "reason": reason, "retries": retries})
                failed_note = reason
            if progress_callback is not None:
                try:
                    progress_callback(i, len(cleaned),
                                      url if not failed_note else f"{url} ✗ {failed_note}")
                except Exception:
                    pass

        result = build_delivery_package(
            downloaded, zip_name=zip_name, output_dir=output_dir,
            manifest_payload={"source": "package_remote_files",
                              "requested": len(cleaned),
                              "downloaded": len(downloaded)},
            failed=failed)
        result.update({"requested": len(cleaned), "downloaded": len(downloaded),
                       "retried": retried, "failed": failed})
        return result
    finally:
        if own_tmp:
            shutil.rmtree(str(base), ignore_errors=True)


# ==================== CLI 入口 ====================

if __name__ == "__main__":
    import sys

    if len(sys.argv) < 2:
        print("用法: python crawl_packager.py <命令> [参数]")
        print("命令:")
        print("  package <names或type> [zip名]  — 批量爬取+打包")
        print("  files <文件1,文件2,...> [zip名] — 打包指定文件")
        print("  deliver <文件1,文件2,...> [zip名] — 生成交付包(含 sha256 清单)")
        print("  verify <zip路径>               — 校验交付包清单")
        print()
        print("示例:")
        print("  python crawl_packager.py package \"华夏基金,易方达基金\" my_package")
        print("  python crawl_packager.py package 基金管理公司")
        print("  python crawl_packager.py files \"data/report1.pdf,data/report2.pdf\"")
        sys.exit(1)

    cmd = sys.argv[1]

    if cmd == "package":
        target = sys.argv[2] if len(sys.argv) > 2 else ""
        zip_name = sys.argv[3] if len(sys.argv) > 3 else ""

        if not target:
            print("请提供机构名称(逗号分隔)或机构类型")
            sys.exit(1)

        # 判断是名称列表还是类型
        if any(c in target for c in "基金证券银行保险信托期货"):
            result = _packager.package_batch_crawl(
                institution_type=target, zip_name=zip_name
            )
        else:
            result = _packager.package_batch_crawl(
                names=target, zip_name=zip_name
            )

        print(f"📦 打包完成: {result}")

    elif cmd == "files":
        if len(sys.argv) < 3:
            print("请提供文件路径(逗号分隔)")
            sys.exit(1)

        paths = [p.strip() for p in sys.argv[2].split(",") if p.strip()]
        zip_name = sys.argv[3] if len(sys.argv) > 3 else ""

        result = _packager.package_from_files(paths, zip_name=zip_name)
        print(f"📦 打包完成: {result}")

    elif cmd == "deliver":
        if len(sys.argv) < 3:
            print("请提供文件路径(逗号分隔)")
            sys.exit(1)
        paths = [p.strip() for p in sys.argv[2].split(",") if p.strip()]
        zip_name = sys.argv[3] if len(sys.argv) > 3 else ""
        out = build_delivery_package(paths, zip_name=zip_name)
        print(f"📦 交付包: {out['zip_path']}（{out['file_count']} 个文件 / "
              f"{out['zip_size_kb']} KB）")
        if out["skipped"]:
            print(f"⚠️ 跳过不存在的路径: {out['skipped']}")

    elif cmd == "verify":
        if len(sys.argv) < 3:
            print("请提供交付包 zip 路径")
            sys.exit(1)
        report = verify_delivery_package(sys.argv[2])
        if report["error"]:
            print(f"❌ {report['error']}: {report.get('reason', '')}")
            sys.exit(2)
        print(f"{'✅ 校验通过' if report['ok'] else '❌ 校验不通过'} "
              f"{report['package_name']}（比对 {report['checked']} 个文件）")
        for label, items in (("sha256 不符", report["mismatched"]),
                             ("缺失", report["missing"]),
                             ("清单外多余", report["extra"])):
            if items:
                print(f"  - {label}: {', '.join(items)}")
        sys.exit(0 if report["ok"] else 2)

    else:
        print(f"未知命令: {cmd}")
