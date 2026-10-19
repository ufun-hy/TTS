"""Windows desktop UI: embedded WebView2 window and persistent tray controller.

The runtime is independent of this UI process. Closing the window hides it;
only an explicit tray exit action requests runtime shutdown.
"""
from __future__ import annotations

import threading
from typing import Callable


def run_desktop(url: str, stop_runtime: Callable[[], bool]) -> None:
    try:
        import webview
        import pystray
        from PIL import Image, ImageDraw
    except ImportError as exc:
        raise RuntimeError("桌面组件缺失；请重新安装完整的 Windows 客户端。") from exc

    stopping = threading.Event()
    window = webview.create_window(
        "AI Live Studio", url, width=1280, height=820,
        min_size=(900, 600), background_color="#FFFFFF",
    )

    def on_close() -> bool:
        if not stopping.is_set():
            window.hide()
            return False  # pywebview cancels closing only on False.
        return True

    window.events.closing += on_close

    icon_image = Image.new("RGB", (64, 64), "#103C62")
    draw = ImageDraw.Draw(icon_image)
    draw.rounded_rectangle((10, 10, 54, 54), radius=12, fill="#FFFFFF")
    draw.rectangle((27, 19, 37, 45), fill="#103C62")

    def show_window(icon=None, item=None) -> None:
        window.show()

    def exit_program(icon=None, item=None) -> None:
        # Keep the tray and running task intact if the runtime refuses to stop.
        if not stop_runtime():
            return
        stopping.set()
        icon.stop()
        window.destroy()

    tray = pystray.Icon(
        "AI Live Studio", icon_image, "AI Live Studio",
        menu=pystray.Menu(
            pystray.MenuItem("打开工作台", show_window, default=True),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem(
                "退出程序（停止后台服务）",
                pystray.Menu(
                    pystray.MenuItem("确认退出并停止后台", exit_program),
                    pystray.MenuItem("取消", lambda icon, item: None),
                ),
            ),
        ),
    )
    tray.run_detached()
    try:
        webview.start(gui="edgechromium", debug=False)
    finally:
        tray.stop()
