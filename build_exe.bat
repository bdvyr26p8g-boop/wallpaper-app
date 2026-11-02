@echo off
REM 从源码构建单文件 exe（需要 Python 3.8+，且该 Python 自带 tkinter）
cd /d "%~dp0"

echo [1/3] 安装打包依赖...
python -m pip install --quiet --upgrade pip >nul 2>&1
python -m pip install --quiet pillow pyinstaller
if errorlevel 1 (
    echo 依赖安装失败，请检查网络或 Python 环境。
    pause & exit /b 1
)

echo [2/3] 打包 exe...
python -m PyInstaller --noconfirm --clean --onefile --windowed ^
    --name WallpaperApp --icon assets\app.ico app.py
if errorlevel 1 (
    echo 打包失败。
    pause & exit /b 1
)

echo [3/3] 完成！
echo.
echo 产物： %CD%\dist\WallpaperApp.exe
echo 该 exe 为单文件、双击即用，无需安装 Python。
pause
