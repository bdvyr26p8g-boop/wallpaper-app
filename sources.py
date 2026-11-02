"""免费壁纸数据源适配器（全部无需付费 API Key）。

统一返回 list[WallpaperItem]。适配的源（按画质排序）：
  1. 360 图片（image.so.com）—— 原图直链 + 尺寸，中文地名命中率高，单查询可达百张
  2. Bing 图片（cn.bing.com/images）—— 原图直链，量大
  3. Wikimedia Commons —— 双语关键词检索，风景/人文摄影质量高（CC 授权）
  4. Openverse —— CC 授权图库
  5. 百度图片（image.baidu.com）—— 命中率最高，但直链被限制为 500px 标清
  6. Bing 每日美图 / Picsum —— 随机精品

注意：360 / 百度 对高频请求会限流（返回非 JSON），本模块内置节流（>=0.8s 间隔）
与指数退避重试；单个源失败不影响其它源。
"""

from __future__ import annotations

import html as _html
import json
import random
import re
import threading
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36")

# 中文地名 -> 英文/西文关键词，用于 Wikimedia / Openverse 提升命中率
ALIASES = {
    "泸沽湖": "Lugu Lake",
    "丽江": "Lijiang",
    "丽江古城": "Lijiang Old Town",
    "理塘": "Litang",
    "稻城": "Daocheng",
    "稻城亚丁": "Yading Nature Reserve",
    "亚丁": "Yading",
    "大理": "Dali Yunnan",
    "洱海": "Erhai Lake",
    "香格里拉": "Shangri-La Yunnan",
    "青海湖": "Qinghai Lake",
    "拉萨": "Lhasa",
    "布达拉宫": "Potala Palace",
    "纳木错": "Namtso",
    "九寨沟": "Jiuzhaigou",
    "张家界": "Zhangjiajie",
    "黄山": "Huangshan",
    "桂林": "Guilin",
    "阳朔": "Yangshuo",
    "婺源": "Wuyuan",
    "禾木": "Hemu",
    "喀纳斯": "Kanas Lake",
    "呼伦贝尔": "Hulunbuir",
    "雪山": "snow mountain China",
    "古镇": "ancient town China",
    "草原": "grassland China",
    "星空": "milky way night sky",
    "海岸": "coast China sea",
    "银杏": "ginkgo tree autumn",
    "梯田": "rice terraces China",
    "沙漠": "desert China",
    "瀑布": "waterfall China",
    "秋景": "autumn landscape China",
    "冬雪": "snow winter landscape China",
}


@dataclass
class WallpaperItem:
    title: str
    thumb_url: str
    full_url: str
    source: str
    page_url: str = ""
    width: int = 0
    height: int = 0
    note: str = ""       # 例如「标清 500px」

    def to_dict(self) -> dict:
        return {
            "title": self.title,
            "thumb_url": self.thumb_url,
            "full_url": self.full_url,
            "source": self.source,
            "page_url": self.page_url,
            "width": self.width,
            "height": self.height,
            "note": self.note,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "WallpaperItem":
        return cls(
            title=d.get("title", ""),
            thumb_url=d.get("thumb_url", ""),
            full_url=d.get("full_url", ""),
            source=d.get("source", ""),
            page_url=d.get("page_url", ""),
            width=int(d.get("width") or 0),
            height=int(d.get("height") or 0),
            note=d.get("note", ""),
        )

    @property
    def res_text(self) -> str:
        if self.width and self.height:
            return f"{self.width}×{self.height}"
        return self.note or "未知尺寸"


# ---------------------------------------------------------------- HTTP 基础

def _http_get(url: str, timeout: int = 30, referer: str | None = None) -> str:
    headers = {"User-Agent": UA, "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8"}
    if referer:
        headers["Referer"] = referer
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read()
    return raw.decode("utf-8", "replace")


def _http_bytes(url: str, timeout: int = 60) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


class _Throttle:
    """简单节流：保证同一站点的两次请求间隔不小于 gap 秒。"""

    def __init__(self, gap: float = 0.8):
        self.gap = gap
        self._lock = threading.Lock()
        self._last = 0.0

    def wait(self):
        with self._lock:
            delta = time.time() - self._last
            if delta < self.gap:
                time.sleep(self.gap - delta)
            self._last = time.time()


_so_throttle = _Throttle(0.8)
_baidu_throttle = _Throttle(0.9)

_baidu_cookies: list[str] = []
_baidu_lock = threading.Lock()

# 带 Cookie 的会话（360 / 百度 均需先访问站点拿 Cookie，否则触发风控）
_session_lock = threading.Lock()
_session_opener = None


def _opener():
    """返回一个共享的、带 CookieJar 的 opener（懒初始化 + 预热首页）。"""
    global _session_opener
    with _session_lock:
        if _session_opener is None:
            import http.cookiejar

            jar = http.cookiejar.CookieJar()
            op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
            op.addheaders = [("User-Agent", UA),
                             ("Accept-Language", "zh-CN,zh;q=0.9,en;q=0.8")]
            _session_opener = op
    return _session_opener


def _warmup(url: str) -> None:
    try:
        _opener().open(urllib.request.Request(url, headers={"User-Agent": UA}), timeout=20).read()
    except Exception:  # noqa: BLE001
        pass


def _int(v) -> int:
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return 0


# ---------------------------------------------------------------- 360 图片

def _clean_title(s: str) -> str:
    """去掉搜索结果标题里的 HTML 高亮标签与多余符号。"""
    s = re.sub(r"<[^>]+>", "", s or "")
    s = _html.unescape(s)
    s = re.sub(r"[\r\n\t]+", " ", s)
    return s.strip(" ,，、|-_")[:40]


def _so360_thumb(url: str, size: int = 500) -> str:
    """360 缩略图支持尺寸指令：/sdr/_500_/txxx.jpg，避免网格直接拉原图。"""
    try:
        p = urllib.parse.urlsplit(url)
        if not p.netloc or not p.path:
            return url
        return f"{p.scheme or 'https'}://{p.netloc}/sdr/_{size}_/{p.path.lstrip('/')}"
    except Exception:  # noqa: BLE001
        return url


def fetch_so360(query: str, page: int = 0, count: int = 60) -> list[WallpaperItem]:
    """360 图片搜索：返回原图直链（含宽高），中文地名命中率高。

    360 对无 Cookie 的裸请求会返回「访问异常」HTML，故统一走会话 opener。
    """
    if not query:
        return []
    q = urllib.parse.quote(query)
    url = f"https://image.so.com/j?q={q}&pn={page * count}&rn={count}&src=srp"
    op = _opener()
    data = None
    for attempt in range(3):
        _so_throttle.wait()
        try:
            if attempt == 0:
                _warmup("https://image.so.com/")
            req = urllib.request.Request(url, headers={
                "User-Agent": UA,
                "Referer": f"https://image.so.com/i?q={q}",
                "Accept": "application/json, text/javascript, */*; q=0.01",
                "X-Requested-With": "XMLHttpRequest",
            })
            with op.open(req, timeout=25) as r:
                data = json.loads(r.read().decode("utf-8", "replace"))
            break
        except Exception:  # noqa: BLE001  限流时返回 HTML/空串
            if attempt == 2:
                return []
            time.sleep(1.5 * (attempt + 1))
    if not data:
        return []
    items: list[WallpaperItem] = []
    for it in data.get("list") or []:
        full = it.get("img") or ""
        if not full.startswith("http"):
            continue
        w, h = _int(it.get("width")), _int(it.get("height"))
        thumb = _so360_thumb(it.get("thumb") or full, 500)
        items.append(WallpaperItem(
            title=_clean_title(it.get("title") or it.get("litetitle") or query),
            thumb_url=thumb,
            full_url=full,
            source="360图片",
            page_url=it.get("link") or "",
            width=w, height=h,
        ))
    return items


# ---------------------------------------------------------------- Bing 图片

def fetch_bing_images(query: str, page: int = 0, count: int = 35) -> list[WallpaperItem]:
    """Bing 图片搜索（仅大图），返回原图直链。"""
    if not query:
        return []
    q = urllib.parse.quote(query)
    first = page * count + 1
    url = (f"https://cn.bing.com/images/async?q={q}&first={first}&count={count}"
           f"&mkt=zh-CN&adlt=off&qft=+filterui:imagesize-large")
    try:
        html = _http_get(url, timeout=25, referer=f"https://cn.bing.com/images/search?q={q}")
    except Exception:  # noqa: BLE001
        return []
    items: list[WallpaperItem] = []
    seen: set[str] = set()
    for raw in re.findall(r'\sm="(\{[^"]*?\})"', html):
        try:
            d = json.loads(_html.unescape(raw))
        except Exception:  # noqa: BLE001
            continue
        murl = d.get("murl") or ""
        if not murl.startswith("http") or murl in seen:
            continue
        seen.add(murl)
        items.append(WallpaperItem(
            title=_clean_title(d.get("t") or query),
            thumb_url=d.get("turl") or murl,
            full_url=murl,
            source="Bing图片",
            page_url=d.get("purl") or "",
        ))
    return items


# ---------------------------------------------------------------- 百度图片

def fetch_baidu(query: str, page: int = 0, count: int = 30) -> list[WallpaperItem]:
    """百度图片（直链被限制为 500px 标清，故标注注明）。"""
    if not query:
        return []
    q = urllib.parse.quote(query)
    op = _opener()
    # 先访问首页拿 BAIDUID Cookie，否则 acjson 返回空 data
    if not _baidu_cookies:
        with _baidu_lock:
            if not _baidu_cookies:
                _warmup("https://image.baidu.com/")
                _baidu_cookies.append("warmed")

    url = ("https://image.baidu.com/search/acjson?tn=resultjson_com&ipn=rj&ct=201326592"
           f"&is=&fp=result&word={q}&queryWord={q}&cl=2&lm=-1&ie=utf-8&oe=utf-8"
           "&st=-1&z=&ic=0&hd=1&width=&height=&face=0&istype=2&qc=&nc=1"
           f"&pn={page * count}&rn={count}&gsm=1e")
    headers = {
        "User-Agent": UA,
        "Referer": f"https://image.baidu.com/search/index?tn=baiduimage&word={q}",
        "Accept": "text/plain, */*; q=0.01",
        "X-Requested-With": "XMLHttpRequest",
    }

    data = None
    for attempt in range(2):
        _baidu_throttle.wait()
        try:
            req = urllib.request.Request(url, headers=headers)
            with op.open(req, timeout=25) as r:
                data = json.loads(r.read().decode("utf-8", "replace"))
            break
        except Exception:  # noqa: BLE001
            if attempt == 1:
                return []
            time.sleep(1.0)

    items: list[WallpaperItem] = []
    for d in (data or {}).get("data") or []:
        if not d:
            continue
        full = d.get("thumbURL") or d.get("middleURL") or ""
        if not full.startswith("http"):
            continue
        items.append(WallpaperItem(
            title=_clean_title(d.get("fromPageTitleEnc") or d.get("fromPageTitle") or query),
            thumb_url=d.get("middleURL") or full,
            full_url=full,
            source="百度图片",
            page_url="",
            # 百度直链被限制为 500px，width 按实际值标注（避免误导为原图尺寸）
            width=500, height=0,
            note="标清 500px",
        ))
    return items


# ---------------------------------------------------------------- Wikimedia

def fetch_wikimedia(query: str, limit: int = 30) -> list[WallpaperItem]:
    """Wikimedia Commons：中文名 + 英文别名各查一次，提升命中率。"""
    qs = [query]
    alias = ALIASES.get(query.strip())
    if alias:
        qs.append(alias)
    elif re.search(r"[\u4e00-\u9fff]", query):
        qs.append(query)  # 无别名时保持原样

    items: list[WallpaperItem] = []
    seen: set[str] = set()
    for q in qs:
        params = {
            "action": "query", "generator": "search", "gsrsearch": q,
            "gsrnamespace": 6, "gsrlimit": limit, "prop": "imageinfo",
            "iiprop": "url|size", "iiurlwidth": 640, "format": "json",
        }
        url = "https://commons.wikimedia.org/w/api.php?" + urllib.parse.urlencode(params)
        try:
            data = json.loads(_http_get(url, timeout=20, referer="https://commons.wikimedia.org/"))
        except Exception:  # noqa: BLE001
            continue
        for p in (data.get("query", {}).get("pages", {}) or {}).values():
            ii = p.get("imageinfo") or []
            if not ii:
                continue
            info = ii[0]
            full = info.get("url", "")
            if not full or full in seen:
                continue
            if not re.search(r"\.(jpe?g|png)$", full, re.I):
                continue
            seen.add(full)
            thumb = info.get("thumburl") or full
            wall = thumb.replace("640px-", "1600px-") if "640px-" in thumb else full
            items.append(WallpaperItem(
                title=p.get("title", "").replace("File:", "").rsplit(".", 1)[0][:40],
                thumb_url=thumb, full_url=wall, source="Wikimedia",
                page_url=info.get("descriptionurl", ""),
                width=_int(info.get("width")), height=_int(info.get("height")),
            ))
    return items


# ---------------------------------------------------------------- Openverse

def fetch_openverse(query: str, limit: int = 30) -> list[WallpaperItem]:
    keyword = ALIASES.get(query.strip(), query or "landscape")
    url = ("https://api.openverse.org/v1/images/?q="
           + urllib.parse.quote(keyword) + f"&page_size={limit}")
    try:
        data = json.loads(_http_get(url, timeout=20))
    except Exception:  # noqa: BLE001
        return []
    items: list[WallpaperItem] = []
    for r in data.get("results", []):
        thumb = r.get("thumbnail") or r.get("url")
        if not thumb:
            continue
        items.append(WallpaperItem(
            title=(r.get("title") or keyword)[:40],
            thumb_url=thumb, full_url=r.get("url") or thumb,
            source="Openverse", page_url=r.get("foreign_landing_url", ""),
            width=_int(r.get("width")), height=_int(r.get("height")),
        ))
    return items


# ---------------------------------------------------------------- 随机美图

def fetch_bing(count: int = 8, idx: int = 0) -> list[WallpaperItem]:
    url = (f"https://www.bing.com/HPImageArchive.aspx?format=js"
           f"&idx={idx}&n={count}&mkt=zh-CN")
    try:
        data = json.loads(_http_get(url, timeout=25))
    except Exception:  # noqa: BLE001
        return []
    items: list[WallpaperItem] = []
    for im in data.get("images", []):
        base = "https://www.bing.com" + im.get("url", "")
        full = "https://www.bing.com" + im.get("urlbase", "") + "_1920x1080.jpg"
        items.append(WallpaperItem(
            title=(im.get("copyright") or "Bing 每日一图").split(" (")[0][:40],
            thumb_url=base, full_url=full, source="Bing每日",
            page_url=im.get("copyrightlink", ""),
            width=1920, height=1080,
        ))
    return items


def fetch_picsum(page: int | None = None, limit: int = 30) -> list[WallpaperItem]:
    if page is None:
        page = random.randint(1, 40)
    url = f"https://picsum.photos/v2/list?page={page}&limit={limit}"
    try:
        data = json.loads(_http_get(url, timeout=25))
    except Exception:  # noqa: BLE001
        return []
    items: list[WallpaperItem] = []
    for r in data:
        iid = r.get("id")
        if iid is None:
            continue
        items.append(WallpaperItem(
            title=f"Picsum #{iid} · {r.get('author', '')}"[:40],
            thumb_url=f"https://picsum.photos/id/{iid}/500/330",
            full_url=f"https://picsum.photos/id/{iid}/1920/1080",
            source="Picsum", page_url=r.get("url", ""),
            width=1920, height=1080,
        ))
    return items


# ---------------------------------------------------------------- 聚合

def _dedupe(items: list[WallpaperItem], key_len: int = 0) -> list[WallpaperItem]:
    out, seen = [], set()
    for it in items:
        key = it.full_url if not key_len else it.full_url[:key_len]
        if key in seen or not it.full_url:
            continue
        seen.add(key)
        out.append(it)
    return out


def fetch_multi(query: str, page: int = 0, min_width: int = 1600,
                include_baidu: bool = True) -> tuple[list[WallpaperItem], list[str]]:
    """综合搜索：并发查询多个源并合并去重，按分辨率降序。

    单个源超时/失败会被忽略，返回 (items, errors)。
    """
    query = (query or "").strip() or "风景"

    jobs = {
        "360图片": lambda: fetch_so360(query, page=page, count=60),
        "Bing图片": lambda: fetch_bing_images(query, page=page, count=35),
        "Wikimedia": lambda: fetch_wikimedia(query, limit=20),
        "Openverse": lambda: fetch_openverse(query, limit=20),
    }
    if include_baidu and page == 0:
        jobs["百度图片"] = lambda: fetch_baidu(query, page=page, count=30)

    results: dict[str, list[WallpaperItem]] = {}
    errors: list[str] = []
    with ThreadPoolExecutor(max_workers=len(jobs)) as ex:
        futures = {name: ex.submit(fn) for name, fn in jobs.items()}
        for name, fut in futures.items():
            try:
                results[name] = fut.result(timeout=40) or []
            except Exception as e:  # noqa: BLE001
                errors.append(f"{name}: {str(e) or type(e).__name__}")

    merged: list[WallpaperItem] = []
    for name in ("360图片", "Bing图片", "Wikimedia", "Openverse", "百度图片"):
        merged.extend(results.get(name, []))
    merged = _dedupe(merged)

    # 分档：高清 > 中等 > 尺寸未知 > 标清；标清仅在数量不足时兜底
    hd = sorted([i for i in merged if i.width >= min_width], key=lambda i: -i.width)
    mid = sorted([i for i in merged if 900 <= i.width < min_width], key=lambda i: -i.width)
    unknown = [i for i in merged if i.width == 0]
    sd = [i for i in merged if 0 < i.width < 900 or i.note.startswith("标清")]

    out = hd + mid + unknown
    if len(out) < 40:          # 高清太少时用标清补足，保证「有图可看」
        out += sd
    return out, errors


# 源名称 -> 抓取函数（page, page_size）
SINGLE_SOURCES = {
    "360 图片（高清）": lambda q, p, n: fetch_so360(q or "风景", page=p, count=n),
    "Bing 图片（高清）": lambda q, p, n: fetch_bing_images(q or "风景", page=p, count=n),
    "百度图片（标清）": lambda q, p, n: fetch_baidu(q or "风景", page=p, count=n),
    "Wikimedia 图库": lambda q, p, n: fetch_wikimedia(q or "风景", limit=n),
    "Openverse 图库": lambda q, p, n: fetch_openverse(q or "landscape", limit=n),
    "Bing 每日美图": lambda q, p, n: fetch_bing(count=min(n, 8), idx=p * 8),
    "Picsum 随机美图": lambda q, p, n: fetch_picsum(limit=min(n, 50)),
}

MULTI_NAME = "🔥 综合搜索（推荐）"
FAV_NAME = "♥ 我的收藏（本地）"
SOURCE_NAMES = [MULTI_NAME] + list(SINGLE_SOURCES.keys()) + [FAV_NAME]


def fetch_page(source: str, query: str, page: int = 0, page_size: int = 60):
    """统一分页入口：返回 (items, errors)。"""
    if source == MULTI_NAME:
        return fetch_multi(query, page=page)
    fn = SINGLE_SOURCES.get(source)
    if not fn:
        return [], [f"未知数据源: {source}"]
    try:
        return fn(query, page, page_size), []
    except Exception as e:  # noqa: BLE001
        return [], [f"{source}: {e}"]


# 兼容旧接口
def fetch(source_name: str, query: str = "", limit: int = 24):
    items, errors = fetch_page(source_name, query, page=0, page_size=limit)
    return items, ("；".join(errors) if errors and not items else None)
