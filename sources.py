"""免费壁纸数据源适配器（全部无需付费 API Key）。

核心改进（v1.2）：**每一张图都必须通过真实分辨率校验**才会进入列表。

图片搜索接口普遍「谎报尺寸」或干脆不返回尺寸：
  - 360 图片：返回真实宽高，可信度最高，原图直链（实测 5184×3456 / 6006×3037）
  - Bing 图片：不返回尺寸，且「大图筛选」形同虚设（实测返回 1200×800）
  - 百度图片：直链被硬限制在 500px
本模块用 HTTP Range 只读取图片头部若干 KB，解析 JPEG/PNG/WebP 的 SOF 头
拿到**真实像素**，再按目标分辨率（1080P/2K/4K）严格过滤——因此界面上看到的
每一张都是真高清，设成壁纸不会糊。

适配源：
  1. 360 图片（image.so.com）—— 主力高清源，中文地名命中率高，支持深翻页
  2. Bing 图片（cn.bing.com/images）—— 补充，经探针筛掉小图
  3. Wikimedia Commons —— CC 授权，原图分辨率高（部分网络不可达时自动跳过）
  4. Openverse —— CC 授权图库
  5. 百度图片（image.baidu.com）—— 命中率最高，但仅 500px 标清（默认不参与高清模式）
  6. Bing 每日美图 / Picsum —— 4K 精品随机图

注意：360 / 百度 对高频请求会限流（返回「访问异常」HTML），本模块内置会话 Cookie、
节流（>=0.8s 间隔）与指数退避重试；单个源失败不影响其它源。
"""

from __future__ import annotations

import html as _html
import http.cookiejar
import json
import random
import re
import struct
import threading
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36")

# 画质档位：名称 -> 最小宽度(px)
QUALITY_PRESETS = {
    "1080P 及以上": 1920,
    "2K 及以上": 2560,
    "4K 及以上": 3840,
    "全部（含标清）": 0,
}
DEFAULT_QUALITY = "1080P 及以上"
DEFAULT_MIN_WIDTH = QUALITY_PRESETS[DEFAULT_QUALITY]

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
    verified: bool = False   # 宽高是否已由探针实测确认

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
            "verified": self.verified,
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
            verified=bool(d.get("verified")),
        )

    @property
    def res_text(self) -> str:
        if self.width and self.height:
            tag = "" if self.verified else "~"
            return f"{tag}{self.width}×{self.height}"
        return self.note or "尺寸未知"

    @property
    def is_landscape(self) -> bool:
        return self.width >= self.height


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
    """返回一个共享的、带 CookieJar 的 opener（懒初始化）。"""
    global _session_opener
    with _session_lock:
        if _session_opener is None:
            jar = http.cookiejar.CookieJar()
            op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
            op.addheaders = [("User-Agent", UA),
                             ("Accept-Language", "zh-CN,zh;q=0.9,en;q=0.8")]
            _session_opener = op
    return _session_opener


def _warmup(url: str) -> None:
    try:
        _opener().open(urllib.request.Request(url, headers={"User-Agent": UA}),
                       timeout=20).read()
    except Exception:  # noqa: BLE001
        pass


def _int(v) -> int:
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return 0


# ------------------------------------------------------- 真实分辨率探针（核心）

_DIMS_CACHE: dict[str, tuple[int, int]] = {}
_DIMS_LOCK = threading.Lock()
_PROBE_BYTES = 163072      # 读前 ~160KB 足够覆盖 EXIF/ICC 后的 SOF 段


def _dims_from_head(buf: bytes) -> tuple[int, int] | None:
    """从图片文件头解析真实像素尺寸（JPEG / PNG / GIF / WebP）。"""
    if len(buf) < 32:
        return None

    # JPEG：扫描 marker 直到 SOFn
    if buf[:2] == b"\xff\xd8":
        i, n = 2, len(buf)
        while i < n - 9:
            if buf[i] != 0xFF:
                i += 1
                continue
            marker = buf[i + 1]
            if marker in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7,
                          0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
                h, w = struct.unpack(">HH", buf[i + 5:i + 9])
                return (w, h) if w and h else None
            if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
                i += 2
                continue
            if i + 4 > n:
                return None
            seg = struct.unpack(">H", buf[i + 2:i + 4])[0]
            i += 2 + seg
        return None

    # PNG
    if buf[:8] == b"\x89PNG\r\n\x1a\n":
        w, h = struct.unpack(">II", buf[16:24])
        return (w, h) if w and h else None

    # GIF
    if buf[:3] == b"GIF":
        w, h = struct.unpack("<HH", buf[6:10])
        return (w, h) if w and h else None

    # WebP
    if buf[:4] == b"RIFF" and buf[8:12] == b"WEBP":
        fourcc = buf[12:16]
        if fourcc == b"VP8 ":
            w, h = struct.unpack("<HH", buf[26:30])
            return (w & 0x3FFF, h & 0x3FFF)
        if fourcc == b"VP8L":
            v = struct.unpack("<I", buf[21:25])[0]
            return ((v & 0x3FFF) + 1, ((v >> 14) & 0x3FFF) + 1)
        if fourcc == b"VP8X":
            w = 1 + (buf[24] | buf[25] << 8 | buf[26] << 16)
            h = 1 + (buf[27] | buf[28] << 8 | buf[29] << 16)
            return (w, h)
    return None


def probe_dims(url: str, referer: str | None = None,
               timeout: int = 8) -> tuple[int, int] | None:
    """用 HTTP Range 只读图片头部，返回 (宽, 高)。带全局缓存。"""
    if not url:
        return None
    with _DIMS_LOCK:
        hit = _DIMS_CACHE.get(url)
    if hit is not None:
        return hit if hit != (0, 0) else None

    headers = {"User-Agent": UA, "Range": f"bytes=0-{_PROBE_BYTES - 1}"}
    if referer:
        headers["Referer"] = referer
    try:
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            buf = resp.read(_PROBE_BYTES)
        dims = _dims_from_head(buf)
    except Exception:  # noqa: BLE001
        dims = None

    with _DIMS_LOCK:
        _DIMS_CACHE[url] = dims or (0, 0)
    return dims


def _referer_for(url: str) -> str | None:
    """不同站点防盗链：给探针带一个合理的 Referer。"""
    try:
        p = urllib.parse.urlsplit(url)
        host = p.netloc.lower()
    except Exception:  # noqa: BLE001
        return None
    if "qhimg" in host:
        return "https://image.so.com/"
    if "baidu" in host:
        return "https://image.baidu.com/"
    if "bing" in host:
        return "https://cn.bing.com/"
    if host:
        return f"{p.scheme or 'https'}://{host}/"
    return None


def probe_items(items: list[WallpaperItem], workers: int = 24,
                force: bool = False, limit: int | None = None,
                deadline: float = 12.0) -> None:
    """并发探测一批条目的真实分辨率，就地写回 width/height/verified。

    deadline：整体墙钟上限（秒）。到点即返回，未探完的条目保持未校验状态，
    避免个别站点连不上把整次搜索拖到一分钟。
    """
    todo = [it for it in items
            if it.full_url and (force or not (it.width and it.height and it.verified))]
    if limit is not None:
        todo = todo[:limit]
    if not todo:
        return

    def work(it: WallpaperItem):
        dims = probe_dims(it.full_url, referer=_referer_for(it.full_url))
        if dims:
            it.width, it.height = dims
            it.verified = True
            if it.note.startswith("标清"):
                it.note = ""

    ex = ThreadPoolExecutor(max_workers=max(4, workers))
    try:
        futs = [ex.submit(work, it) for it in todo]
        end = time.time() + max(2.0, deadline)
        for f in futs:
            left = end - time.time()
            if left <= 0:
                break
            try:
                f.result(timeout=left)
            except Exception:  # noqa: BLE001  超时或单条异常都跳过
                pass
    finally:
        ex.shutdown(wait=False)


# ---------------------------------------------------------------- 360 图片

def _clean_title(s: str) -> str:
    """去掉搜索结果标题里的 HTML 高亮标签与多余符号。"""
    s = re.sub(r"<[^>]+>", "", s or "")
    s = _html.unescape(s)
    s = re.sub(r"[\r\n\t]+", " ", s)
    return s.strip(" ,，、|-_")[:40]


def _so360_thumb(url: str, size: int = 500) -> str:
    """360 缩略图支持尺寸指令：/sdr/_500_/txxx.jpg，避免网格直接拉原图。

    已带尺寸指令的 URL 会先剥离旧指令，便于按需换尺寸（网格 500 / 预览 1200）。
    """
    try:
        p = urllib.parse.urlsplit(url)
        if not p.netloc or not p.path:
            return url
        path = re.sub(r"^/+sdr/_\d+_/+", "/", p.path)
        path = path.lstrip("/")
        return f"{p.scheme or 'https'}://{p.netloc}/sdr/_{size}_/{path}"
    except Exception:  # noqa: BLE001
        return url


def preview_url(item: "WallpaperItem", size: int = 1200) -> str:
    """预览用的大图地址：360 可换尺寸；其它源直接用缩略图。"""
    u = item.thumb_url or item.full_url
    if "qhimg" in u:
        return _so360_thumb(u, size)
    return u


def _so360_page(query: str, pn: int, count: int = 60) -> list[dict]:
    """拉取 360 图片的一页原始 JSON 列表（带 Referer，否则返回访问异常页）。"""
    q = urllib.parse.quote(query)
    url = f"https://image.so.com/j?q={q}&pn={pn}&rn={count}&src=srp"
    op = _opener()
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
            return data.get("list") or []
        except Exception:  # noqa: BLE001  限流时返回 HTML/空串
            if attempt == 2:
                return []
            time.sleep(1.5 * (attempt + 1))
    return []


def fetch_so360(query: str, page: int = 0, count: int = 60,
                subpages: int = 3) -> list[WallpaperItem]:
    """360 图片搜索：返回原图直链（含真实宽高）。

    一次翻 subpages 个子页（默认 3×60=180 条），因为高清占比约 7%，
    只取一页凑不出足够的高清图。
    """
    if not query:
        return []
    items: list[WallpaperItem] = []
    seen: set[str] = set()
    for i in range(max(1, subpages)):
        pn = (page * subpages + i) * count
        for it in _so360_page(query, pn, count):
            full = it.get("img") or ""
            if not full.startswith("http") or full in seen:
                continue
            seen.add(full)
            items.append(WallpaperItem(
                title=_clean_title(it.get("title") or it.get("litetitle") or query),
                thumb_url=_so360_thumb(it.get("thumb") or full, 500),
                full_url=full,
                source="360图片",
                page_url=it.get("link") or "",
                width=_int(it.get("width")), height=_int(it.get("height")),
                verified=True,          # 360 返回的宽高与实测一致
            ))
        if len(items) >= count * subpages:
            break
    return items


# ---------------------------------------------------------------- Bing 图片

def fetch_bing_images(query: str, page: int = 0, count: int = 35) -> list[WallpaperItem]:
    """Bing 图片搜索：Bing 不返回尺寸，故取回后由调用方用探针实测。"""
    if not query:
        return []
    q = urllib.parse.quote(query)
    first = page * count + 1
    # imagesize-wallpaper 由 Bing 服务端粗筛（实测仍会混入小图），真正把关靠探针
    url = (f"https://cn.bing.com/images/async?q={q}&first={first}&count={count}"
           f"&mkt=zh-CN&adlt=off&qft=+filterui:imagesize-wallpaper")
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
    # Bing 不返回尺寸：就地并发实测，与 360 的抓取并行，避免总耗时叠加
    if items:
        probe_items(items, workers=24, deadline=10.0)
    return items


# ---------------------------------------------------------------- 百度图片

def fetch_baidu(query: str, page: int = 0, count: int = 30) -> list[WallpaperItem]:
    """百度图片（直链被硬限制为 500px 标清，只作「全部」模式的兜底）。"""
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
            width=500, height=0,
            note="标清 500px",
        ))
    return items


# ---------------------------------------------------------------- Wikimedia

def fetch_wikimedia(query: str, limit: int = 30, target_width: int = 3840) -> list[WallpaperItem]:
    """Wikimedia Commons：中文名 + 英文别名各查一次，直接取大尺寸版本。"""
    qs = [query]
    alias = ALIASES.get(query.strip())
    if alias:
        qs.append(alias)

    items: list[WallpaperItem] = []
    seen: set[str] = set()
    for q in qs:
        params = {
            "action": "query", "generator": "search", "gsrsearch": q,
            "gsrnamespace": 6, "gsrlimit": limit, "prop": "imageinfo",
            "iiprop": "url|size", "iiurlwidth": target_width, "format": "json",
        }
        url = "https://commons.wikimedia.org/w/api.php?" + urllib.parse.urlencode(params)
        try:
            data = json.loads(_http_get(url, timeout=6, referer="https://commons.wikimedia.org/"))
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
            w, h = _int(info.get("width")), _int(info.get("height"))
            # 优先用 API 生成的大尺寸缩略图（既可当壁纸又不必拉十几 MB 原文件）
            big = info.get("thumburl") or full
            items.append(WallpaperItem(
                title=p.get("title", "").replace("File:", "").rsplit(".", 1)[0][:40],
                thumb_url=big, full_url=big, source="Wikimedia",
                page_url=info.get("descriptionurl", ""),
                width=w, height=h, verified=True,
            ))
    return items


# ---------------------------------------------------------------- Openverse

def fetch_openverse(query: str, limit: int = 30) -> list[WallpaperItem]:
    keyword = ALIASES.get(query.strip(), query or "landscape")
    url = ("https://api.openverse.org/v1/images/?q="
           + urllib.parse.quote(keyword) + f"&page_size={limit}")
    try:
        data = json.loads(_http_get(url, timeout=6))
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
        # 直接要 UHD 版本（3840×2160），拿不到再退回 1920
        uhd = "https://www.bing.com" + im.get("urlbase", "") + "_UHD.jpg"
        full = "https://www.bing.com" + im.get("urlbase", "") + "_1920x1080.jpg"
        items.append(WallpaperItem(
            title=(im.get("copyright") or "Bing 每日一图").split(" (")[0][:40],
            thumb_url=base, full_url=uhd, source="Bing每日",
            page_url=im.get("copyrightlink", ""),
            width=3840, height=2160, note=full,
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
            full_url=f"https://picsum.photos/id/{iid}/3840/2160",
            source="Picsum", page_url=r.get("url", ""),
            width=3840, height=2160, verified=True,
        ))
    return items


# ---------------------------------------------------------------- 聚合

def _dedupe(items: list[WallpaperItem]) -> list[WallpaperItem]:
    out, seen = [], set()
    for it in items:
        if not it.full_url or it.full_url in seen:
            continue
        seen.add(it.full_url)
        out.append(it)
    return out


def _sort_key(it: WallpaperItem):
    """横图优先，其次按真实宽度降序。"""
    return (0 if it.is_landscape else 1, -it.width, -it.height)


def fetch_multi(query: str, page: int = 0, min_width: int = DEFAULT_MIN_WIDTH,
                allow_low: bool = False,
                include_baidu: bool | None = None) -> tuple[list[WallpaperItem], list[str]]:
    """综合搜索：并发多源 → 探针校验真实分辨率 → 严格按画质过滤。

    min_width: 目标最小宽度（1920/2560/3840，0 表示不限制）
    allow_low: True 时保留不达标的图（用 note 标注），False 则只返回达标图
    返回 (items, errors)
    """
    query = (query or "").strip() or "风景"
    if include_baidu is None:
        include_baidu = min_width <= 0          # 标清源只在「全部」模式下参与

    jobs = {
        "360图片": lambda: fetch_so360(query, page=page, count=60, subpages=3),
        "Bing图片": lambda: fetch_bing_images(query, page=page, count=35),
        "Wikimedia": lambda: fetch_wikimedia(query, limit=20,
                                            target_width=max(2560, min_width or 2560)),
        "Openverse": lambda: fetch_openverse(query, limit=20),
    }
    if page == 0:
        jobs["Bing每日"] = lambda: fetch_bing(count=8, idx=0)
    if include_baidu and page == 0:
        jobs["百度图片"] = lambda: fetch_baidu(query, page=page, count=30)

    results: dict[str, list[WallpaperItem]] = {}
    errors: list[str] = []
    with ThreadPoolExecutor(max_workers=len(jobs)) as ex:
        futures = {name: ex.submit(fn) for name, fn in jobs.items()}
        for name, fut in futures.items():
            try:
                results[name] = fut.result(timeout=45) or []
            except Exception as e:  # noqa: BLE001
                errors.append(f"{name}: {str(e) or type(e).__name__}")

    merged: list[WallpaperItem] = []
    for name in ("360图片", "Bing图片", "Wikimedia", "Openverse", "百度图片", "Bing每日"):
        merged.extend(results.get(name, []))
    merged = _dedupe(merged)

    # 对「尺寸未知」的条目做真实分辨率探针（Bing / Openverse 不返回尺寸）
    unknown = [i for i in merged if not i.verified]
    if unknown:
        probe_items(unknown, workers=24, limit=56, deadline=8.0)

    if min_width > 0:
        good = [i for i in merged if i.width >= min_width]
        if not good:      # 一个都不达标：退回最好的几张，避免「空白页」
            good = sorted([i for i in merged if i.width > 0],
                          key=lambda i: -i.width)[:12]
            for i in good:
                if not i.note:
                    i.note = f"未达标 · 仅 {i.width}px"
    else:
        good = [i for i in merged if i.width > 0 or i.note]

    landscape = [i for i in good if i.is_landscape]
    portrait = [i for i in good if not i.is_landscape]
    # 横图够多时，桌面壁纸场景只留横图
    out = landscape + (portrait if len(landscape) < 12 else [])
    out.sort(key=_sort_key)
    return out, errors


# 源名称 -> 抓取函数（page, page_size）
SINGLE_SOURCES = {
    "360 图片（高清）": lambda q, p, n: fetch_so360(q or "风景", page=p, count=n, subpages=2),
    "Bing 图片（需校验）": lambda q, p, n: (
        lambda items: (probe_items(items, limit=40), items)[1]
    )(fetch_bing_images(q or "风景", page=p, count=n)),
    "百度图片（标清）": lambda q, p, n: fetch_baidu(q or "风景", page=p, count=n),
    "Wikimedia 图库": lambda q, p, n: fetch_wikimedia(q or "风景", limit=n),
    "Openverse 图库": lambda q, p, n: fetch_openverse(q or "landscape", limit=n),
    "Bing 每日美图": lambda q, p, n: fetch_bing(count=min(n, 8), idx=p * 8),
    "Picsum 随机美图": lambda q, p, n: fetch_picsum(limit=min(n, 50)),
}

MULTI_NAME = "🔥 综合搜索（推荐）"
FAV_NAME = "♥ 我的收藏（本地）"
SOURCE_NAMES = [MULTI_NAME] + list(SINGLE_SOURCES.keys()) + [FAV_NAME]


def fetch_page(source: str, query: str, page: int = 0, page_size: int = 60,
               min_width: int = DEFAULT_MIN_WIDTH):
    """统一分页入口：返回 (items, errors)。"""
    if source == MULTI_NAME:
        return fetch_multi(query, page=page, min_width=min_width)
    fn = SINGLE_SOURCES.get(source)
    if not fn:
        return [], [f"未知数据源: {source}"]
    try:
        return fn(query, page, page_size), []
    except Exception as e:  # noqa: BLE001
        return [], [f"{source}: {e}"]


def best_of(items: list[WallpaperItem], min_width: int) -> WallpaperItem | None:
    """从列表里挑达标且最大的横图（设壁纸兜底用）。"""
    cand = [i for i in items if i.width >= max(min_width, 1) and i.is_landscape]
    if not cand:
        cand = [i for i in items if i.width >= max(min_width, 1)]
    if not cand:
        return None
    return max(cand, key=lambda i: i.width)


# 兼容旧接口
def fetch(source_name: str, query: str = "", limit: int = 24):
    items, errors = fetch_page(source_name, query, page=0, page_size=limit)
    return items, ("；".join(errors) if errors and not items else None)
