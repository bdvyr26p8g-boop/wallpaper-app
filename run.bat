@echo off
REM 免费壁纸软件启动脚本（Windows）
cd /d "%~dp0"

REM 若未安装 Pillow（用于显示缩略图），尝试自动安装
python -c "import PIL" >nul 2>&1
if errorlevel 1 (
    echo 未检测到 Pillow，正在尝试安装（用于显示缩略图）...
    python -m pip install --quiet pillow >nul 2>&1
)

REM 选择解释器：优先 py 启动器，其次 python
set RUNNER=
py -3 --version >nul 2>&1 && set RUNNER=py -3
if not defined RUNNER python --version >nul 2>&1 && set RUNNER=python

if not defined RUNNER (
    echo 未找到 Python。请先安装 Python 3.8+ 并勾选 "Add to PATH"。
    pause
    exit /b 1
)

start "" %RUNNER% app.py
