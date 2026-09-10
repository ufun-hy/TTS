"""Small Tkinter desktop shell for the LAN audio client and player."""

from __future__ import annotations

import logging
import queue
import threading
import tkinter as tk
from tkinter import messagebox, ttk
from typing import Any, Dict, Optional, Tuple

from .client import AudioClient, AudioClientError
from .config import ClientConfig, config_path, install_dir, load_config, resolve_cache_dir, save_config
from .playback import PlaybackController


class AudioClientApp:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title("AI Audio Client")
        self.root.geometry("560x600")
        self.root.minsize(520, 540)
        self.events: "queue.Queue[Tuple[str, Any]]" = queue.Queue()
        self.stop_event: Optional[threading.Event] = None
        self.worker: Optional[threading.Thread] = None
        self.playback: Optional[PlaybackController] = None
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
        self.playing_var = tk.StringVar(value="-")
        self.buffered_var = tk.StringVar(value="0")
        self.played_var = tk.StringVar(value="0")
        self.playback_status_var = tk.StringVar(value="已停止")
        self.playback_failed_var = tk.StringVar(value="0")
        self.error_var = tk.StringVar(value="无")

        self._build_ui()
        self.root.protocol("WM_DELETE_WINDOW", self.close)
        self.root.after(200, self._drain_events)
        self.root.after(1000, self._refresh_local_stats)
        self.root.after(100, self.start)

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
        self._status_row(status_frame, 4, "正在播放", self.playing_var)
        self._status_row(status_frame, 5, "待播放缓存", self.buffered_var)
        self._status_row(status_frame, 6, "已播放", self.played_var)
        self._status_row(status_frame, 7, "播放状态", self.playback_status_var)
        self._status_row(status_frame, 8, "播放失败", self.playback_failed_var)
        self._status_row(status_frame, 9, "最近错误", self.error_var)

        network_buttons = ttk.Frame(outer)
        network_buttons.grid(row=2, column=0, columnspan=2, sticky="ew", pady=(14, 0))
        self.start_button = ttk.Button(network_buttons, text="启动", command=self.start)
        self.start_button.pack(side="left", expand=True, fill="x")
        self.stop_button = ttk.Button(network_buttons, text="停止", command=self.stop, state="disabled")
        self.stop_button.pack(side="left", expand=True, fill="x", padx=8)
        self.reconnect_button = ttk.Button(network_buttons, text="重新连接", command=self.reconnect)
        self.reconnect_button.pack(side="left", expand=True, fill="x")

        playback_frame = ttk.LabelFrame(outer, text="播放控制", padding=10)
        playback_frame.grid(row=3, column=0, columnspan=2, sticky="ew", pady=(12, 0))
        for column in range(4):
            playback_frame.columnconfigure(column, weight=1)
        ttk.Button(playback_frame, text="开始播放", command=self.start_playback).grid(row=0, column=0, sticky="ew")
        ttk.Button(playback_frame, text="暂停播放", command=self.pause_playback).grid(row=0, column=1, sticky="ew", padx=6)
        ttk.Button(playback_frame, text="继续播放", command=self.resume_playback).grid(row=0, column=2, sticky="ew", padx=6)
        ttk.Button(playback_frame, text="停止播放", command=self.stop_playback).grid(row=0, column=3, sticky="ew")
        ttk.Button(playback_frame, text="清除缓存", command=self.clear_cache).grid(
            row=1, column=0, columnspan=4, sticky="ew", pady=(8, 0)
        )

        ttk.Label(
            outer,
            text="清除缓存只会删除已播放、已作废和播放失败的片段；当前待播放内容不会删除。",
            foreground="#666666",
        ).grid(row=4, column=0, columnspan=2, sticky="w", pady=(8, 0))
        ttk.Label(outer, text=f"配置文件: {config_path()}").grid(row=5, column=0, columnspan=2, sticky="w", pady=(10, 0))

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

    def _playback_controller(self) -> PlaybackController:
        cache_dir = resolve_cache_dir(self.config)
        if self.playback is None or self.playback.cache_dir != cache_dir:
            if self.playback and self.playback.is_running():
                self.playback.stop()
            self.playback = PlaybackController(cache_dir, self._on_playback_event, self.logger)
        return self.playback

    def start_playback(self) -> None:
        if not self.save():
            return
        try:
            self._playback_controller().start()
        except (OSError, ValueError, RuntimeError) as exc:
            self.error_var.set(str(exc))
            self.logger.error("start playback failed: %s", exc)

    def pause_playback(self) -> None:
        if self.playback:
            self.playback.pause()

    def resume_playback(self) -> None:
        if self.playback:
            self.playback.resume()

    def stop_playback(self) -> None:
        if self.playback:
            self.playback.stop()
        else:
            self.playback_status_var.set("已停止")

    def clear_cache(self) -> None:
        if not self.save():
            return
        if not messagebox.askyesno(
            "清除缓存",
            "将删除已播放、已作废和播放失败的声音缓存。\n当前正在播放和待播放的内容会保留。\n\n是否继续？",
            parent=self.root,
        ):
            return
        try:
            result = self._playback_controller().clear_cache()
            removed_items = int(result.get("removed_items", 0))
            removed_bytes = int(result.get("removed_bytes", 0))
            self._set_playback_stats(self.playback.stats())
            messagebox.showinfo(
                "缓存已清理",
                f"已删除 {removed_items} 个安全缓存片段，释放 {_format_bytes(removed_bytes)}。",
                parent=self.root,
            )
        except (OSError, ValueError, RuntimeError) as exc:
            self.error_var.set(str(exc))
            self.logger.error("clear cache failed: %s", exc)
            messagebox.showerror("清除缓存失败", str(exc), parent=self.root)

    def close(self) -> None:
        self.restart_requested = False
        self.stop()
        if self.playback:
            self.playback.stop()
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
                    # V1 has no playback consumer acknowledgement on the server.
                    # The durable local WAV is the transport completion boundary.
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

    def _on_playback_event(self, stats: Dict[str, Any]) -> None:
        self.events.put(("playback", stats))

    def _drain_events(self) -> None:
        try:
            while True:
                kind, payload = self.events.get_nowait()
                if kind == "connected":
                    self.status_var.set("● 已连接")
                    self.error_var.set("无")
                elif kind in ("received", "completed"):
                    self._set_network_stats(payload)
                elif kind == "playback":
                    self._set_playback_stats(payload)
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
            client = AudioClient(self.config.server, resolve_cache_dir(self.config), self.config.poll_interval, self.config.api_key, self.config.timeout)
            self._set_network_stats(client.local_stats())
            if self.playback:
                self._set_playback_stats(self.playback.stats())
        except (OSError, ValueError, TypeError):
            pass
        self.root.after(1000, self._refresh_local_stats)

    def _set_network_stats(self, stats: Optional[Dict[str, Any]]) -> None:
        if not stats:
            return
        self.received_var.set(str(stats.get("received", self.received_var.get())))
        if not self.playback:
            self.cache_count_var.set(str(stats.get("cache", self.cache_count_var.get())))

    def _set_playback_stats(self, stats: Optional[Dict[str, Any]]) -> None:
        if not stats:
            return
        self.cache_count_var.set(str(stats.get("cache", self.cache_count_var.get())))
        self.playing_var.set(str(stats.get("playing", "-")))
        self.buffered_var.set(str(stats.get("buffered_segments", 0)))
        self.played_var.set(str(stats.get("played", 0)))
        self.playback_failed_var.set(str(stats.get("playback_failed", 0)))
        state = str(stats.get("playback_status", "stopped"))
        labels = {
            "playing": "播放中",
            "paused": "已暂停",
            "waiting": "等待音频" if stats.get("buffered_segments", 0) == 0 else "播放中",
            "stopped": "已停止",
        }
        self.playback_status_var.set(labels.get(state, state))
        if stats.get("playback_error"):
            self.error_var.set(str(stats["playback_error"]))


def _format_bytes(value: int) -> str:
    size = float(max(0, value))
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} GB"


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
