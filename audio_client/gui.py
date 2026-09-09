"""Small Tkinter desktop shell for the LAN audio client."""

from __future__ import annotations

import logging
import queue
import threading
import tkinter as tk
from tkinter import messagebox, ttk
from typing import Any, Dict, Optional, Tuple

from .client import AudioClient, AudioClientError
from .config import ClientConfig, config_path, install_dir, load_config, resolve_cache_dir, save_config


class AudioClientApp:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title("AI Audio Client")
        self.root.geometry("520x370")
        self.root.minsize(480, 340)
        self.events: "queue.Queue[Tuple[str, Any]]" = queue.Queue()
        self.stop_event: Optional[threading.Event] = None
        self.worker: Optional[threading.Thread] = None
        self.restart_requested = False
        self.logger = _make_logger()

        try:
            self.config = load_config()
        except (OSError, ValueError, TypeError) as exc:
            self.config = ClientConfig()
            self.logger.error("load config failed: %s", exc)

        self.server_var = tk.StringVar(value=self.config.server)
        self.cache_var = tk.StringVar(value=self.config.cache_dir)
        self.poll_var = tk.StringVar(value=str(self.config.poll_interval))
        self.api_key_var = tk.StringVar(value=self.config.api_key)
        self.status_var = tk.StringVar(value="等待连接")
        self.server_status_var = tk.StringVar(value=self.config.server)
        self.received_var = tk.StringVar(value="0")
        self.cache_count_var = tk.StringVar(value="0")
        self.completed_var = tk.StringVar(value="0")
        self.failed_var = tk.StringVar(value="0")
        self.error_var = tk.StringVar(value="无")

        self._build_ui()
        self.root.protocol("WM_DELETE_WINDOW", self.close)
        self.root.after(200, self._drain_events)
        self.root.after(1000, self._refresh_local_stats)

    def _build_ui(self) -> None:
        outer = ttk.Frame(self.root, padding=16)
        outer.pack(fill="both", expand=True)
        outer.columnconfigure(1, weight=1)

        config_frame = ttk.LabelFrame(outer, text="服务配置", padding=10)
        config_frame.grid(row=0, column=0, columnspan=2, sticky="ew")
        config_frame.columnconfigure(1, weight=1)
        self._field(config_frame, 0, "AI Server", self.server_var)
        self._field(config_frame, 1, "缓存目录", self.cache_var)
        self._field(config_frame, 2, "轮询间隔(s)", self.poll_var)
        self._field(config_frame, 3, "API Key", self.api_key_var, password=True)
        ttk.Button(config_frame, text="保存配置", command=self.save).grid(row=4, column=1, sticky="e", pady=(8, 0))

        status_frame = ttk.LabelFrame(outer, text="运行状态", padding=10)
        status_frame.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(12, 0))
        status_frame.columnconfigure(1, weight=1)
        self._status_row(status_frame, 0, "连接状态", self.status_var)
        self._status_row(status_frame, 1, "服务器", self.server_status_var)
        self._status_row(status_frame, 2, "已接收", self.received_var)
        self._status_row(status_frame, 3, "本地已缓存", self.cache_count_var)
        self._status_row(status_frame, 4, "已完成", self.completed_var)
        self._status_row(status_frame, 5, "失败", self.failed_var)
        self._status_row(status_frame, 6, "最近错误", self.error_var)

        buttons = ttk.Frame(outer)
        buttons.grid(row=2, column=0, columnspan=2, sticky="ew", pady=(14, 0))
        self.start_button = ttk.Button(buttons, text="启动", command=self.start)
        self.start_button.pack(side="left", expand=True, fill="x")
        self.stop_button = ttk.Button(buttons, text="停止", command=self.stop, state="disabled")
        self.stop_button.pack(side="left", expand=True, fill="x", padx=8)
        self.reconnect_button = ttk.Button(buttons, text="重新连接", command=self.reconnect)
        self.reconnect_button.pack(side="left", expand=True, fill="x")

        ttk.Label(outer, text=f"配置文件: {config_path()}").grid(row=3, column=0, columnspan=2, sticky="w", pady=(12, 0))

    @staticmethod
    def _field(parent: ttk.Frame, row: int, label: str, variable: tk.StringVar, password: bool = False) -> None:
        ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w", padx=(0, 10), pady=3)
        entry = ttk.Entry(parent, textvariable=variable, show="*" if password else "")
        entry.grid(row=row, column=1, sticky="ew", pady=3)

    @staticmethod
    def _status_row(parent: ttk.Frame, row: int, label: str, variable: tk.StringVar) -> None:
        ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w", padx=(0, 16), pady=2)
        ttk.Label(parent, textvariable=variable).grid(row=row, column=1, sticky="w", pady=2)

    def _read_form(self) -> ClientConfig:
        return ClientConfig.from_dict({
            "server": self.server_var.get(),
            "cache_dir": self.cache_var.get(),
            "poll_interval": self.poll_var.get(),
            "api_key": self.api_key_var.get(),
        })

    def save(self) -> bool:
        try:
            self.config = self._read_form()
            save_config(self.config)
            self.server_status_var.set(self.config.server)
            self.error_var.set("无")
            self.logger.info("configuration saved")
            return True
        except (OSError, ValueError, TypeError) as exc:
            self.error_var.set(str(exc))
            messagebox.showerror("配置错误", str(exc), parent=self.root)
            return False

    def start(self) -> None:
        if self.worker and self.worker.is_alive():
            return
        if not self.save():
            return
        self.stop_event = threading.Event()
        self.restart_requested = False
        self.status_var.set("连接中")
        self.start_button.configure(state="disabled")
        self.stop_button.configure(state="normal")
        self.worker = threading.Thread(target=self._worker_loop, args=(self.config, self.stop_event), daemon=True)
        self.worker.start()
        self.logger.info("client started")

    def stop(self) -> None:
        if self.stop_event:
            self.stop_event.set()
        self.status_var.set("已停止")
        self.start_button.configure(state="normal")
        self.stop_button.configure(state="disabled")
        self.logger.info("client stop requested")

    def reconnect(self) -> None:
        if not self.save():
            return
        self.restart_requested = True
        self.stop()
        self._wait_for_worker()

    def _wait_for_worker(self) -> None:
        if self.worker and self.worker.is_alive():
            self.root.after(100, self._wait_for_worker)
            return
        if self.restart_requested:
            self.start()

    def close(self) -> None:
        self.restart_requested = False
        self.stop()
        self.root.destroy()

    def _worker_loop(self, config: ClientConfig, stop: threading.Event) -> None:
        client = AudioClient(config.server, resolve_cache_dir(config), config.poll_interval, config.api_key, config.timeout)
        connected = False
        while not stop.is_set():
            try:
                health = client.health()
                if not connected:
                    self.logger.info("connect server success: %s", config.server)
                    connected = True
                self.events.put(("connected", health))
                item = client.fetch_next()
                if item:
                    self.logger.info("received %s", item.id)
                    self.events.put(("received", client.local_stats()))
                    # V1 has no playback consumer. A durable local WAV is the
                    # transport completion boundary, so ACK only after the atomic save.
                    client.ack(item.id, "completed")
                    self.logger.info("completed %s", item.id)
                    self.events.put(("completed", client.local_stats()))
                else:
                    stop.wait(config.poll_interval)
            except (AudioClientError, OSError, ValueError, TypeError) as exc:
                if connected:
                    self.logger.info("server connection lost: %s", config.server)
                    connected = False
                self.logger.error("client error: %s", exc)
                self.events.put(("error", str(exc)))
                stop.wait(config.poll_interval)
        self.events.put(("stopped", None))

    def _drain_events(self) -> None:
        try:
            while True:
                kind, payload = self.events.get_nowait()
                if kind == "connected":
                    self.status_var.set("● 已连接")
                    self.error_var.set("无")
                elif kind in ("received", "completed"):
                    self._set_stats(payload)
                elif kind == "error":
                    self.status_var.set("等待连接")
                    self.error_var.set(str(payload))
                elif kind == "stopped":
                    self.status_var.set("已停止")
        except queue.Empty:
            pass
        self.root.after(200, self._drain_events)

    def _refresh_local_stats(self) -> None:
        try:
            stats = AudioClient(self.config.server, resolve_cache_dir(self.config), self.config.poll_interval, self.config.api_key, self.config.timeout).local_stats()
            self._set_stats(stats)
        except (OSError, ValueError, TypeError):
            pass
        self.root.after(1000, self._refresh_local_stats)

    def _set_stats(self, stats: Optional[Dict[str, int]]) -> None:
        if not stats:
            return
        self.received_var.set(str(stats.get("received", 0)))
        self.cache_count_var.set(str(stats.get("cache", 0)))
        self.completed_var.set(str(stats.get("completed", 0)))
        self.failed_var.set(str(stats.get("failed", 0)))


def _make_logger() -> logging.Logger:
    logger = logging.getLogger("ai-audio-client")
    logger.setLevel(logging.INFO)
    if not logger.handlers:
        log_dir = install_dir() / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        handler = logging.FileHandler(log_dir / "client.log", encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        logger.addHandler(handler)
    return logger


def main() -> None:
    root = tk.Tk()
    AudioClientApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
