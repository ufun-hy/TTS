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
        config.strict_session,
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
                client.session_id = session_id
        elif config.strict_session:
            metadata = item.metadata.get("server_metadata")
            incoming = metadata.get("session_id", "") if isinstance(metadata, dict) else ""
            if incoming and incoming != bound_session:
                logger.warning("ignoring late session %s while %s is bound", incoming, bound_session)
                return "completed"
        if not playback.is_running() and (
            not config.strict_session or config.session_id or session_id
        ):
            playback.start()
        logger.info("received %s", item.id)
        return "completed"

    def before_fetch() -> bool:
        nonlocal bound_session
        if not bound_session:
            # Dynamic strict mode discovers the next active session through
            # /audio/next; the cache excludes stopped sessions.
            return True
        control = client.session_control(bound_session)
        status = control.get("status", "running")
        if status == "paused":
            playback.pause()
            return False
        elif status == "running":
            playback.resume()
            return True
        elif status in ("stopping", "stopped"):
            playback.stop()
            if config.strict_session and not config.session_id:
                playback.reset_session()
                bound_session = ""
                client.session_id = ""
            else:
                stop_event.set()
            return False
        return True

    if not config.strict_session or config.session_id:
        playback.start()
    try:
        client.run(on_audio, stop=stop_event.is_set, before_fetch=before_fetch)
    except KeyboardInterrupt:
        stop_event.set()
    finally:
        playback.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
