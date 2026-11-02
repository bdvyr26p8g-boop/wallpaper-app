# 🖼️ WallpaperApp — Free & Open-Source Wallpaper App

<p align="center">
  <img src="docs/banner.svg" alt="WallpaperApp banner" width="100%">
</p>

<p align="center">
  <a href="README.md">中文</a> · <b>English</b>
</p>

> A completely free, open-source Windows desktop wallpaper app with **no paid API keys required**.
> It fetches beautiful photos in real time from public sources (Lugu Lake, Lijiang, Litang, Daocheng, snow mountains, ancient towns…), sets them as your desktop wallpaper in one click, and supports favorites plus scheduled rotation.

![CI](https://github.com/bdvyr26p8g-boop/wallpaper-app/actions/workflows/ci.yml/badge.svg)
![Python](https://img.shields.io/badge/Python-3.8%2B-blue)
![Platform](https://img.shields.io/badge/Platform-Windows-lightgrey)
![License](https://img.shields.io/badge/License-MIT-green)

---

## ✨ Features

- 🆓 **Free, no sign-up**: uses public, key-free APIs — Bing Daily, Wikimedia Commons, Openverse and Picsum. No paid quota, no API key.
- 🔍 **Place / keyword search**: built-in quick tags (Lugu Lake, Lijiang, Litang, Daocheng, Dali, Qinghai Lake, snow mountain, ancient town, grassland, starry sky, coast, ginkgo…) or type your own keywords.
- 🖼️ **Thumbnail grid**: multi-threaded async loading, smooth scrolling.
- ⚡ **One-click wallpaper**: downloads the original image and sets it as your Windows desktop wallpaper.
- ⬇️ **Download original / ♥ Favorite**: favorites are stored locally under `Pictures/WallpaperApp/favorites`.
- 🔄 **Scheduled rotation**: change wallpaper automatically every 1–120 minutes.
- 💾 **Persistence**: favorites, last source/keyword and rotation interval are saved.
- 🧩 **Pure Python stdlib + optional Pillow**: minimal dependencies, just double-click to run.

## 📸 Screenshot

<p align="center">
  <img src="docs/ui-mockup.svg" alt="WallpaperApp UI mockup" width="100%">
</p>

> The image above is a UI mockup. The actual interface may differ. Feel free to submit real screenshots to `docs/`.

## 🚀 Quick Start

### Option 0: Use the prebuilt exe (no install, recommended)

No Python needed — just download and double-click **`WallpaperApp.exe`**:

- Grab it from [Releases](https://github.com/bdvyr26p8g-boop/wallpaper-app/releases);
- Double-click, pick a source, click a keyword (e.g. "Lugu Lake") → **Search** → click an image → **★ Set as wallpaper**;
- To rotate automatically, set the **interval (minutes)** and click **▶ Start rotation**.

> If Windows SmartScreen or antivirus warns you, that's because the unsigned single-file exe is a common PyInstaller false positive — choose "More info → Run anyway".

### Requirements (running from source)

- Windows 10 / 11
- Python 3.8+ (tick **Add Python to PATH** during installation)
- Optional: Pillow (for thumbnails; `run.bat` will try to install it automatically)

### Option 1: Double-click (recommended)

Double-click **`run.bat`**. It will:

1. Detect and try to install Pillow;
2. Start the app with `py -3` or `python`.

### Option 2: Command line

```bat
:: Install dependency (only needed for thumbnails)
python -m pip install pillow

:: Run
python app.py
```

### Option 3: Build your own exe

Double-click **`build_exe.bat`**, or run:

```bat
python -m pip install pillow pyinstaller
python -m PyInstaller --onefile --windowed --name WallpaperApp --icon assets/app.ico app.py
```

The output is `dist\WallpaperApp.exe` — a single portable file you can share with people who don't have Python.

## 🖱️ Usage

1. Choose a **source** at the top, type or click a **keyword / place**, then click **Search**.
2. Click a thumbnail on the left to preview it on the right.
3. **★ Set as wallpaper** applies it immediately; **⬇ Download original** saves it; **♥ Favorite** adds it to your collection.
4. Set the **interval (minutes)** and click **▶ Start rotation** to change wallpaper automatically.
5. Click **📁 My favorites** to open the favorites folder.

## 🗂️ Project Structure

| File | Purpose |
|------|---------|
| `app.py` | GUI main program (tkinter) — UI and interaction |
| `sources.py` | Four key-free source adapters (Bing / Wikimedia / Openverse / Picsum) |
| `wallpaper_core.py` | Image download with cache, Windows wallpaper setting, cache/favorites dirs |
| `run.bat` | One-click launcher for Windows |
| `build_exe.bat` | Build a single-file exe (PyInstaller) |
| `assets/app.ico` | App icon |
| `docs/` | Banner and UI mockup |
| `.github/workflows/ci.yml` | CI: syntax, import and SVG checks |
| `requirements.txt` | Dependencies (Pillow) |
| `settings.json` / `favorites.json` | User settings & favorites (generated at runtime, git-ignored) |

Cache: `%USERPROFILE%\Pictures\WallpaperApp\cache`
Favorites: `%USERPROFILE%\Pictures\WallpaperApp\favorites`

## 🌐 Sources & Licensing

| Source | Key required | Notes |
|--------|--------------|-------|
| **Bing Daily** | No | Microsoft's daily homepage image, with copyright notice |
| **Wikimedia Commons** | No | Place-based search, mostly Creative Commons licensed |
| **Openverse** | No | Aggregates Creative Commons images, keyword search |
| **Picsum (Lorem Picsum)** | No | Random high-quality photos (from Unsplash etc.) |

> All images belong to their original authors. This app only provides search and download capabilities; **downloaded content is for personal desktop use only**. For public sharing or commercial use, follow the license of the corresponding source and verify it via **🔗 View source** in the app.

## ❓ FAQ

- **Thumbnails don't show / "Pillow not detected"?**
  Run `python -m pip install pillow` and restart.

- **"Set as wallpaper" does nothing?**
  Make sure you're on Windows; some "wallpaper-protection" security tools may block the system call — allow it.

- **A source fails to load?**
  Your network may not reach that site (Wikimedia / Openverse can be restricted on some networks). The app is fault-tolerant; switch source or retry — one failing source won't break the others.

- **No results for a place name?**
  Try **Openverse keyword search** or **Bing Daily**; some obscure places have few images on Commons.

## 🤝 Contributing

Issues and PRs are welcome! You can:

- Add more free sources (NASA, Unsplash official API, …);
- Add resolution filtering (4K only), system tray, launch-on-startup;
- Improve UI, docs or screenshots.

Please make sure the code runs correctly and stays consistent. See [CONTRIBUTING.md](CONTRIBUTING.md).

## 📄 License

Released under the [MIT License](LICENSE). Free to use, modify and distribute.
