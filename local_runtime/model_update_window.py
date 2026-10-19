"""Select and inspect user-supplied local models; V1 does not download models."""
import json
import os
from pathlib import Path
import queue
import threading
import tkinter as tk
from tkinter import filedialog, ttk

from .model_install import inspect_local_models, set_models_directory


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
    window.title('AI Live Studio · 本地模型')
    window.geometry('600x370')
    busy = False
    events = queue.Queue()
    selected = tk.StringVar(value=str(_model_root(data)))
    status = tk.StringVar(value='模型需由用户复制到本机；此窗口只检查本地文件。')
    ttk.Label(window, textvariable=status, wraplength=550).pack(padx=18, pady=12)
    row = ttk.Frame(window)
    row.pack(fill='x', padx=18)
    ttk.Label(row, text='模型目录').pack(side='left')
    ttk.Entry(row, textvariable=selected).pack(side='left', fill='x', expand=True, padx=8)
    ttk.Button(row, text='选择…', command=lambda: choose()).pack(side='right')
    details = tk.Text(window, height=10, wrap='word', state='disabled')
    details.pack(fill='both', expand=True, padx=18, pady=8)
    button = ttk.Button(window, text='检查本地模型')
    button.pack(pady=(0, 14))

    def choose():
        folder = filedialog.askdirectory(title='选择已复制模型的目录', parent=window)
        if folder:
            selected.set(folder)
            check()

    def close():
        if busy:
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

    def check():
        path = Path(selected.get()).expanduser()
        run(lambda: (path, inspect_local_models(path, app)), 'checked')

    def apply_directory():
        path = Path(selected.get()).expanduser()
        set_models_directory(data, path)
        status.set('模型目录已保存；重新启动后台 Runtime 后加载此目录。')
        button.configure(text='重新检查', command=check, state='normal')

    def render(path, models):
        labels = (('tts', 'CosyVoice3 / 默认音色'), ('asr', 'Qwen3-ASR'), ('llm', 'Ollama 本地模型'))
        lines = [f'目录：{path}']
        for key, label in labels:
            item = models[key]
            lines.append(f'{label}：{"就绪" if item["ready"] else "不可用"}')
            lines.extend(f'  {error}' for error in item['errors'])
        return '\n'.join(lines)

    def poll():
        nonlocal busy
        try:
            while True:
                kind, value = events.get_nowait()
                busy = False
                if kind == 'checked':
                    path, models = value
                    tts_ready = models['tts']['ready']
                    status.set('CosyVoice3 已就绪。' if tts_ready else '基础工作台可用；AI 模型服务尚未就绪。')
                    details.configure(state='normal')
                    details.delete('1.0', 'end')
                    details.insert('end', render(path, models))
                    details.configure(state='disabled')
                    button.configure(text='使用此目录', command=apply_directory, state='normal')
                elif kind == 'error':
                    status.set('本地模型检查失败。')
                    details.configure(state='normal')
                    details.delete('1.0', 'end')
                    details.insert('end', value)
                    details.configure(state='disabled')
                    button.configure(text='重试检查', command=check, state='normal')
        except queue.Empty:
            pass
        if window.winfo_exists():
            window.after(100, poll)
    button.configure(command=check)
    window.after(100, poll)
