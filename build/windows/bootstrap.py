"""Small signed-manifest installer for a clean Windows machine."""
from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import queue
import subprocess
import sys
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

ROOT = Path(getattr(sys, '_MEIPASS', Path(__file__).resolve().parents[2]))
LOCAL = Path(os.environ.get('LOCALAPPDATA') or Path.home() / 'AppData/Local')
DATA = Path(os.environ.get('AI_LIVE_STUDIO_DATA') or LOCAL / 'AI-Live-Studio')
DOWNLOADS = LOCAL / 'AI-Live-Studio-Setup'
DEFAULT_MODELS = Path(os.environ.get('AI_LIVE_STUDIO_MODELS') or LOCAL / 'AI-Live-Studio-Models')

sys.path.insert(0, str(ROOT))
from local_runtime.model_install import ModelInstaller, set_models_directory
from local_runtime.online_update import OnlineUpdater, ensure_idle, file_hash


def _log(message):
    DOWNLOADS.mkdir(parents=True, exist_ok=True)
    with (DOWNLOADS / 'bootstrap.log').open('a', encoding='utf-8') as output:
        output.write(f'{datetime.now(timezone.utc).isoformat()} {message}\n')


def default_models():
    try:
        value = json.loads((DATA / 'config/models-path.json').read_text(encoding='utf-8'))
        selected = value.get('models_dir', '') if isinstance(value, dict) else ''
        if selected:
            return Path(selected).expanduser()
    except (OSError, ValueError):
        pass
    return DEFAULT_MODELS.expanduser()


class Bootstrap:
    def __init__(self, root):
        self.root = root
        self.root.title('AI Live Studio 安装引导')
        self.root.geometry('600x340')
        self.root.resizable(False, False)
        self.events = queue.Queue()
        self.busy = False
        self.updater = None
        self.models = None
        self.stage = None

        ttk.Label(root, text='AI Live Studio', font=('', 18, 'bold')).pack(padx=20, pady=(18, 4), anchor='w')
        self.status = tk.StringVar(value='检查签名安装清单后下载程序与默认模型。')
        ttk.Label(root, textvariable=self.status, wraplength=550).pack(padx=20, pady=8, anchor='w')
        row = ttk.Frame(root)
        row.pack(fill='x', padx=20, pady=4)
        ttk.Label(row, text='模型目录').pack(side='left')
        self.model_path = tk.StringVar(value=str(default_models()))
        ttk.Entry(row, textvariable=self.model_path).pack(side='left', fill='x', expand=True, padx=8)
        ttk.Button(row, text='选择…', command=self.choose_models).pack(side='right')
        self.details = tk.Text(root, height=7, wrap='word', state='disabled')
        self.details.pack(fill='both', expand=True, padx=20, pady=6)
        self.progress = ttk.Progressbar(root, maximum=100)
        self.progress.pack(fill='x', padx=20, pady=8)
        self.button = ttk.Button(root, text='检查版本', command=self.check)
        self.button.pack(pady=(0, 16))
        root.protocol('WM_DELETE_WINDOW', self.close)
        self.root.after(100, self.poll)

    def close(self):
        if self.busy:
            messagebox.showinfo('正在安装', '请等待当前下载或安装完成。', parent=self.root)
            return
        self.root.destroy()

    def choose_models(self):
        selected = filedialog.askdirectory(title='选择模型安装目录', parent=self.root)
        if selected:
            self.model_path.set(selected)

    def run(self, action, result):
        self.busy = True
        self.button.configure(state='disabled')
        def worker():
            try:
                self.events.put((result, action()))
            except Exception as exc:
                _log(f'ERROR {type(exc).__name__}: {exc}')
                self.events.put(('error', str(exc)))
        threading.Thread(target=worker, daemon=True).start()

    def check(self):
        model_dir = self.model_path.get()
        def action():
            self.updater = OnlineUpdater(ROOT, DOWNLOADS, current={})
            release = self.updater.check()
            if not release or not self.updater.package or self.updater.package['kind'] != 'installer':
                raise RuntimeError('签名清单没有可用的完整安装包。')
            if getattr(sys, 'frozen', False):
                signed_bootstrap = release.get('bootstrap')
                running_bootstrap = Path(sys.executable)
                actual_size = running_bootstrap.stat().st_size
                actual_sha256 = file_hash(running_bootstrap)
                self.bootstrap_matches = bool(signed_bootstrap and actual_size == signed_bootstrap['size']
                                              and actual_sha256 == signed_bootstrap['sha256'])
                _log(f'BOOTSTRAP expected_sha256={signed_bootstrap.get("sha256", "") if signed_bootstrap else ""} '
                     f'actual_sha256={actual_sha256} matches={self.bootstrap_matches}')
                self.bootstrap_url = signed_bootstrap.get('url', '') if signed_bootstrap else ''
            else:
                self.bootstrap_matches, self.bootstrap_url = True, ''
            self.models = ModelInstaller(model_dir, DOWNLOADS,
                                         ROOT / 'config/model-source.json')
            model_manifest = self.models.check()
            state = self.models.status()
            _log(f'CHECK app={release["version"]} commit={release["commit"]} '
                 f'installer_sha256={self.updater.package["sha256"]} '
                 f'model={model_manifest["version"]} model_sequence={model_manifest["sequence"]}')
            return release, state
        self.run(action, 'checked')

    def install(self):
        model_dir = Path(self.model_path.get()).expanduser().resolve()
        if not self.models or self.models.models_dir.resolve() != model_dir:
            messagebox.showinfo('重新检查模型目录', '模型目录已更改，请先重新检查版本。', parent=self.root)
            self.check()
            return
        if not messagebox.askokcancel('安装 AI Live Studio',
                '安装程序会请求 Windows 管理员权限。请先结束其他 AI Live Studio 任务。\n\n'
                '下载会校验签名清单、文件大小和 SHA256。', parent=self.root):
            return
        def action():
            installed_app = Path(os.environ.get('ProgramFiles', r'C:\Program Files')) / 'AI Live Studio'
            if (installed_app / 'scripts/windows-runtime.py').is_file():
                ensure_idle(installed_app, DATA)
            package = self.updater.package
            archive = self.updater.download(lambda done, total: self.events.put(('progress', done * 100 / total)))
            self.stage = self.updater.unpack(archive)
            installer = self.stage / 'AI-Live-Studio-Windows-Test-Setup.exe'
            if not installer.is_file():
                raise RuntimeError('已验证的安装包中缺少 Setup.exe。')
            _log(f'DOWNLOADED app={self.updater.release["version"]} '
                 f'size={package["size"]} sha256={package["sha256"]}')
            process = subprocess.Popen([str(installer), '/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART'],
                                       cwd=str(self.stage))
            code = process.wait()
            if code != 0:
                raise RuntimeError(f'完整安装器退出码为 {code}；可重新运行引导程序继续。')
            result = self.models.install(
                lambda done, total: self.events.put(('progress', done * 100 / total)))
            set_models_directory(DATA, model_dir)
            _log(f'INSTALLED model={result["version"]} files={result["installed"]} '
                 f'reused={result["reused"]} manifest_sha256={result["manifest_sha256"]}')
            return result
        self.run(action, 'installed')

    def _details(self, value):
        self.details.configure(state='normal')
        self.details.delete('1.0', 'end')
        self.details.insert('end', value)
        self.details.configure(state='disabled')

    def poll(self):
        try:
            while True:
                kind, value = self.events.get_nowait()
                if kind != 'progress':
                    self.busy = False
                if kind == 'progress':
                    self.progress['value'] = value
                elif kind == 'checked':
                    release, model = value
                    package = self.updater.package
                    self.status.set(f'可安装版本 {release["version"]} · 模型 {model["version"]}')
                    self._details(
                        f'提交：{release["commit"]}\n'
                        f'安装包：{package["size"] / 1024**3:.2f} GiB · SHA256 {package["sha256"]}\n'
                        f'模型文件：{len(self.models.manifest["files"])} 项，'
                        f'复用 {len(model["reused"])} 项，需下载/更新 {len(model["missing"])+len(model["changed"])} 项。\n'
                        f'模型清单 SHA256：{self.models.envelope_hash}\n'
                        f'Bootstrap 与当前清单匹配：{"是" if self.bootstrap_matches else "否；当前版本下载地址：" + self.bootstrap_url}')
                    self.button.configure(text='下载并安装', command=self.install, state='normal')
                elif kind == 'installed':
                    self.status.set('安装完成，已创建桌面快捷方式。')
                    self._details(f'默认模型已就绪。模型目录：{self.model_path.get()}\n\n日志：{DOWNLOADS / "bootstrap.log"}')
                    self.button.configure(text='关闭', command=self.close, state='normal')
                    messagebox.showinfo('安装完成', 'AI Live Studio 与默认模型已安装。\n请从桌面快捷方式启动。', parent=self.root)
                elif kind == 'error':
                    self.status.set('操作未完成；可查看日志并重新运行。')
                    self.button.configure(text='重试检查', command=self.check, state='normal')
                    self._details(f'{value}\n\n日志：{DOWNLOADS / "bootstrap.log"}')
                    messagebox.showerror('安装失败', value, parent=self.root)
        except queue.Empty:
            pass
        self.root.after(100, self.poll)


def main():
    root = tk.Tk()
    Bootstrap(root)
    root.mainloop()


if __name__ == '__main__':
    main()
