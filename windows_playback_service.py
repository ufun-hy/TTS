"""Headless Windows audio-cache downloader and WinMM playback service."""

from __future__ import annotations

import logging
import threading

from audio_client.client import AudioClient, ClientAudio
from audio_client.config import load_config, resolve_cache_dir
from audio_client.playback import PlaybackController


def _logger() -> logging.Logger:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    return logging.getLogger("ai-live-studio-playback")


def main() -> int:
    config = load_config()
    logger = _logger()
    cache_dir = resolve_cache_dir(config)
    client = AudioClient(
        config.server,
        cache_dir,
        config.poll_interval,
        config.api_key,
        config.timeout,
        config.session_id,
    )
    playback = PlaybackController(
        cache_dir,
        logger=logger,
        strict_session=config.strict_session,
        session_id=config.session_id,
        startup_buffer_seconds=config.startup_buffer_seconds,
    )
    stop_event = threading.Event()
    bound_session = config.session_id

    def on_audio(item: ClientAudio) -> str:
        nonlocal bound_session
        session_id = ""
        if config.strict_session and not bound_session:
            metadata = item.metadata.get("server_metadata")
            session_id = metadata.get("session_id", "") if isinstance(metadata, dict) else ""
            if session_id:
                playback.set_session(session_id)
                bound_session = session_id
        if not playback.is_running() and (
            not config.strict_session or config.session_id or session_id
        ):
            playback.start()
        logger.info("received %s", item.id)
        return "completed"

    if not config.strict_session or config.session_id:
        playback.start()
    try:
        client.run(on_audio, stop=stop_event.is_set)
    except KeyboardInterrupt:
        stop_event.set()
    finally:
        playback.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
