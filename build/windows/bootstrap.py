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
import time

ROOT = Path(getattr(sys, '_MEIPASS', Path(__file__).resolve().parents[2]))
LOCAL = Path(os.environ.get('LOCALAPPDATA') or Path.home() / 'AppData/Local')
DATA = Path(os.environ.get('AI_LIVE_STUDIO_DATA') or LOCAL / 'AI-Live-Studio')
DOWNLOADS = LOCAL / 'AI-Live-Studio-Setup'
DEFAULT_MODELS = Path(os.environ.get('AI_LIVE_STUDIO_MODELS') or LOCAL / 'AI-Live-Studio-Models')

sys.path.insert(0, str(ROOT))
from local_runtime.model_install import inspect_local_models, set_models_directory
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


def installed_app():
    return Path(os.environ.get('ProgramFiles', r'C:\Program Files')) / 'AI Live Studio'


class Bootstrap:
    def __init__(self, root):
        self.root = root
        self.root.title('AI Live Studio 安装引导')
        self.root.geometry('600x340')
        self.root.resizable(False, False)
        self.events = queue.Queue()
        self.busy = False
        self.updater = None
        self.model_status = None
        self.checked_model_dir = None
        self.program_installed = False
        self.stage = None

        ttk.Label(root, text='AI Live Studio', font=('', 18, 'bold')).pack(padx=20, pady=(18, 4), anchor='w')
        self.status = tk.StringVar(value='检查签名程序清单。模型由用户自行复制。')
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
            self.model_status = inspect_local_models(model_dir, installed_app())
            self.checked_model_dir = Path(model_dir).expanduser().resolve()
            _log(f'CHECK app={release["version"]} commit={release["commit"]} '
                 f'installer_sha256={self.updater.package["sha256"]} '
                 f'local_tts_ready={self.model_status["tts"]["ready"]}')
            return release, self.model_status
        self.run(action, 'checked')

    def install(self):
        model_dir = Path(self.model_path.get()).expanduser().resolve()
        if self.checked_model_dir != model_dir:
            messagebox.showinfo('重新检查安装包', '模型目录已更改，请先重新检查。', parent=self.root)
            self.check()
            return
        if not messagebox.askokcancel('安装 AI Live Studio',
                '此步骤只安装程序和运行环境，不下载模型。\n\n'
                '安装程序会请求 Windows 管理员权限，并校验签名清单、大小和 SHA256。', parent=self.root):
            return
        def action():
            app_dir = installed_app()
            if (app_dir / 'scripts/windows-runtime.py').is_file():
                ensure_idle(app_dir, DATA)
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
            self.program_installed = True
            set_models_directory(DATA, model_dir)
            self.model_status = inspect_local_models(model_dir, app_dir)
            _log(f'PROGRAM_INSTALLED app={self.updater.release["version"]} '
                 f'local_tts_ready={self.model_status["tts"]["ready"]}')
            return {'app_dir': app_dir, 'model_status': self.model_status}
        self.run(action, 'installed')

    def open_workspace(self):
        app_exe = installed_app() / 'AI-Live-Studio.exe'
        if not app_exe.is_file():
            messagebox.showerror('程序未安装', f'找不到客户端：{app_exe}', parent=self.root)
            return
        def action():
            subprocess.Popen([str(app_exe)], cwd=str(app_exe.parent))
            python = app_exe.parent / 'runtime/python/python.exe'
            status_script = app_exe.parent / 'scripts/windows-runtime.py'
            deadline = time.monotonic() + 180
            last_error = ''
            while time.monotonic() < deadline:
                try:
                    result = subprocess.run(
                        [str(python), str(status_script), 'status', '--data', str(DATA)],
                        capture_output=True, text=True, timeout=15,
                        creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
                    payload = json.loads(result.stdout)
                    health = payload.get('health') if isinstance(payload.get('health'), dict) else {}
                    processes = payload.get('processes') if isinstance(payload.get('processes'), dict) else {}
                    studio = processes.get('text-studio') if isinstance(processes.get('text-studio'), dict) else {}
                    if result.returncode == 0 and health.get('studio') is True and studio.get('owned') is True:
                        return True
                    last_error = '基础工作台尚未由 AI Live Studio Runtime 提供'
                except Exception as exc:
                    last_error = str(exc)
                time.sleep(0.5)
            raise RuntimeError(f'程序已安装，但基础工作台未能启动：{last_error}')
        self.run(action, 'workbench')

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
                    self.status.set(f'可安装程序版本 {release["version"]}')
                    self._details(
                        f'提交：{release["commit"]}\n'
                        f'安装包：{package["size"] / 1024**3:.2f} GiB · SHA256 {package["sha256"]}\n'
                        f'程序安装：待安装\n基础工作台：安装后启动检查\n'
                        f'本地 CosyVoice3：{"就绪" if model["tts"]["ready"] else "未就绪"}\n'
                        f'本地 ASR：{"就绪" if model["asr"]["ready"] else "未就绪"}；'
                        f'本地 Ollama 模型：{"就绪" if model["llm"]["ready"] else "未就绪"}\n'
                        f'Bootstrap 与当前清单匹配：{"是" if self.bootstrap_matches else "否；当前版本下载地址：" + self.bootstrap_url}')
                    self.button.configure(text='安装程序', command=self.install, state='normal')
                elif kind == 'installed':
                    model = value['model_status']
                    self.status.set('程序安装：完成；基础工作台：尚未启动。')
                    self._details(
                        f'程序安装：完成，桌面快捷方式已创建。\n基础工作台：点击下方按钮启动并检查。\n'
                        f'本地 CosyVoice3：{"就绪" if model["tts"]["ready"] else "未就绪"}\n'
                        f'本地 ASR：{"就绪" if model["asr"]["ready"] else "未就绪"}\n'
                        f'本地 Ollama 模型：{"就绪" if model["llm"]["ready"] else "未就绪"}\n'
                        f'模型目录：{self.model_path.get()}\n日志：{DOWNLOADS / "bootstrap.log"}')
                    self.button.configure(text='打开基础工作台', command=self.open_workspace, state='normal')
                elif kind == 'workbench':
                    self.status.set('程序安装：完成；基础工作台：可用。')
                    self._details(self.details.get('1.0', 'end').rstrip() + '\n基础工作台：可用')
                    self.button.configure(text='重新打开工作台', command=self.open_workspace, state='normal')
                    messagebox.showinfo('基础工作台已启动',
                                        '程序安装完成，基础工作台可用。本地模型状态见窗口详情。', parent=self.root)
                elif kind == 'error':
                    if self.program_installed:
                        self.status.set('程序安装：完成；基础工作台：未启动。')
                        self.button.configure(text='重试打开工作台', command=self.open_workspace, state='normal')
                    else:
                        self.status.set('程序安装：未完成；可查看日志并重试。')
                        self.button.configure(text='重试检查', command=self.check, state='normal')
                    self._details(f'{value}\n\n日志：{DOWNLOADS / "bootstrap.log"}')
                    messagebox.showerror('操作未完成', value, parent=self.root)
        except queue.Empty:
            pass
        self.root.after(100, self.poll)


def main():
    root = tk.Tk()
    Bootstrap(root)
    root.mainloop()


if __name__ == '__main__':
    main()
