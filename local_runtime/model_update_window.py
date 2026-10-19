"""Model updates run independently from application updates."""
import json
import os
from pathlib import Path
import queue
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from .model_install import ModelInstaller, set_models_directory
from .online_update import ensure_idle


def _model_root(data):
    try:
        value = json.loads((Path(data) / 'config/models-path.json').read_text(encoding='utf-8'))
        selected = value.get('models_dir', '') if isinstance(value, dict) else ''
        if selected:
            return Path(selected).expanduser()
    except (OSError, ValueError):
        pass
    return Path(os.environ.get('AI_LIVE_STUDIO_MODELS', 'D:/AI-Live-Studio-Models')).expanduser()


def show_models(parent, app, data):
    existing = getattr(parent, '_models_window', None)
    if existing is not None and existing.winfo_exists():
        existing.lift()
        return
    window = tk.Toplevel(parent)
    parent._models_window = window
    window.title('AI Live Studio · 模型')
    window.geometry('560x320')
    busy = False
    events = queue.Queue()
    installer = None
    selected = tk.StringVar(value=str(_model_root(data)))
    status = tk.StringVar(value='模型版本独立于程序更新。')
    ttk.Label(window, textvariable=status, wraplength=520).pack(padx=18, pady=12)
    row = ttk.Frame(window)
    row.pack(fill='x', padx=18)
    ttk.Label(row, text='模型目录').pack(side='left')
    ttk.Entry(row, textvariable=selected).pack(side='left', fill='x', expand=True, padx=8)
    ttk.Button(row, text='选择…', command=lambda: choose()).pack(side='right')
    details = tk.Text(window, height=7, wrap='word', state='disabled')
    details.pack(fill='both', expand=True, padx=18, pady=8)
    bar = ttk.Progressbar(window, maximum=100)
    bar.pack(fill='x', padx=18, pady=8)
    button = ttk.Button(window, text='检查模型更新')
    button.pack(pady=(0, 14))

    def choose():
        folder = filedialog.askdirectory(title='选择模型目录', parent=window)
        if folder:
            selected.set(folder)

    def close():
        if busy:
            messagebox.showinfo('更新进行中', '请等待当前操作完成。', parent=window)
            return
        window.destroy()
    window.protocol('WM_DELETE_WINDOW', close)

    def run(action, kind):
        nonlocal busy
        busy = True
        button.configure(state='disabled')
        def worker():
            try:
                events.put((kind, action()))
            except Exception as exc:
                events.put(('error', str(exc)))
        threading.Thread(target=worker, daemon=True).start()

    def check(model_dir):
        nonlocal installer
        installer = ModelInstaller(model_dir, data, Path(app) / 'config/model-source.json')
        return installer.check(), installer.status()

    def install():
        if not installer or Path(selected.get()).expanduser().resolve() != installer.models_dir.resolve():
            messagebox.showinfo('重新检查模型目录', '模型目录已更改，请先重新检查版本。', parent=window)
            start_check()
            return
        if not messagebox.askokcancel('更新模型',
                '模型文件会在下载校验后安装。请先结束直播、生成、播放和录音任务。继续吗？', parent=window):
            return
        def action():
            ensure_idle(app, data)
            result = installer.install(lambda done, total: events.put(('progress', done * 100 / total)))
            set_models_directory(data, installer.models_dir)
            return result
        run(action, 'installed')

    def start_check():
        model_dir = selected.get()
        run(lambda: check(model_dir), 'checked')
    button.configure(command=start_check)

    def show(text):
        details.configure(state='normal')
        details.delete('1.0', 'end')
        details.insert('end', text)
        details.configure(state='disabled')

    def poll():
        nonlocal busy
        try:
            while True:
                kind, value = events.get_nowait()
                if kind != 'progress':
                    busy = False
                if kind == 'progress':
                    bar['value'] = value
                elif kind == 'checked':
                    manifest, state = value
                    status.set(f'模型 {manifest["id"]} · 版本 {manifest["version"]}')
                    show(f'版本序号：{manifest["sequence"]}\n'
                         f'文件：{len(manifest["files"])} 项；可复用 {len(state["reused"])} 项；'
                         f'需下载/更新 {len(state["missing"])+len(state["changed"])} 项。\n'
                         f'清单 SHA256：{installer.envelope_hash}\n目录：{selected.get()}')
                    button.configure(text='下载并安装模型', command=install, state='normal')
                elif kind == 'installed':
                    status.set(f'模型 {value["version"]} 已安装')
                    show(f'安装文件：{value["installed"]} 项\n复用文件：{value["reused"]} 项\n'
                         f'清单 SHA256：{value["manifest_sha256"]}\n目录：{selected.get()}')
                    button.configure(text='重新检查', command=start_check, state='normal')
                elif kind == 'error':
                    status.set('模型操作失败')
                    show(value)
                    button.configure(text='重试检查', command=start_check, state='normal')
                    messagebox.showerror('模型更新失败', value, parent=window)
        except queue.Empty:
            pass
        if window.winfo_exists():
            window.after(100, poll)
    window.after(100, poll)
