"""免费壁纸数据源适配器（全部无需付费 API Key）。

所有函数统一返回 list[WallpaperItem]，失败时返回空列表并尽量不抛异常，
由上层 GUI 捕获并提示。适配的源：
  - Bing 每日美图（已验证可用，含中文版权说明）
  - Wikimedia Commons（按地名关键词搜索，标准公共 API）
  - Openverse（按关键词搜索 CC 授权图片，标准公共 API）
  - Picsum / Lorem Picsum（随机高质量美图，无需关键词）
"""

from __future__ import annotations

import json
import random
import urllib.parse
import urllib.request
from dataclasses import dataclass

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) WallpaperApp/1.0 (+https://github.com/local/wallpaper-app)"


@dataclass
class WallpaperItem:
    title: str          # 展示标题
    thumb_url: str      # 缩略图地址（网格展示用）
    full_url: str       # 设为壁纸/下载原图用（尽量 1080p+）
    source: str         # 来源名
    page_url: str = ""  # 原始页面（点击可查看出处/版权）

    def to_dict(self) -> dict:
        return {
            "title": self.title,
            "thumb_url": self.thumb_url,
            "full_url": self.full_url,
            "source": self.source,
            "page_url": self.page_url,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "WallpaperItem":
        return cls(
            title=d.get("title", ""),
            thumb_url=d.get("thumb_url", ""),
            full_url=d.get("full_url", ""),
            source=d.get("source", ""),
            page_url=d.get("page_url", ""),
        )


def _http_get(url: str, timeout: int = 40) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", "replace")


def _http_bytes(url: str, timeout: int = 60) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def _safe(fn):
    """包裹抓取函数：任何异常都吞掉并返回空列表，避免单个源故障拖垮整体。"""
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except Exception as e:  # noqa: BLE001
            return [], f"{fn.__name__} 失败: {e}"
    return wrapper


@_safe
def fetch_bing(count: int = 8, idx: int = 0):
    url = (f"https://www.bing.com/HPImageArchive.aspx?format=js"
           f"&idx={idx}&n={count}&mkt=zh-CN")
    data = json.loads(_http_get(url, timeout=30))
    items = []
    for im in data.get("images", []):
        base = "https://www.bing.com" + im.get("url", "")
        full = "https://www.bing.com" + im.get("urlbase", "") + "_1920x1080.jpg"
        items.append(WallpaperItem(
            title=im.get("copyright", "Bing 每日一图").split(" (")[0],
            thumb_url=base,
            full_url=full,
            source="Bing",
            page_url=im.get("copyrightlink", ""),
        ))
    return items, None


@_safe
def fetch_wikimedia(query: str, limit: int = 24):
    params = {
        "action": "query",
        "generator": "search",
        "gsrsearch": query,
        "gsrnamespace": 6,
        "gsrlimit": limit,
        "prop": "imageinfo",
        "iiprop": "url|size|extmetadata",
        "iiurlwidth": 600,
        "format": "json",
    }
    url = "https://commons.wikimedia.org/w/api.php?" + urllib.parse.urlencode(params)
    data = json.loads(_http_get(url, timeout=40))
    items = []
    pages = data.get("query", {}).get("pages", {})
    for p in pages.values():
        ii = p.get("imageinfo")
        if not ii:
            continue
        info = ii[0]
        thumb = info.get("thumburl") or info.get("url", "")
        full = info.get("url", "")
        # 把 600px 缩略图换成 1600px 版本用于设壁纸，体积更友好
        wall = thumb.replace("600px-", "1600px-") if "600px-" in thumb else full
        title = p.get("title", "").replace("File:", "").rsplit(".", 1)[0]
        items.append(WallpaperItem(
            title=title,
            thumb_url=thumb,
            full_url=wall,
            source="Wikimedia",
            page_url=info.get("descriptionurl", ""),
        ))
    return items, None


@_safe
def fetch_openverse(query: str, limit: int = 24):
    url = ("https://api.openverse.org/v1/images/?q="
           + urllib.parse.quote(query) + f"&page_size={limit}")
    data = json.loads(_http_get(url, timeout=40))
    items = []
    for r in data.get("results", []):
        thumb = r.get("thumbnail") or r.get("url")
        if not thumb:
            continue
        items.append(WallpaperItem(
            title=(r.get("title") or "Openverse 图片")[:60],
            thumb_url=thumb,
            full_url=r.get("url") or thumb,
            source="Openverse",
            page_url=r.get("foreign_landing_url", ""),
        ))
    return items, None


@_safe
def fetch_picsum(page: int | None = None, limit: int = 30):
    if page is None:
        page = random.randint(1, 40)
    url = f"https://picsum.photos/v2/list?page={page}&limit={limit}"
    data = json.loads(_http_get(url, timeout=30))
    items = []
    for r in data:
        iid = r.get("id")
        if iid is None:
            continue
        items.append(WallpaperItem(
            title=f"Picsum #{iid} · {r.get('author', '')}",
            thumb_url=f"https://picsum.photos/id/{iid}/500/330",
            full_url=f"https://picsum.photos/id/{iid}/1920/1080",
            source="Picsum",
            page_url=r.get("url", ""),
        ))
    return items, None


# 源名称 -> 抓取函数映射，供 GUI 下拉选择
SOURCES = {
    "Bing 每日美图": lambda q, n: fetch_bing(count=n),
    "Wikimedia 地名搜图": fetch_wikimedia,
    "Openverse 关键词搜图": fetch_openverse,
    "Picsum 随机美图": lambda q, n: fetch_picsum(limit=n),
}


def fetch(source_name: str, query: str = "", limit: int = 24):
    """统一入口：返回 (items, error_or_None)。"""
    if source_name == "Bing 每日美图":
        return fetch_bing(count=limit)
    if source_name == "Wikimedia 地名搜图":
        return fetch_wikimedia(query or "风景", limit=limit)
    if source_name == "Openverse 关键词搜图":
        return fetch_openverse(query or "landscape", limit=limit)
    if source_name == "Picsum 随机美图":
        return fetch_picsum(limit=limit)
    return [], f"未知数据源: {source_name}"
