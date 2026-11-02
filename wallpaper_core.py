"""壁纸核心能力：下载（带缓存）、Windows 设置壁纸、缓存目录管理。"""

from __future__ import annotations

import ctypes
import os
import threading
import urllib.parse

from sources import _http_bytes, UA

SPI_SETDESKWALLPAPER = 20
SPIF_UPDATEINIFILE = 0x01
SPIF_SENDCHANGE = 0x02


def cache_dir() -> str:
    d = os.path.join(os.path.expanduser("~"), "Pictures", "WallpaperApp", "cache")
    os.makedirs(d, exist_ok=True)
    return d


def favorites_dir() -> str:
    d = os.path.join(os.path.expanduser("~"), "Pictures", "WallpaperApp", "favorites")
    os.makedirs(d, exist_ok=True)
    return d


def _safe_filename(name: str, ext: str) -> str:
    bad = '\\/:*?"<>|'
    clean = "".join("_" if c in bad else c for c in name)[:60]
    return f"{clean}.{ext}"


def download(url: str, dest_path: str, timeout: int = 90) -> str:
    """下载 url 到 dest_path，返回本地路径。失败抛异常由调用方处理。"""
    data = _http_bytes(url, timeout=timeout)
    with open(dest_path, "wb") as f:
        f.write(data)
    return dest_path


def download_to_cache(url: str, prefix: str = "wp") -> str:
    """下载到缓存目录，文件名用 URL 哈希避免重复。返回本地路径。"""
    ext = "jpg"
    p = urllib.parse.urlparse(url)
    last = p.path.rsplit("/", 1)[-1].lower()
    if last.endswith(".png"):
        ext = "png"
    elif last.endswith(".jpeg") or last.endswith(".jpg"):
        ext = "jpg"
    import hashlib
    h = hashlib.md5(url.encode("utf-8")).hexdigest()[:16]
    dest = os.path.join(cache_dir(), f"{prefix}_{h}.{ext}")
    if os.path.exists(dest) and os.path.getsize(dest) > 0:
        return dest
    return download(url, dest)


def set_wallpaper(path: str) -> bool:
    """在 Windows 上把指定图片设为桌面壁纸。成功返回 True。"""
    abs_path = os.path.abspath(path)
    if not os.path.exists(abs_path):
        return False
    try:
        ok = ctypes.windll.user32.SystemParametersInfoW(
            SPI_SETDESKWALLPAPER, 0, abs_path,
            SPIF_UPDATEINIFILE | SPIF_SENDCHANGE,
        )
        return ok != 0
    except Exception:  # noqa: BLE001
        return False


def set_wallpaper_from_url(url: str) -> tuple[bool, str]:
    """下载并设壁纸的便捷封装，返回 (成功, 本地路径)。"""
    local = download_to_cache(url, prefix="wall")
    ok = set_wallpaper(local)
    return ok, local


def image_size(path: str) -> tuple[int, int] | None:
    """读取本地图片的真实像素尺寸（不依赖 Pillow）。"""
    try:
        from sources import _dims_from_head
        with open(path, "rb") as f:
            head = f.read(262144)
        return _dims_from_head(head)
    except Exception:  # noqa: BLE001
        return None


if __name__ == "__main__":
    # 简单自检：Bing 每日一图下载并设置（需 Windows + 网络）
    from sources import fetch_bing
    items, err = fetch_bing(count=1)
    if err:
        print("抓取失败:", err)
    elif items:
        ok, local = set_wallpaper_from_url(items[0].full_url)
        print("设置壁纸:", ok, local)
    else:
        print("未获取到图片")
