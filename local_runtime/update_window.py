"""Tk update UI. Network and disk work never runs on the Tk thread."""
import os
import json
import queue
import subprocess
import threading
import tkinter as tk
from tkinter import messagebox, ttk

from .online_update import OnlineUpdater, ensure_idle


def show_updates(parent, app, data):
    existing = getattr(parent, '_update_window', None)
    if existing is not None and existing.winfo_exists():
        existing.lift()
        return
    window = tk.Toplevel(parent)
    parent._update_window = window
    window.title('AI Live Studio · 更新')
    window.geometry('560x360')
    busy = False
    def close():
        if busy:
            messagebox.showinfo('更新进行中', '请等待当前检查或下载完成，再关闭窗口。', parent=window)
            return
        parent.destroy() if parent.state() == 'withdrawn' else window.destroy()
    window.protocol('WM_DELETE_WINDOW', close)
    text = tk.StringVar(value='正在读取安装版本…')
    ttk.Label(window, textvariable=text, wraplength=520).pack(padx=20, pady=16)
    notes = tk.Text(window, height=8, wrap='word')
    notes.pack(fill='both', expand=True, padx=20)
    bar = ttk.Progressbar(window, maximum=100)
    bar.pack(fill='x', padx=20, pady=12)
    button = ttk.Button(window, text='检查更新')
    button.pack(pady=12)
    events = queue.Queue()
    updater = None
    downloaded = None

    def run(action, complete):
        nonlocal busy
        busy = True
        button.configure(state='disabled')
        def worker():
            try:
                events.put((complete, action()))
            except Exception as exc:
                events.put(('error', str(exc)))
        threading.Thread(target=worker, daemon=True).start()

    def check():
        nonlocal updater
        updater = OnlineUpdater(app, data)
        return updater.check()

    def download():
        run(lambda: updater.download(lambda done, total: events.put(('progress', done * 100 / total))), 'downloaded')

    def install():
        if not messagebox.askokcancel('安装更新', '请先保存项目，并结束直播、录音和泛化任务。\n安装将关闭服务并请求管理员权限。继续吗？', parent=window):
            return
        def prepare():
            ensure_idle(app, data)
            return updater.unpack(downloaded)
        run(prepare, 'unpacked')

    def poll():
        nonlocal downloaded, busy
        try:
            while True:
                kind, value = events.get_nowait()
                if kind != 'progress':
                    busy = False
                if kind == 'progress':
                    bar['value'] = value
                elif kind == 'checked':
                    current = updater.current
                    text.set(f'当前：{current.get("version", current["commit"][:7])} · {current["commit"][:7]}')
                    notes.delete('1.0', 'end')
                    if value:
                        p = updater.package
                        notes.insert('end', f'新版本：{value["version"]}\n{p["kind"]} · {p["size"] / 1024**2:.1f} MB\n\n{value["notes"]}')
                        button.configure(text='下载更新', command=download, state='normal')
                    else:
                        notes.insert('end', '当前已是此更新源的最新版本。')
                        button.configure(text='重新检查', command=lambda: run(check, 'checked'), state='normal')
                elif kind == 'downloaded':
                    downloaded = value
                    text.set('下载和校验完成。安装前请保存工作。')
                    button.configure(text='安装并重启', command=install, state='normal')
                elif kind == 'unpacked':
                    if updater.package['kind'] == 'patch':
                        command = ['powershell.exe', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File',
                                   str(value / 'Apply-Patch.ps1'), '-InstallDir', str(app), '-DataDir', str(data),
                                   '-WaitForProcessId', str(os.getpid())]
                    try:
                        if updater.package['kind'] == 'patch':
                            subprocess.Popen(command, cwd=value)
                        else:
                            import ctypes
                            result = ctypes.windll.shell32.ShellExecuteW(None, 'runas',
                                str(value / 'AI-Live-Studio-Windows-Test-Setup.exe'), None, str(value), 1)
                            if result <= 32:
                                raise OSError('安装器未启动或管理员授权已取消')
                    except OSError as exc:
                        events.put(('error', str(exc)))
                    else:
                        # Release the launcher executable so the external updater can replace it.
                        parent.destroy()
                        return
                elif kind == 'error':
                    text.set(value)
                    button.configure(text='重试检查', command=lambda: run(check, 'checked'), state='normal')
        except queue.Empty:
            pass
        if window.winfo_exists():
            window.after(100, poll)

    button.configure(command=lambda: run(check, 'checked'))
    run(check, 'checked')
    window.after(100, poll)
