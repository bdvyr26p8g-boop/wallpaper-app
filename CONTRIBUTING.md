# 参与贡献

感谢你对 **WallpaperApp** 的兴趣！欢迎通过 Issue 反馈问题、通过 Pull Request 提交改进。

## 环境准备

1. 安装 Python 3.8+（Windows）。
2. 克隆仓库并安装依赖：

```bat
git clone <your-fork-url>
cd wallpaper_app
python -m pip install -r requirements.txt
```

3. 运行：

```bat
python app.py
```

## 提交 PR 的流程

1. Fork 本仓库并新建分支：`git checkout -b feat/your-feature`
2. 修改代码，确保程序可正常启动、核心功能可用。
3. 提交：`git commit -m "feat: 简要说明"`
4. 推送到你的 fork：`git push origin feat/your-feature`
5. 发起 Pull Request，并描述改动内容与测试方式。

## 代码约定

- 保持 Python 3.8+ 兼容（避免使用 3.10+ 专属语法）。
- 新增图源请放在 `sources.py`，实现统一返回 `list[WallpaperItem]` 的适配器，并做好异常容错。
- 网络请求一律在后台线程中进行，禁止阻塞 Tk 主线程。
- 不要提交 `settings.json` / `favorites.json` 等用户本地数据（已在 `.gitignore` 中排除）。

## 欢迎的方向

- 新增免费图源（NASA、Unsplash 官方 API、Pixabay 等）
- 分辨率筛选（仅 4K/1080p）、图片裁剪适配
- 系统托盘常驻、开机自启
- 多显示器 / 幻灯片模式
- UI 美化与文档、截图补充

## 版权

参与贡献即表示你同意你的代码以本项目的 [MIT License](LICENSE) 授权发布。
