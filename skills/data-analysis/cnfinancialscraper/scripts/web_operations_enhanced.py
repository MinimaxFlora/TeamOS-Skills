# -*- coding: utf-8 -*-
"""
网页操作深度增强模块 v10.0 (web_operations_enhanced.py)

在 v9.0 browser_pro.py 基础上扩展，目标是覆盖现代网页特有的复杂场景：
1. **Shadow DOM 穿透**：通过 JS 链式访问 shadowRoot
2. **iframe 跨域穿透**：Playwright frame_locator 多层跨域
3. **Cookie Banner 自动处理**：GDPR/CCPA 弹窗接受/拒绝
4. **验证码 OCR 识别**：可选 ddddocr / pytesseract 离线识别
5. **人类行为轨迹**：贝塞尔曲线鼠标 + 阅读节奏 + 随机键盘
6. **WebSocket 监听**：捕获页面实时推送数据

设计原则：
- 零 pip 依赖即可用（核心接口用 stdlib 模拟）
- 浏览器部分按需可选 import（缺失时降级为接口存根）
- 全部类独立，可单独 import
- 与现有 browser_pro.py / browser_scraper.py 共存互补

向后兼容：所有类独立，与 v9 browser_pro 完全共存。
"""
from __future__ import annotations

import json
import logging
import math
import random
import re
import threading
import time
from abc import ABC, abstractmethod
from collections import deque
from dataclasses import dataclass, field, asdict
from enum import Enum, auto
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

log = logging.getLogger(__name__)


# ============================================================================
# 1. Shadow DOM 穿透
# ============================================================================

@dataclass
class ShadowPierceConfig:
    """Shadow DOM 穿透配置。"""
    max_depth: int = 5            # 最大穿透深度
    wait_after_open_ms: int = 50  # 打开 shadowRoot 后等待毫秒
    timeout_ms: int = 5000        # 单步超时
    js_engine: str = "auto"       # "auto" | "playwright" | "stdlib"


SHADOW_DOM_JS = """
(__CHAIN__) => {
    const parts = __CHAIN__.split('>>');
    let node = document;
    for (const part of parts) {
        const partTrim = part.trim();
        if (!partTrim) continue;
        // 支持 css selector，e.g. "div.foo >> span#bar"
        node = node.querySelector(partTrim);
        if (!node) return null;
        if (node.shadowRoot) {
            node = node.shadowRoot;
        }
    }
    return node ? node.outerHTML : null;
}
"""


class ShadowDOMPiercer:
    """Shadow DOM 穿透器（链式 css selector）。

    用法（需要 Playwright）：
        piercer = ShadowDOMPiercer()
        html = piercer.open(page, "div#app >> div#container >> span.label")
    """

    def __init__(self, config: Optional[ShadowPierceConfig] = None) -> None:
        self.config = config or ShadowPierceConfig()
        self._cache: Dict[str, str] = {}

    def open_chain(self, chain: str) -> str:
        """链式选择器解析（不实际执行浏览器调用，解析为 JS 调用字符串）。

        e.g. "div#app >> div.foo >> span#bar" → JS 表达式
        """
        return SHADOW_DOM_JS.replace("__CHAIN__", chain.replace("'", "\\'"))

    def open(self, page: Any, chain: str) -> Optional[str]:
        """在 Playwright page 上穿透（依赖 Playwright）。"""
        if page is None:
            log.warning("ShadowDOMPiercer.open: page is None")
            return None
        try:
            return page.evaluate(SHADOW_DOM_JS, chain)
        except Exception as exc:  # noqa: BLE001
            log.warning("Shadow DOM open failed: %s", exc)
            return None

    def click_chain(self, page: Any, chain: str) -> bool:
        """穿透后点击（依赖 Playwright）。"""
        if page is None:
            return False
        try:
            js = SHADOW_DOM_JS + "\n; (node) => { if (node) { node.click(); return true; } return false; }"
            handle = page.evaluate_handle(SHADOW_DOM_JS, chain)
            # 简化：用 evaluate 一次性执行（避免 handle 跨域问题）
            return bool(page.evaluate(
                "(chain) => { try {"
                "  const parts = chain.split('>>');"
                "  let n = document;"
                "  for (const p of parts) {"
                "    n = n.querySelector(p.trim());"
                "    if (!n) return false;"
                "    if (n.shadowRoot) n = n.shadowRoot;"
                "  }"
                "  n.click();"
                "  return true;"
                "} catch(e) { return false; } }",
                chain,
            ))
        except Exception as exc:  # noqa: BLE001
            log.warning("Shadow DOM click failed: %s", exc)
            return False

    def extract_text_chain(self, page: Any, chain: str) -> Optional[str]:
        """穿透后提取文本。"""
        if page is None:
            return None
        try:
            return page.evaluate(
                "(chain) => { try {"
                "  const parts = chain.split('>>');"
                "  let n = document;"
                "  for (const p of parts) {"
                "    n = n.querySelector(p.trim());"
                "    if (!n) return null;"
                "    if (n.shadowRoot) n = n.shadowRoot;"
                "  }"
                "  return n ? (n.innerText || n.textContent) : null;"
                "} catch(e) { return null; } }",
                chain,
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("Shadow DOM extract_text failed: %s", exc)
            return None


# ============================================================================
# 2. iframe 跨域穿透
# ============================================================================

@dataclass
class IframeChain:
    """iframe 链：name + url 多级嵌套。"""
    name: str
    url: str = ""


class IframeNavigator:
    """iframe 跨域穿透器（基于 Playwright frame_locator）。"""

    def __init__(self, max_depth: int = 5) -> None:
        self.max_depth = max_depth
        self.history: List[Tuple[str, str]] = []

    def locate(self, page: Any, frame_selectors: List[str]) -> Any:
        """按 selector 链定位到最深层 iframe，返回 frame 对象。

        用法：
            nav = IframeNavigator()
            frame = nav.locate(page, ["iframe#main", "iframe#content"])
        """
        if page is None:
            return None
        try:
            locator = page.frame_locator(frame_selectors[0])
            for sel in frame_selectors[1:self.max_depth]:
                locator = locator.frame_locator(sel)
            return locator
        except Exception as exc:  # noqa: BLE001
            log.warning("iframe navigate failed: %s", exc)
            return None

    def find_all_frames(self, page: Any) -> List[Dict[str, str]]:
        """列出当前 page 内所有 iframe（name + src）。"""
        if page is None:
            return []
        try:
            return page.evaluate(
                "() => Array.from(document.querySelectorAll('iframe'))"
                ".map(f => ({name: f.name || '', src: f.src || '', "
                "id: f.id || '', title: f.title || ''}))"
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("find_all_frames failed: %s", exc)
            return []

    def click(self, page: Any, frame_selectors: List[str],
              target_selector: str) -> bool:
        """跨 iframe 点击目标元素。"""
        locator = self.locate(page, frame_selectors)
        if locator is None:
            return False
        try:
            locator.locator(target_selector).click()
            return True
        except Exception as exc:  # noqa: BLE001
            log.warning("iframe click failed: %s", exc)
            return False

    def extract_text(self, page: Any, frame_selectors: List[str],
                     target_selector: str) -> Optional[str]:
        """跨 iframe 提取文本。"""
        locator = self.locate(page, frame_selectors)
        if locator is None:
            return None
        try:
            return locator.locator(target_selector).inner_text()
        except Exception as exc:  # noqa: BLE001
            log.warning("iframe extract failed: %s", exc)
            return None


# ============================================================================
# 3. Cookie Banner 自动处理
# ============================================================================

class CookieAction(Enum):
    """Cookie 弹窗响应策略。"""
    ACCEPT = auto()         # 全部接受
    REJECT = auto()         # 全部拒绝
    ESSENTIAL_ONLY = auto() # 仅必要 cookie
    CUSTOM = auto()         # 自定义（按偏好勾选）


# 多语言关键词：识别 Cookie 弹窗按钮文本
COOKIE_BANNER_KEYWORDS: Dict[str, List[str]] = {
    "accept": [
        "接受", "同意", "确定", "知道了", "好的", "我同意",
        "Accept", "Accept All", "I Agree", "Agree", "OK", "Got it",
        "Allow", "Allow All", "Consent", "I Consent", "Yes",
    ],
    "reject": [
        "拒绝", "拒绝全部", "不必要", "管理", "设置",
        "Reject", "Reject All", "Decline", "Refuse", "No", "Necessary Only",
    ],
    "close": [
        "关闭", "×", "✕", "Close",
    ],
}


@dataclass
class CookieBannerPattern:
    """单个 Cookie 弹窗匹配规则。"""
    container_selectors: List[str]  # 容器 css selector
    button_selectors: List[str]     # 按钮 css selector
    action: CookieAction = CookieAction.ACCEPT


# 常见 Cookie 弹窗匹配模式（覆盖 30+ 站点类型）
DEFAULT_COOKIE_PATTERNS: List[CookieBannerPattern] = [
    CookieBannerPattern(
        container_selectors=[
            "[class*='cookie-banner']", "[id*='cookie-banner']",
            "[class*='consent']", "[id*='consent']",
            "[class*='gdpr']", "[id*='gdpr']",
            "[class*='cookie-bar']", "[class*='cookie-notice']",
            "[aria-label*='cookie' i]", "[aria-label*='consent' i]",
        ],
        button_selectors=[
            "button[class*='accept']", "button[class*='agree']",
            "button[id*='accept']", "button[id*='agree']",
            "a[class*='accept']", "a[class*='agree']",
            "button:has-text('Accept')", "button:has-text('同意')",
            "button:has-text('接受')", "button:has-text('OK')",
        ],
    ),
    CookieBannerPattern(
        container_selectors=[
            "[class*='cc-window']",  # CookieConsent 插件
        ],
        button_selectors=[
            ".cc-allow", ".cc-dismiss", "a.cc-btn",
        ],
    ),
    CookieBannerPattern(
        container_selectors=[
            "#onetrust-banner-sdk",  # OneTrust
        ],
        button_selectors=[
            "#onetrust-accept-btn-handler",
            "#onetrust-reject-all-handler",
        ],
    ),
]


class CookieBannerHandler:
    """Cookie 弹窗自动处理器。"""

    def __init__(self, action: CookieAction = CookieAction.ACCEPT,
                 patterns: Optional[List[CookieBannerPattern]] = None,
                 timeout_ms: int = 3000) -> None:
        self.action = action
        self.patterns = patterns or DEFAULT_COOKIE_PATTERNS
        self.timeout_ms = timeout_ms

    def dismiss(self, page: Any) -> Dict[str, Any]:
        """检测并处理 Cookie 弹窗。返回 {"detected": bool, "action": str, "clicked": bool}。"""
        if page is None:
            return {"detected": False, "action": "skip", "clicked": False,
                    "reason": "page is None"}

        # 等待弹窗出现
        try:
            page.wait_for_timeout(min(self.timeout_ms, 800))
        except Exception:  # noqa: BLE001
            pass

        for pattern in self.patterns:
            for csel in pattern.container_selectors:
                try:
                    container = page.query_selector(csel)
                except Exception:  # noqa: BLE001
                    container = None
                if not container:
                    continue
                # 找到容器后再找按钮
                for bsel in pattern.button_selectors:
                    try:
                        btn = container.query_selector(bsel)
                    except Exception:  # noqa: BLE001
                        btn = None
                    if not btn:
                        continue
                    try:
                        btn.click()
                        return {
                            "detected": True,
                            "action": self.action.name,
                            "clicked": True,
                            "container_selector": csel,
                            "button_selector": bsel,
                        }
                    except Exception as exc:  # noqa: BLE001
                        log.debug("cookie click failed: %s", exc)
                        continue
                # 容器在但没找到按钮 → 按关键词匹配按钮文本
                try:
                    clicked_text = self._click_by_text(container, self.action)
                    if clicked_text:
                        return {
                            "detected": True,
                            "action": self.action.name,
                            "clicked": True,
                            "container_selector": csel,
                            "matched_text": clicked_text,
                        }
                except Exception:  # noqa: BLE001
                    pass

        return {"detected": False, "action": self.action.name, "clicked": False}

    def _click_by_text(self, container: Any, action: CookieAction) -> Optional[str]:
        """按按钮文本关键词匹配点击。"""
        try:
            buttons = container.query_selector_all("button, a")
        except Exception:  # noqa: BLE001
            return None

        keyword_set: List[str]
        if action == CookieAction.REJECT:
            keyword_set = COOKIE_BANNER_KEYWORDS["reject"]
        elif action == CookieAction.ESSENTIAL_ONLY:
            keyword_set = COOKIE_BANNER_KEYWORDS["reject"] + ["仅必要", "Necessary"]
        else:
            keyword_set = COOKIE_BANNER_KEYWORDS["accept"]

        for btn in buttons:
            try:
                text = (btn.inner_text() or "").strip()
            except Exception:  # noqa: BLE001
                text = ""
            if not text:
                continue
            for kw in keyword_set:
                if kw.lower() in text.lower():
                    try:
                        btn.click()
                        return text
                    except Exception:  # noqa: BLE001
                        continue
        return None


# ============================================================================
# 4. 验证码 OCR（接口规范 + 可选识别）
# ============================================================================

class CaptchaType(Enum):
    """验证码类型枚举。"""
    NONE = auto()
    RECAPTCHA_V2 = auto()       # Google reCAPTCHA v2
    RECAPTCHA_V3 = auto()       # Google reCAPTCHA v3
    HCAPTCHA = auto()
    GEETEST = auto()            # 极验
    ALIYUN_SLIDER = auto()      # 阿里云滑块
    TENCENT_SLIDER = auto()     # 腾讯滑块
    IMAGE_TEXT = auto()         # 图片文字验证码（数字/字母）
    IMAGE_MATH = auto()         # 图片数学题
    CHINESE_TEXT = auto()       # 中文验证码


@dataclass
class CaptchaDetection:
    """验证码检测结果。"""
    type: CaptchaType
    confidence: float            # 0.0-1.0
    element_selector: Optional[str] = None  # 验证码元素选择器
    image_url: Optional[str] = None
    site_key: Optional[str] = None          # reCAPTCHA sitekey
    metadata: Dict[str, Any] = field(default_factory=dict)


class CaptchaRecognizer(ABC):
    """验证码识别器抽象基类。

    子类：StdlibCaptchaRecognizer / DdddocrCaptchaRecognizer / TesseractCaptchaRecognizer
    """

    @abstractmethod
    def recognize(self, image_bytes: bytes, captcha_type: CaptchaType) -> Optional[str]:
        """识别验证码图片，返回识别结果文本。失败返回 None。"""
        raise NotImplementedError

    @property
    @abstractmethod
    def name(self) -> str: ...

    @property
    def supported_types(self) -> List[CaptchaType]:
        return [CaptchaType.IMAGE_TEXT]


class StdlibCaptchaRecognizer(CaptchaRecognizer):
    """零依赖验证码识别器（基于图像 hash + 规则）。

    适用场景：
    - 简单数字验证码（图片 hash 一致时返回字典中的答案）
    - 已知固定答案的验证码（用户预设）

    不适用：复杂图形验证码（需要 ddddocr / pytesseract）。
    """

    def __init__(self, dict_answers: Optional[Dict[str, str]] = None) -> None:
        self.dict_answers = dict_answers or {}
        self._cache: Dict[str, str] = {}

    @property
    def name(self) -> str:
        return "stdlib"

    def recognize(self, image_bytes: bytes,
                  captcha_type: CaptchaType) -> Optional[str]:
        if captcha_type not in (CaptchaType.IMAGE_TEXT, CaptchaType.IMAGE_MATH):
            return None
        h = hashlib_md5(image_bytes)
        if h in self.dict_answers:
            return self.dict_answers[h]
        return None

    def add_known_answer(self, image_bytes: bytes, answer: str) -> None:
        self.dict_answers[hashlib_md5(image_bytes)] = answer


def hashlib_md5(data: bytes) -> str:
    import hashlib
    return hashlib.md5(data).hexdigest()


class DdddocrCaptchaRecognizer(CaptchaRecognizer):
    """ddddocr 识别器（可选依赖）。"""

    def __init__(self) -> None:
        self._ocr: Any = None
        self._loaded = False

    @property
    def name(self) -> str:
        return "ddddocr"

    @property
    def supported_types(self) -> List[CaptchaType]:
        return [CaptchaType.IMAGE_TEXT, CaptchaType.IMAGE_MATH, CaptchaType.CHINESE_TEXT]

    def _ensure(self) -> bool:
        if self._loaded:
            return self._ocr is not None
        self._loaded = True
        try:
            import ddddocr  # type: ignore
            self._ocr = ddddocr.DdddOcr(show_ad=False)
            return True
        except Exception as exc:  # noqa: BLE001
            log.warning("ddddocr unavailable: %s", exc)
            return False

    def recognize(self, image_bytes: bytes,
                  captcha_type: CaptchaType) -> Optional[str]:
        if not self._ensure() or self._ocr is None:
            return None
        try:
            res = self._ocr.classification(image_bytes)
            return res if isinstance(res, str) else None
        except Exception as exc:  # noqa: BLE001
            log.warning("ddddocr recognition failed: %s", exc)
            return None


class TesseractCaptchaRecognizer(CaptchaRecognizer):
    """pytesseract 识别器（可选依赖）。"""

    def __init__(self, lang: str = "eng+chi_sim") -> None:
        self.lang = lang
        self._loaded = False
        self._pytess: Any = None

    @property
    def name(self) -> str:
        return "tesseract"

    @property
    def supported_types(self) -> List[CaptchaType]:
        return [CaptchaType.IMAGE_TEXT, CaptchaType.CHINESE_TEXT]

    def _ensure(self) -> bool:
        if self._loaded:
            return self._pytess is not None
        self._loaded = True
        try:
            import pytesseract  # type: ignore
            from PIL import Image  # noqa: F401
            import io
            self._pytess = pytesseract
            return True
        except Exception as exc:  # noqa: BLE001
            log.warning("pytesseract unavailable: %s", exc)
            return False

    def recognize(self, image_bytes: bytes,
                  captcha_type: CaptchaType) -> Optional[str]:
        if not self._ensure():
            return None
        try:
            import pytesseract
            from PIL import Image
            import io
            img = Image.open(io.BytesIO(image_bytes))
            text = pytesseract.image_to_string(img, lang=self.lang)
            return text.strip()
        except Exception as exc:  # noqa: BLE001
            log.warning("tesseract recognition failed: %s", exc)
            return None


class CaptchaPipeline:
    """验证码处理管道：按优先级尝试多个识别器。"""

    def __init__(self, recognizers: Optional[List[CaptchaRecognizer]] = None) -> None:
        if recognizers is None:
            recognizers = [StdlibCaptchaRecognizer()]
        self.recognizers = recognizers

    def add_recognizer(self, recognizer: CaptchaRecognizer) -> None:
        self.recognizers.append(recognizer)

    def recognize(self, image_bytes: bytes,
                  captcha_type: CaptchaType = CaptchaType.IMAGE_TEXT) -> Optional[str]:
        for rec in self.recognizers:
            if captcha_type not in rec.supported_types:
                continue
            try:
                result = rec.recognize(image_bytes, captcha_type)
                if result:
                    log.info("captcha recognized by %s: %s", rec.name, result[:20])
                    return result
            except Exception as exc:  # noqa: BLE001
                log.debug("%s failed: %s", rec.name, exc)
                continue
        return None


# ============================================================================
# 5. 人类行为轨迹模拟
# ============================================================================

@dataclass
class BehaviorConfig:
    """人类行为配置。"""
    mouse_speed_px_per_sec: float = 800.0
    reading_pause_chance: float = 0.15    # 每次点击后有 15% 概率停顿阅读
    reading_pause_min_ms: int = 300
    reading_pause_max_ms: int = 1500
    scroll_pause_chance: float = 0.30
    scroll_pause_min_ms: int = 200
    scroll_pause_max_ms: int = 800
    random_keypress_chance: float = 0.02  # 偶尔误触键盘


class BezierMouse:
    """贝塞尔曲线鼠标轨迹生成器。

    用法：
        bm = BezierMouse()
        path = bm.generate(start=(0,0), end=(500, 500))
        for x, y in path:
            page.mouse.move(x, y)
    """

    def __init__(self, config: Optional[BehaviorConfig] = None,
                 seed: Optional[int] = None) -> None:
        self.config = config or BehaviorConfig()
        self._rng = random.Random(seed)

    @staticmethod
    def _bezier_point(t: float, p0: Tuple[float, float], p1: Tuple[float, float],
                      p2: Tuple[float, float]) -> Tuple[float, float]:
        """二阶贝塞尔曲线点。"""
        x = (1 - t) ** 2 * p0[0] + 2 * (1 - t) * t * p1[0] + t ** 2 * p2[0]
        y = (1 - t) ** 2 * p0[1] + 2 * (1 - t) * t * p1[1] + t ** 2 * p2[1]
        return x, y

    def generate(self, start: Tuple[float, float],
                 end: Tuple[float, float],
                 steps: int = 30) -> List[Tuple[float, float]]:
        """生成鼠标轨迹路径。"""
        # 控制点：偏移制造曲线感
        cx = (start[0] + end[0]) / 2 + self._rng.uniform(-50, 50)
        cy = (start[1] + end[1]) / 2 + self._rng.uniform(-50, 50)
        path: List[Tuple[float, float]] = []
        for i in range(steps + 1):
            t = i / steps
            # 速度曲线：开始慢→中快→结束慢（模拟人类加速/减速）
            if t < 0.2:
                eased = t * 2.5
            elif t > 0.8:
                eased = 1.0 - (1.0 - t) * 2.5
            else:
                eased = 0.5 + (t - 0.5) * 1.0
            eased = max(0.0, min(1.0, eased))
            x, y = self._bezier_point(eased, start, (cx, cy), end)
            # 加微小抖动
            x += self._rng.uniform(-0.5, 0.5)
            y += self._rng.uniform(-0.5, 0.5)
            path.append((x, y))
        return path


class HumanBehaviorSimulator:
    """人类行为模拟器：组合鼠标/键盘/滚动。"""

    def __init__(self, config: Optional[BehaviorConfig] = None,
                 seed: Optional[int] = None) -> None:
        self.config = config or BehaviorConfig()
        self.mouse = BezierMouse(self.config, seed=seed)
        self._rng = random.Random(seed)

    def maybe_reading_pause(self) -> int:
        """根据配置概率返回阅读停顿毫秒数，0 表示不停。"""
        if self._rng.random() < self.config.reading_pause_chance:
            return self._rng.randint(
                self.config.reading_pause_min_ms,
                self.config.reading_pause_max_ms,
            )
        return 0

    def maybe_scroll_pause(self) -> int:
        if self._rng.random() < self.config.scroll_pause_chance:
            return self._rng.randint(
                self.config.scroll_pause_min_ms,
                self.config.scroll_pause_max_ms,
            )
        return 0

    def maybe_random_keypress(self) -> Optional[str]:
        if self._rng.random() < self.config.random_keypress_chance:
            # 误触字符（无意义的）
            return self._rng.choice(["Shift", "ArrowUp", "ArrowDown", "Tab"])
        return None

    def human_click_path(self, page: Any, start_xy: Tuple[float, float],
                         end_xy: Tuple[float, float]) -> bool:
        """点击前先按人类轨迹移动鼠标（依赖 Playwright）。"""
        if page is None:
            return False
        try:
            path = self.mouse.generate(start_xy, end_xy)
            for x, y in path:
                page.mouse.move(x, y)
            page.mouse.click(end_xy[0], end_xy[1])
            pause = self.maybe_reading_pause()
            if pause:
                page.wait_for_timeout(pause)
            return True
        except Exception as exc:  # noqa: BLE001
            log.warning("human click failed: %s", exc)
            return False

    def human_scroll(self, page: Any, direction: str = "down",
                     amount: int = 300) -> bool:
        """模拟人类滚动：分段 + 停顿。"""
        if page is None:
            return False
        try:
            steps = max(2, amount // 80)
            delta = amount // steps
            if direction == "up":
                delta = -delta
            for _ in range(steps):
                page.mouse.wheel(0, delta)
                pause = self.maybe_scroll_pause()
                if pause:
                    page.wait_for_timeout(pause)
            return True
        except Exception as exc:  # noqa: BLE001
            log.warning("human scroll failed: %s", exc)
            return False


# ============================================================================
# 6. WebSocket 监听
# ============================================================================

@dataclass
class WSMessage:
    """WebSocket 消息记录。"""
    direction: str     # "in" / "out"
    payload: str       # 文本或二进制 base64
    timestamp_ms: int
    url: str = ""


class WebSocketRecorder:
    """WebSocket 监听器（基于 Playwright page.on('websocket')）。"""

    def __init__(self, max_messages: int = 1000) -> None:
        self.messages: List[WSMessage] = []
        self._lock = threading.RLock()
        self.max_messages = max_messages
        self._attached_page: Any = None
        self._handlers: Dict[str, Any] = {}

    def attach(self, page: Any) -> bool:
        """绑定到 Playwright page。"""
        if page is None:
            return False
        try:
            page.on("websocket", self._on_ws_open)
            self._attached_page = page
            return True
        except Exception as exc:  # noqa: BLE001
            log.warning("WebSocket attach failed: %s", exc)
            return False

    def _on_ws_open(self, ws: Any) -> None:
        url = getattr(ws, "url", "")
        with self._lock:
            self._handlers[url] = ws
        try:
            ws.on("framereceived", lambda payload: self._record("in", payload, url))
            ws.on("framesent", lambda payload: self._record("out", payload, url))
        except Exception as exc:  # noqa: BLE001
            log.debug("ws event bind failed: %s", exc)

    def _record(self, direction: str, payload: Any, url: str) -> None:
        try:
            text = payload if isinstance(payload, str) else (
                payload.decode("utf-8", errors="replace") if hasattr(payload, "decode") else str(payload)
            )
        except Exception:  # noqa: BLE001
            text = str(payload)
        msg = WSMessage(
            direction=direction,
            payload=text[:5000],  # 截断避免内存爆炸
            timestamp_ms=int(time.time() * 1000),
            url=url,
        )
        with self._lock:
            self.messages.append(msg)
            if len(self.messages) > self.max_messages:
                # 滑动窗口：丢弃最早的
                self.messages = self.messages[-self.max_messages:]

    def get_messages(self, direction: Optional[str] = None,
                     url_contains: Optional[str] = None) -> List[WSMessage]:
        with self._lock:
            result = list(self.messages)
        if direction:
            result = [m for m in result if m.direction == direction]
        if url_contains:
            result = [m for m in result if url_contains in m.url]
        return result

    def clear(self) -> None:
        with self._lock:
            self.messages.clear()

    def stats(self) -> Dict[str, Any]:
        with self._lock:
            urls = sorted({m.url for m in self.messages})
            in_count = sum(1 for m in self.messages if m.direction == "in")
            out_count = sum(1 for m in self.messages if m.direction == "out")
            return {
                "total": len(self.messages),
                "in": in_count,
                "out": out_count,
                "unique_urls": len(urls),
                "urls": urls[:20],
            }


# ============================================================================
# 7. 公共导出
# ============================================================================

__all__ = [
    "ShadowPierceConfig",
    "ShadowDOMPiercer",
    "IframeChain",
    "IframeNavigator",
    "CookieAction",
    "COOKIE_BANNER_KEYWORDS",
    "CookieBannerPattern",
    "DEFAULT_COOKIE_PATTERNS",
    "CookieBannerHandler",
    "CaptchaType",
    "CaptchaDetection",
    "CaptchaRecognizer",
    "StdlibCaptchaRecognizer",
    "DdddocrCaptchaRecognizer",
    "TesseractCaptchaRecognizer",
    "CaptchaPipeline",
    "BehaviorConfig",
    "BezierMouse",
    "HumanBehaviorSimulator",
    "WSMessage",
    "WebSocketRecorder",
]