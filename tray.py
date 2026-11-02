"""Windows 系统托盘支持（纯 ctypes 实现，不依赖任何第三方库）。

设计要点：
  - 在**独立线程**里创建隐藏的 Win32 窗口并跑消息循环，用 Shell_NotifyIconW
    把图标挂到任务栏右下角。绝不在托盘线程里直接调用 tkinter。
  - 用户点击图标 / 右键菜单产生的动作先写入 `actions` 队列，
    由 tkinter 主线程用 after() 定时读取后执行 —— 线程安全。
  - 关闭窗口只是隐藏（root.withdraw()），tkinter 的 mainloop 仍在运行，
    所以定时换壁纸等 after() 任务照常继续，实现真正的后台运行。
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as w
import os
import queue
import sys
import threading
import time

IS_WINDOWS = sys.platform.startswith("win")

# ---- 常量 ----
WM_DESTROY = 0x0002
WM_CLOSE = 0x0010
WM_COMMAND = 0x0111
WM_LBUTTONUP = 0x0202
WM_LBUTTONDBLCLK = 0x0203
WM_RBUTTONUP = 0x0205
WM_APP = 0x8000
WM_TRAY_CALLBACK = WM_APP + 1          # Shell_NotifyIcon 回调消息

NIM_ADD = 0x00000000
NIM_MODIFY = 0x00000001
NIM_DELETE = 0x00000002
NIF_MESSAGE = 0x00000001
NIF_ICON = 0x00000002
NIF_TIP = 0x00000004
NIF_INFO = 0x00000010
NIIF_INFO = 0x00000001
NIIF_USER = 0x00000004

IMAGE_ICON = 1
LR_LOADFROMFILE = 0x00000010
LR_DEFAULTSIZE = 0x00000040
IDI_APPLICATION = 32512

MF_STRING = 0x0000
MF_SEPARATOR = 0x0800
TPM_RETURNCMD = 0x0100
TPM_LEFTALIGN = 0x0000
TPM_RIGHTBUTTON = 0x0002
TPM_NONOTIFY = 0x0080

PM_REMOVE = 0x0001

# 菜单项 ID
CMD_SHOW = 1001
CMD_NEXT = 1002
CMD_AUTO = 1003
CMD_FAV = 1004
CMD_QUIT = 1009


class GUID(ctypes.Structure):
    _fields_ = [("Data1", w.DWORD), ("Data2", w.WORD), ("Data3", w.WORD),
                ("Data4", ctypes.c_ubyte * 8)]


class NOTIFYICONDATAW(ctypes.Structure):
    _fields_ = [
        ("cbSize", w.DWORD),
        ("hWnd", w.HWND),
        ("uID", w.UINT),
        ("uFlags", w.UINT),
        ("uCallbackMessage", w.UINT),
        ("hIcon", w.HICON),
        ("szTip", w.WCHAR * 128),
        ("dwState", w.DWORD),
        ("dwStateMask", w.DWORD),
        ("szInfo", w.WCHAR * 256),
        ("uVersion", w.UINT),
        ("szInfoTitle", w.WCHAR * 64),
        ("dwInfoFlags", w.DWORD),
        ("guidItem", GUID),
        ("hBalloonIcon", w.HICON),
    ]


class WNDCLASSEXW(ctypes.Structure):
    _fields_ = [
        ("cbSize", w.UINT),
        ("style", w.UINT),
        ("lpfnWndProc", ctypes.c_void_p),
        ("cbClsExtra", ctypes.c_int),
        ("cbWndExtra", ctypes.c_int),
        ("hInstance", w.HINSTANCE),
        ("hIcon", w.HICON),
        ("hCursor", w.HANDLE),
        ("hbrBackground", w.HBRUSH),
        ("lpszMenuName", w.LPCWSTR),
        ("lpszClassName", w.LPCWSTR),
        ("hIconSm", w.HICON),
    ]


WNDPROC = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, w.HWND, w.UINT, w.WPARAM, w.LPARAM)

user32 = ctypes.windll.user32 if IS_WINDOWS else None
shell32 = ctypes.windll.shell32 if IS_WINDOWS else None
kernel32 = ctypes.windll.kernel32 if IS_WINDOWS else None

if IS_WINDOWS:
    user32.DefWindowProcW.restype = ctypes.c_ssize_t
    user32.DefWindowProcW.argtypes = [w.HWND, w.UINT, w.WPARAM, w.LPARAM]
    user32.CreateWindowExW.restype = w.HWND
    user32.CreateWindowExW.argtypes = [
        w.DWORD, w.LPCWSTR, w.LPCWSTR, w.DWORD,
        ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
        w.HWND, w.HMENU, w.HINSTANCE, w.LPVOID]
    user32.RegisterClassExW.argtypes = [ctypes.POINTER(WNDCLASSEXW)]
    user32.LoadImageW.restype = w.HANDLE
    user32.LoadImageW.argtypes = [w.HINSTANCE, w.LPCWSTR, w.UINT,
                                  ctypes.c_int, ctypes.c_int, w.UINT]
    user32.LoadIconW.restype = w.HICON
    user32.LoadIconW.argtypes = [w.HINSTANCE, ctypes.c_void_p]
    user32.CreatePopupMenu.restype = w.HMENU
    user32.AppendMenuW.argtypes = [w.HMENU, w.UINT, ctypes.c_size_t, w.LPCWSTR]
    user32.TrackPopupMenu.restype = ctypes.c_int
    user32.TrackPopupMenu.argtypes = [w.HMENU, w.UINT, ctypes.c_int, ctypes.c_int,
                                      ctypes.c_int, w.HWND, w.LPVOID]
    user32.DestroyMenu.argtypes = [w.HMENU]
    user32.GetCursorPos.argtypes = [ctypes.POINTER(w.POINT)]
    user32.SetForegroundWindow.argtypes = [w.HWND]
    user32.PostMessageW.argtypes = [w.HWND, w.UINT, w.WPARAM, w.LPARAM]
    user32.PeekMessageW.argtypes = [ctypes.POINTER(w.MSG), w.HWND, w.UINT, w.UINT, w.UINT]
    user32.DispatchMessageW.argtypes = [ctypes.POINTER(w.MSG)]
    user32.TranslateMessage.argtypes = [ctypes.POINTER(w.MSG)]
    shell32.Shell_NotifyIconW.argtypes = [w.DWORD, ctypes.POINTER(NOTIFYICONDATAW)]
    kernel32.GetModuleHandleW.restype = w.HINSTANCE


def load_app_icon(path: str | None = None):
    """加载托盘图标：优先 ico 文件（源码运行），打包后从 exe 自身提取。"""
    if not IS_WINDOWS:
        return None
    # 1) 显式 ico 文件
    cands = []
    if path:
        cands.append(path)
    here = os.path.dirname(os.path.abspath(__file__))
    cands.append(os.path.join(here, "assets", "app.ico"))
    base = getattr(sys, "_MEIPASS", None)
    if base:
        cands.append(os.path.join(base, "assets", "app.ico"))
    for p in cands:
        if p and os.path.exists(p):
            h = user32.LoadImageW(None, p, IMAGE_ICON, 32, 32,
                                  LR_LOADFROMFILE | LR_DEFAULTSIZE)
            if h:
                return h
    # 2) 从当前 exe 提取图标（PyInstaller 打包后 exe 自带图标）
    if getattr(sys, "frozen", False):
        small = w.HICON()
        large = w.HICON()
        try:
            n = shell32.ExtractIconExW(sys.executable, 0, ctypes.byref(large),
                                       ctypes.byref(small), 1)
            if n and large.value:
                return large
            if n and small.value:
                return small
        except Exception:  # noqa: BLE001
            pass
    # 3) 系统默认图标兜底
    return user32.LoadIconW(None, ctypes.c_void_p(IDI_APPLICATION))


class TrayIcon:
    """托盘图标 + 右键菜单。actions 队列中可能出现：
    'show'（显示窗口）/ 'hide'（隐藏窗口）/ 'toggle'（切换显示隐藏）
    'next'（立即换一张）/ 'auto'（自动轮换开关）/ 'favorites' / 'quit'
    """

    def __init__(self, icon_path: str | None = None, tooltip: str = "免费壁纸"):
        self.actions: queue.Queue[str] = queue.Queue()
        self.tooltip = tooltip
        self.icon_path = icon_path
        self._auto_on = False
        self._hwnd = None
        self._hicon = None
        self._nid = NOTIFYICONDATAW()
        self._thread: threading.Thread | None = None
        self._alive = False
        self._ready = threading.Event()
        self.ok = False                    # 托盘是否真的挂上了
        self._wndproc = None               # 必须持有引用，否则回调被 GC
        self._lock = threading.Lock()

    # ---------- 生命周期 ----------
    def start(self, timeout: float = 3.0) -> bool:
        if not IS_WINDOWS:
            return False
        self._alive = True
        self._thread = threading.Thread(target=self._run, name="tray", daemon=True)
        self._thread.start()
        self._ready.wait(timeout)
        self.ok = self._hwnd is not None
        return self.ok

    def stop(self):
        self._alive = False
        hwnd = self._hwnd
        if hwnd and IS_WINDOWS:
            try:
                user32.PostMessageW(hwnd, WM_CLOSE, 0, 0)
            except Exception:  # noqa: BLE001
                pass
        self._hwnd = None

    # ---------- 对外操作（线程安全：Windows 允许跨线程调用 Shell_NotifyIcon） ----------
    def set_tip(self, text: str):
        self.tooltip = (text or "免费壁纸")[:120]
        self._modify()

    def set_auto_state(self, on: bool):
        self._auto_on = bool(on)

    def notify(self, title: str, body: str, timeout_ms: int = 4000):
        """弹出托盘气泡提示（Win10 会显示为通知）。"""
        if not self.ok:
            return
        nid = self._copy_nid()
        nid.uFlags = NIF_INFO
        nid.szInfoTitle = (title or "")[:63]
        nid.szInfo = (body or "")[:255]
        nid.dwInfoFlags = NIIF_INFO
        nid.uVersion = timeout_ms
        try:
            shell32.Shell_NotifyIconW(NIM_MODIFY, ctypes.byref(nid))
        except Exception:  # noqa: BLE001
            pass

    # ---------- 内部 ----------
    def _copy_nid(self) -> NOTIFYICONDATAW:
        nid = NOTIFYICONDATAW()
        nid.cbSize = ctypes.sizeof(NOTIFYICONDATAW)
        nid.hWnd = self._nid.hWnd
        nid.uID = 1
        nid.uCallbackMessage = WM_TRAY_CALLBACK
        nid.hIcon = self._nid.hIcon
        nid.szTip = self.tooltip[:127]
        return nid

    def _modify(self):
        if not self.ok:
            return
        try:
            nid = self._copy_nid()
            nid.uFlags = NIF_TIP | NIF_ICON | NIF_MESSAGE
            shell32.Shell_NotifyIconW(NIM_MODIFY, ctypes.byref(nid))
        except Exception:  # noqa: BLE001
            pass

    def _run(self):
        try:
            hinst = kernel32.GetModuleHandleW(None)
            class_name = f"WallpaperAppTray_{os.getpid()}"
            self._hicon = load_app_icon(self.icon_path)

            @WNDPROC
            def _proc(hwnd, msg, wparam, lparam):
                try:
                    if msg == WM_TRAY_CALLBACK:
                        low = lparam & 0xFFFF
                        if low in (WM_LBUTTONUP, WM_LBUTTONDBLCLK):
                            self.actions.put("toggle")
                        elif low == WM_RBUTTONUP:
                            self._popup(hwnd)
                        return 0
                    if msg == WM_CLOSE:
                        user32.DestroyWindow(hwnd)
                        return 0
                    if msg == WM_DESTROY:
                        self._remove_icon()
                        user32.PostQuitMessage(0)
                        return 0
                except Exception:  # noqa: BLE001
                    pass
                return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

            self._wndproc = _proc

            wc = WNDCLASSEXW()
            wc.cbSize = ctypes.sizeof(WNDCLASSEXW)
            wc.lpfnWndProc = ctypes.cast(_proc, ctypes.c_void_p)
            wc.hInstance = hinst
            wc.lpszClassName = class_name
            wc.hIcon = self._hicon
            wc.hIconSm = self._hicon
            if not user32.RegisterClassExW(ctypes.byref(wc)):
                self._ready.set()
                return

            hwnd = user32.CreateWindowExW(0, class_name, "WallpaperAppTray", 0,
                                          0, 0, 0, 0, None, None, hinst, None)
            if not hwnd:
                self._ready.set()
                return
            self._hwnd = hwnd

            nid = self._copy_nid()
            nid.hWnd = hwnd
            nid.hIcon = self._hicon
            nid.uFlags = NIF_MESSAGE | NIF_ICON | NIF_TIP
            ok = shell32.Shell_NotifyIconW(NIM_ADD, ctypes.byref(nid))
            self._nid = nid
            self.ok = bool(ok)
            self._ready.set()
            if not ok:
                return

            msg = w.MSG()
            while self._alive:
                if user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, PM_REMOVE):
                    if msg.message == 0x0012:   # WM_QUIT
                        break
                    user32.TranslateMessage(ctypes.byref(msg))
                    user32.DispatchMessageW(ctypes.byref(msg))
                else:
                    time.sleep(0.02)
        except Exception:  # noqa: BLE001
            self.ok = False
        finally:
            self._remove_icon()
            self._ready.set()

    def _remove_icon(self):
        try:
            if self.ok:
                shell32.Shell_NotifyIconW(NIM_DELETE, ctypes.byref(self._nid))
                self.ok = False
        except Exception:  # noqa: BLE001
            pass

    def _popup(self, hwnd):
        menu = user32.CreatePopupMenu()
        if not menu:
            return
        try:
            user32.AppendMenuW(menu, MF_STRING, CMD_SHOW, "显示主窗口")
            user32.AppendMenuW(menu, MF_STRING, CMD_NEXT, "立即换一张壁纸")
            auto_txt = "暂停自动轮换" if self._auto_on else "开启自动轮换"
            user32.AppendMenuW(menu, MF_STRING, CMD_AUTO, auto_txt)
            user32.AppendMenuW(menu, MF_SEPARATOR, 0, None)
            user32.AppendMenuW(menu, MF_STRING, CMD_FAV, "打开收藏目录")
            user32.AppendMenuW(menu, MF_SEPARATOR, 0, None)
            user32.AppendMenuW(menu, MF_STRING, CMD_QUIT, "退出（停止换壁纸）")

            pt = w.POINT()
            user32.GetCursorPos(ctypes.byref(pt))
            # 必须先置前台，否则菜单点外面不会消失
            user32.SetForegroundWindow(hwnd)
            cmd = user32.TrackPopupMenu(
                menu, TPM_RETURNCMD | TPM_LEFTALIGN | TPM_RIGHTBUTTON | TPM_NONOTIFY,
                pt.x, pt.y, 0, hwnd, None)
            user32.PostMessageW(hwnd, 0, 0, 0)
            mapping = {CMD_SHOW: "show", CMD_NEXT: "next", CMD_AUTO: "auto",
                       CMD_FAV: "favorites", CMD_QUIT: "quit"}
            act = mapping.get(cmd)
            if act:
                self.actions.put(act)
        finally:
            user32.DestroyMenu(menu)


def set_autostart(enable: bool, exe_path: str | None = None, args: str = "--tray") -> bool:
    """写 / 删 HKCU\\...\\Run 实现开机自启（普通权限即可，无需管理员）。"""
    if not IS_WINDOWS:
        return False
    import winreg
    key_path = r"Software\Microsoft\Windows\CurrentVersion\Run"
    name = "WallpaperApp"
    try:
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path, 0,
                             winreg.KEY_SET_VALUE | winreg.KEY_QUERY_VALUE)
    except OSError:
        return False
    try:
        if enable:
            target = exe_path or sys.executable
            if getattr(sys, "frozen", False):
                cmd = f'"{target}" {args}'
            else:                       # 源码运行时用 pythonw 静默启动
                pyw = os.path.join(os.path.dirname(target), "pythonw.exe")
                runner = pyw if os.path.exists(pyw) else target
                script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "app.py")
                cmd = f'"{runner}" "{script}" {args}'
            winreg.SetValueEx(key, name, 0, winreg.REG_SZ, cmd)
        else:
            try:
                winreg.DeleteValue(key, name)
            except FileNotFoundError:
                pass
        return True
    except OSError:
        return False
    finally:
        winreg.CloseKey(key)


def is_autostart_enabled() -> bool:
    if not IS_WINDOWS:
        return False
    import winreg
    try:
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                             r"Software\Microsoft\Windows\CurrentVersion\Run")
        try:
            winreg.QueryValueEx(key, "WallpaperApp")
            return True
        finally:
            winreg.CloseKey(key)
    except OSError:
        return False
