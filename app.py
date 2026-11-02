"""免费壁纸软件 —— 桌面 GUI 主程序。

功能：
  - 从 Bing / Wikimedia / Openverse / Picsum 免费获取壁纸（无需任何付费 Key）
  - 关键词/地名搜索（泸沽湖、丽江、理塘……）
  - 缩略图网格浏览（异步加载，不卡界面）
  - 一键设为 Windows 桌面壁纸
  - 下载原图、收藏到本地
  - 定时自动轮换壁纸
  - 设置与收藏本地持久化

运行：双击 run.bat，或 `python app.py`（需 Python 3.8+，建议 `pip install pillow` 以显示缩略图）。
"""

from __future__ import annotations

import io
import json
import os
import queue
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

try:
    from PIL import Image, ImageTk  # type: ignore
    HAS_PIL = True
except Exception:  # noqa: BLE001
    HAS_PIL = False

import sources
from sources import WallpaperItem
import wallpaper_core as wc

APP_DIR = os.path.dirname(os.path.abspath(__file__))
SETTINGS_PATH = os.path.join(APP_DIR, "settings.json")
FAV_PATH = os.path.join(APP_DIR, "favorites.json")

QUICK_TAGS = ["泸沽湖", "丽江", "理塘", "稻城", "大理", "青海湖",
              "雪山", "古镇", "草原", "星空", "海岸", "银杏"]

SOURCE_NAMES = ["Bing 每日美图", "Wikimedia 地名搜图",
                "Openverse 关键词搜图", "Picsum 随机美图", "我的收藏 (本地)"]

# 列数（缩略图网格）
COLS = 3
CELL_W, CELL_H = 240, 180


class ScrollableFrame(tk.Frame):
    """带纵向滚动条的画布容器，内部放缩略图网格。"""

    def __init__(self, master, **kw):
        super().__init__(master, **kw)
        self.canvas = tk.Canvas(self, bg="#f5f6f8", highlightthickness=0)
        self.scroll = ttk.Scrollbar(self, orient="vertical", command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=self.scroll.set)
        self.inner = tk.Frame(self.canvas, bg="#f5f6f8")
        self.inner_id = self.canvas.create_window((0, 0), window=self.inner, anchor="nw")
        self.canvas.pack(side="left", fill="both", expand=True)
        self.scroll.pack(side="right", fill="y")
        self.inner.bind("<Configure>", self._on_configure)
        self.canvas.bind("<Configure>", self._on_canvas_configure)
        # 鼠标滚轮
        self.canvas.bind_all("<MouseWheel>", self._on_mousewheel)

    def _on_configure(self, _e):
        self.canvas.configure(scrollregion=self.canvas.bbox("all"))

    def _on_canvas_configure(self, _e):
        self.canvas.itemconfig(self.inner_id, width=self.canvas.winfo_width())

    def _on_mousewheel(self, event):
        # 仅当鼠标在画布上时滚动
        if event.widget == self.canvas:
            self.canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")

    def clear(self):
        for w in self.inner.winfo_children():
            w.destroy()


class WallpaperApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("免费壁纸 · WallpaperApp")
        self.root.geometry("1080x720")
        self.root.minsize(820, 560)

        self.items: list[WallpaperItem] = []
        self.photo_refs: list = []          # 持有 PhotoImage 引用，防止被 GC
        self.selected: int | None = None
        self.favorites: list[dict] = []
        self.settings: dict = {}
        self.load_settings()
        self.load_favorites()

        self.load_q: queue.Queue = queue.Queue()
        for _ in range(8):
            threading.Thread(target=self._loader_worker, daemon=True).start()

        self.auto_timer = None
        self.auto_index = 0
        self.auto_stop = threading.Event()

        self._build_ui()
        self._show_pil_hint()
        # 启动即拉取默认源
        self.source_var.set(self.settings.get("source", "Bing 每日美图"))
        self.root.after(300, self.do_search)

    # ---------------- UI 构建 ----------------
    def _build_ui(self):
        # 顶部搜索栏
        top = ttk.Frame(self.root, padding=8)
        top.pack(side="top", fill="x")

        ttk.Label(top, text="关键词").pack(side="left")
        self.query_var = tk.StringVar(value=self.settings.get("last_query", ""))
        qe = ttk.Entry(top, textvariable=self.query_var, width=22)
        qe.pack(side="left", padx=4)
        qe.bind("<Return>", lambda e: self.do_search())

        ttk.Button(top, text="搜索", command=self.do_search).pack(side="left", padx=2)
        ttk.Label(top, text="来源").pack(side="left", padx=(10, 2))
        self.source_var = tk.StringVar()
        src = ttk.Combobox(top, textvariable=self.source_var, values=SOURCE_NAMES,
                           state="readonly", width=18)
        src.pack(side="left")
        src.bind("<<ComboboxSelected>>", lambda e: self.do_search())
        ttk.Button(top, text="随机换一批", command=self.do_search).pack(side="left", padx=4)
        ttk.Button(top, text="📁 我的收藏", command=self.open_favorites_folder).pack(side="left", padx=2)

        # 快捷标签
        tag_frame = ttk.Frame(self.root, padding=(8, 0, 8, 6))
        tag_frame.pack(side="top", fill="x")
        ttk.Label(tag_frame, text="快捷:").pack(side="left")
        for t in QUICK_TAGS:
            b = ttk.Button(tag_frame, text=t, width=6,
                           command=lambda x=t: self.search_tag(x))
            b.pack(side="left", padx=2)

        # 主区域：左缩略图 + 右预览/操作
        main = ttk.Frame(self.root)
        main.pack(side="top", fill="both", expand=True, padx=8, pady=4)

        self.scroll_frame = ScrollableFrame(main)
        self.scroll_frame.pack(side="left", fill="both", expand=True)

        right = ttk.Frame(main, width=320, padding=8)
        right.pack(side="right", fill="y")
        right.pack_propagate(False)

        ttk.Label(right, text="预览", font=("Microsoft YaHei", 11, "bold")).pack(anchor="w")
        self.preview = tk.Label(right, bg="#e9ecef", width=38, height=12,
                                relief="sunken", text="点击左侧图片预览")
        self.preview.pack(fill="x", pady=4)

        self.info_var = tk.StringVar(value="")
        ttk.Label(right, textvariable=self.info_var, wraplength=300,
                  justify="left").pack(anchor="w", pady=2)

        ttk.Button(right, text="★ 设为壁纸", command=self.set_current).pack(fill="x", pady=3)
        ttk.Button(right, text="⬇ 下载原图", command=self.download_current).pack(fill="x", pady=3)
        ttk.Button(right, text="♥ 收藏到本地", command=self.fav_current).pack(fill="x", pady=3)
        ttk.Button(right, text="🔗 查看出处", command=self.open_page).pack(fill="x", pady=3)

        ttk.Separator(right, orient="horizontal").pack(fill="x", pady=8)
        ttk.Label(right, text="自动换壁纸", font=("Microsoft YaHei", 11, "bold")).pack(anchor="w")
        af = ttk.Frame(right)
        af.pack(fill="x", pady=2)
        self.interval_var = tk.IntVar(value=self.settings.get("interval", 10))
        ttk.Label(af, text="间隔(分钟)").pack(side="left")
        ttk.Spinbox(af, from_=1, to=120, width=6,
                    textvariable=self.interval_var).pack(side="left", padx=4)
        self.auto_btn = ttk.Button(right, text="▶ 开始自动轮换", command=self.toggle_auto)
        self.auto_btn.pack(fill="x", pady=3)

        # 状态栏
        self.status_var = tk.StringVar(value="就绪")
        ttk.Label(self.root, textvariable=self.status_var, relief="sunken",
                  anchor="w", padding=4).pack(side="bottom", fill="x")

    def _show_pil_hint(self):
        if not HAS_PIL:
            self.status_var.set("提示: 未检测到 Pillow，缩略图无法显示。"
                                "请运行 `pip install pillow` 后重启。")
        else:
            self.status_var.set("就绪")

    # ---------------- 搜索 ----------------
    def search_tag(self, tag: str):
        self.query_var.set(tag)
        # 地名标签强制用关键词源（除非当前是收藏/Bing）
        if self.source_var.get() in ("Bing 每日美图", "Picsum 随机美图", "我的收藏 (本地)"):
            self.source_var.set("Wikimedia 地名搜图")
        self.do_search()

    def do_search(self):
        src = self.source_var.get()
        query = self.query_var.get().strip()
        self.settings["source"] = src
        self.settings["last_query"] = query
        self.save_settings()
        self.status_var.set(f"正在从【{src}】获取…")
        self.scroll_frame.clear()
        self.photo_refs.clear()
        self.items = []
        threading.Thread(target=self._fetch_thread, args=(src, query),
                         daemon=True).start()

    def _fetch_thread(self, src: str, query: str):
        if src == "我的收藏 (本地)":
            items = [WallpaperItem.from_dict(f) for f in self.favorites]
            err = None
        else:
            items, err = sources.fetch(src, query, limit=24)
        self.root.after(0, self._on_fetched, items, err)

    def _on_fetched(self, items, err):
        if err:
            self.status_var.set(f"获取失败: {err}")
            messagebox.showwarning("获取失败", str(err))
            return
        if not items:
            self.status_var.set("没有找到图片，换个关键词或来源试试。")
            return
        self.items = items
        self._populate_grid(items)
        self.status_var.set(f"共 {len(items)} 张 · 来源【{items[0].source}】")

    # ---------------- 网格 ----------------
    def _populate_grid(self, items: list[WallpaperItem]):
        self.scroll_frame.clear()
        self.photo_refs.clear()
        inner = self.scroll_frame.inner
        for idx, item in enumerate(items):
            cell = ttk.Frame(inner, relief="groove", borderwidth=1)
            r, c = divmod(idx, COLS)
            cell.grid(row=r, column=c, padx=4, pady=4, sticky="nsew")
            img_label = tk.Label(cell, bg="#dfe3e8", width=CELL_W // 8, height=CELL_H // 14)
            img_label.pack(fill="both", expand=True)
            title_label = ttk.Label(cell, text=item.title[:18],
                                   anchor="center", font=("Microsoft YaHei", 8))
            title_label.pack(fill="x")
            cell.bind("<Button-1>", lambda e, i=idx: self.select(i))
            img_label.bind("<Button-1>", lambda e, i=idx: self.select(i))
            # 入队加载缩略图
            if HAS_PIL:
                self.load_q.put((item, img_label))
            else:
                img_label.configure(text="(无预览)")
        # 让网格列等宽
        for c in range(COLS):
            inner.columnconfigure(c, weight=1)

    def _loader_worker(self):
        while True:
            try:
                item, label = self.load_q.get(timeout=1)
            except queue.Empty:
                continue
            try:
                data = wc.download_to_cache(item.thumb_url, prefix="thumb")
                with open(data, "rb") as f:
                    raw = f.read()
                img = Image.open(io.BytesIO(raw)).convert("RGB")
                img.thumbnail((CELL_W, CELL_H))
                photo = ImageTk.PhotoImage(img)
                self.photo_refs.append(photo)
                self.root.after(0, lambda lb=label, ph=photo: lb.configure(image=ph))
            except Exception:  # noqa: BLE001
                self.root.after(0, lambda lb=label: lb.configure(text="加载失败"))
            finally:
                self.load_q.task_done()

    # ---------------- 选择 / 预览 ----------------
    def select(self, idx: int):
        if idx < 0 or idx >= len(self.items):
            return
        self.selected = idx
        item = self.items[idx]
        self.info_var.set(f"标题: {item.title}\n来源: {item.source}"
                          + (f"\n出处: {item.page_url}" if item.page_url else ""))
        self.status_var.set(f"已选择第 {idx + 1} 张：{item.title}")
        # 预览（用更大缩略图）
        if HAS_PIL:
            threading.Thread(target=self._preview_thread, args=(item,),
                             daemon=True).start()

    def _preview_thread(self, item: WallpaperItem):
        try:
            data = wc.download_to_cache(item.thumb_url, prefix="prev")
            with open(data, "rb") as f:
                raw = f.read()
            img = Image.open(io.BytesIO(raw)).convert("RGB")
            img.thumbnail((320, 220))
            photo = ImageTk.PhotoImage(img)
            self.photo_refs.append(photo)
            self.root.after(0, lambda ph=photo: self.preview.configure(image=ph, text=""))
        except Exception:  # noqa: BLE001
            self.root.after(0, lambda: self.preview.configure(text="预览失败"))

    # ---------------- 操作 ----------------
    def set_current(self):
        if self.selected is None:
            messagebox.showinfo("提示", "请先在左侧选择一张图片。")
            return
        item = self.items[self.selected]
        self.status_var.set(f"正在下载并设为壁纸：{item.title} …")
        self.root.update_idletasks()
        threading.Thread(target=self._set_thread, args=(item,), daemon=True).start()

    def _set_thread(self, item: WallpaperItem):
        try:
            ok, local = wc.set_wallpaper_from_url(item.full_url)
        except Exception as e:  # noqa: BLE001
            self.root.after(0, lambda: self.status_var.set(f"设置失败: {e}"))
            return
        if ok:
            self.root.after(0, lambda: self.status_var.set(f"✅ 已设为壁纸：{item.title}"))
        else:
            self.root.after(0, lambda: self.status_var.set(
                "设置失败（仅支持 Windows；请确认程序以普通权限运行）。"))

    def download_current(self):
        if self.selected is None:
            messagebox.showinfo("提示", "请先选择一张图片。")
            return
        item = self.items[self.selected]
        dest = filedialog.asksaveasfilename(
            defaultextension=".jpg",
            initialfile=wc._safe_filename(item.title, "jpg"),
            filetypes=[("图片", "*.jpg *.png"), ("所有文件", "*.*")])
        if not dest:
            return
        self.status_var.set(f"正在下载原图：{item.title} …")
        threading.Thread(target=self._download_thread, args=(item, dest),
                         daemon=True).start()

    def _download_thread(self, item: WallpaperItem, dest: str):
        try:
            wc.download(item.full_url, dest, timeout=120)
            self.root.after(0, lambda: self.status_var.set(f"✅ 已保存：{dest}"))
            self.root.after(0, lambda: messagebox.showinfo("完成", f"已保存原图：\n{dest}"))
        except Exception as e:  # noqa: BLE001
            self.root.after(0, lambda: self.status_var.set(f"下载失败: {e}"))

    def fav_current(self):
        if self.selected is None:
            messagebox.showinfo("提示", "请先选择一张图片。")
            return
        item = self.items[self.selected]
        # 下载到收藏目录
        try:
            local = os.path.join(wc.favorites_dir(),
                                 wc._safe_filename(item.title, "jpg"))
            wc.download(item.full_url, local, timeout=120)
        except Exception as e:  # noqa: BLE001
            messagebox.showerror("收藏失败", str(e))
            return
        rec = item.to_dict()
        rec["local"] = local
        # 去重
        if not any(f.get("full_url") == item.full_url for f in self.favorites):
            self.favorites.append(rec)
            self.save_favorites()
            self.status_var.set(f"♥ 已收藏：{item.title}（共 {len(self.favorites)} 张）")
        else:
            self.status_var.set("该图片已在收藏中。")

    def open_page(self):
        if self.selected is None or not self.items[self.selected].page_url:
            messagebox.showinfo("提示", "该图片没有可访问的出处链接。")
            return
        import webbrowser
        webbrowser.open(self.items[self.selected].page_url)

    def open_favorites_folder(self):
        import webbrowser
        path = wc.favorites_dir()
        webbrowser.open("file:///" + path.replace("\\", "/"))

    # ---------------- 自动轮换 ----------------
    def toggle_auto(self):
        if self.auto_timer is not None:
            self._stop_auto()
            return
        if not self.items:
            messagebox.showinfo("提示", "请先搜索并获取一批图片，再开启自动轮换。")
            return
        self.auto_stop.clear()
        self.auto_index = 0
        self.auto_btn.configure(text="■ 停止自动轮换")
        self.status_var.set("自动轮换已开启…")
        self._schedule_auto()

    def _schedule_auto(self):
        minutes = max(1, self.interval_var.get())
        self.auto_timer = self.root.after(minutes * 60 * 1000, self._auto_tick)

    def _auto_tick(self):
        if self.auto_stop.is_set():
            return
        if not self.items:
            self._stop_auto()
            return
        item = self.items[self.auto_index % len(self.items)]
        self.auto_index += 1
        self.status_var.set(f"自动轮换：{item.title}")
        try:
            wc.set_wallpaper_from_url(item.full_url)
        except Exception:  # noqa: BLE001
            pass
        self._schedule_auto()

    def _stop_auto(self):
        if self.auto_timer is not None:
            self.root.after_cancel(self.auto_timer)
            self.auto_timer = None
        self.auto_stop.set()
        self.auto_btn.configure(text="▶ 开始自动轮换")
        self.status_var.set("自动轮换已停止。")

    # ---------------- 持久化 ----------------
    def load_settings(self):
        try:
            with open(SETTINGS_PATH, "r", encoding="utf-8") as f:
                self.settings = json.load(f)
        except Exception:  # noqa: BLE001
            self.settings = {}

    def save_settings(self):
        try:
            with open(SETTINGS_PATH, "w", encoding="utf-8") as f:
                json.dump(self.settings, f, ensure_ascii=False, indent=2)
        except Exception:  # noqa: BLE001
            pass

    def load_favorites(self):
        try:
            with open(FAV_PATH, "r", encoding="utf-8") as f:
                self.favorites = json.load(f)
        except Exception:  # noqa: BLE001
            self.favorites = []

    def save_favorites(self):
        try:
            with open(FAV_PATH, "w", encoding="utf-8") as f:
                json.dump(self.favorites, f, ensure_ascii=False, indent=2)
        except Exception:  # noqa: BLE001
            pass

    def on_close(self):
        self._stop_auto()
        self.save_settings()
        self.root.destroy()


def main():
    root = tk.Tk()
    app = WallpaperApp(root)
    root.protocol("WM_DELETE_WINDOW", app.on_close)
    root.mainloop()


if __name__ == "__main__":
    main()
