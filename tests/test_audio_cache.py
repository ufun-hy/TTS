import base64
import io
import json
import math
from pathlib import Path
import struct
import tempfile
import threading
import time
import unittest
import wave

from audio_client.client import AudioClient
from audio_client.playback import PlaybackController
from audio_client.config import ClientConfig
from audio_cache.manager import AudioCacheManager
from audio_cache.processing import AudioProcessingConfig, calculate_speed_factor, calculate_volume_gain
from audio_cache.server import AudioCacheServer, make_handler


def wav_bytes(duration=0.1, rate=8000):
    frames = b"\x00\x00" * int(duration * rate)
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(rate)
        audio.writeframes(frames)
    return buffer.getvalue()


def tone_wav_bytes(duration=1.0, rate=8000):
    buffer = io.BytesIO()
    frames = b"".join(int(10000 * math.sin(2 * math.pi * 440 * index / rate)).to_bytes(2, "little", signed=True) for index in range(int(duration * rate)))
    with wave.open(buffer, "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(rate)
        audio.writeframes(frames)
    return buffer.getvalue()


def float_wav_bytes(duration=1.0, rate=24000):
    frames = b"".join(struct.pack("<f", 0.25 * math.sin(2 * math.pi * 440 * index / rate)) for index in range(int(duration * rate)))
    fmt = struct.pack("<HHIIHH", 3, 1, rate, rate * 4, 4, 32)
    body = b"WAVEfmt " + struct.pack("<I", len(fmt)) + fmt + b"data" + struct.pack("<I", len(frames)) + frames
    return b"RIFF" + struct.pack("<I", len(body)) + body


def write_client_item(root, item_id, session_id, duration, playback_status, downloaded_at):
    (root / f"{item_id}.wav").write_bytes(wav_bytes(duration=min(duration, 0.1)))
    (root / f"{item_id}.json").write_text(json.dumps({
        "id": item_id,
        "status": "completed",
        "duration": duration,
        "downloaded_at": downloaded_at,
        "sequence": int(item_id.rsplit("_", 1)[-1]),
        "playback_status": playback_status,
        "server_metadata": {"session_id": session_id},
    }), encoding="utf-8")


class AudioCacheTests(unittest.TestCase):
    def test_short_download_after_playback_does_not_reenter_startup_buffer(self):
        root = Path(tempfile.mkdtemp(prefix="live-short-resume-"))
        write_client_item(root, "old_001", "session", 30, "played", "2026-09-24T00:00:00Z")
        client = AudioClient("http://unused", root)
        self.assertFalse(client._session_needs_buffering("session"))
        write_client_item(root, "new_002", "session", 3, "buffering", "2026-09-24T00:01:00Z")
        self.assertTrue(client._release_startup_buffer("session"))
        self.assertEqual(json.loads((root / "new_002.json").read_text())["playback_status"], "cached")

    def test_startup_buffer_links_download_metadata_to_playback_selection(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manager = AudioCacheManager(root / "server", lambda audio, _metadata: audio)
            session_id = "linked-startup"
            for index, duration in enumerate((5.0, 4.0, 4.0), 1):
                manager.add_audio(wav_bytes(), {
                    "sequence": index,
                    "session_id": session_id,
                    "duration": duration,
                    "source_audio_sha256": f"{index:064x}",
                    "playback_speed": 1.0,
                    "volume": 100.0,
                    "session_final": False,
                }, f"linked_{index:03d}")

            server = AudioCacheServer(("127.0.0.1", 0), make_handler(manager))
            server_thread = threading.Thread(target=server.serve_forever, daemon=True)
            server_thread.start()
            played = []

            class Player:
                def play(self, path, stop_event, pause_event):
                    played.append(path.stem)
                    time.sleep(0.02)

            controller = PlaybackController(root / "client", player=Player())
            controller.start()
            try:
                client = AudioClient(f"http://127.0.0.1:{server.server_port}", root / "client")
                for item_id in ("linked_001", "linked_002"):
                    item = client.fetch_next()
                    self.assertEqual(item.id, item_id)
                    self.assertEqual(item.metadata["playback_status"], "buffering")
                    client.ack(item.id)
                    time.sleep(0.1)
                    self.assertEqual(played, [])
                item = client.fetch_next()
                self.assertEqual(item.metadata["playback_status"], "cached")
                client.ack(item.id)
                deadline = time.time() + 2
                while len(played) < 3 and time.time() < deadline:
                    time.sleep(0.01)
                self.assertEqual(played, ["linked_001", "linked_002", "linked_003"])
            finally:
                controller.stop()
                server.shutdown()
                server.server_close()

    def test_delivery_claim_lease_requeues_only_expired_claims(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manager = AudioCacheManager(root, lambda audio, _metadata: audio, claim_lease_seconds=30)
            manager.add_audio(wav_bytes(), {"sequence": 1}, "expired")
            manager.add_audio(wav_bytes(), {"sequence": 2}, "active")
            self.assertNotIn("processing_kind", manager.get("expired").metadata)
            claimed = manager.claim_next("windows-a")
            self.assertEqual(claimed.metadata["processing_kind"], "delivery")
            self.assertEqual(claimed.metadata["claimed_by"], "windows-a")
            claimed.metadata["claim_expires_at"] = "2000-01-01T00:00:00+00:00"
            still_active = manager.claim_next("windows-b")
            self.assertEqual(still_active.id, "expired")
            self.assertEqual(manager.get("active").status, "ready")
            self.assertEqual(manager.get("expired").metadata["claimed_by"], "windows-b")

    def test_audio_processing_is_not_requeued_as_delivery_claim(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            started = threading.Event()
            release = threading.Event()

            def process(audio, _metadata):
                started.set()
                release.wait(1)
                return audio

            manager = AudioCacheManager(root, process, claim_lease_seconds=0.01)
            worker = threading.Thread(target=lambda: manager.add_audio(wav_bytes(), {}, "processing"))
            worker.start()
            self.assertTrue(started.wait(1))
            time.sleep(0.05)
            self.assertEqual(manager.requeue_expired_claims(), 0)
            self.assertEqual(manager.get("processing").status, "processing")
            release.set()
            worker.join(1)

    def test_state_flow_keeps_order_and_ack_is_idempotent(self):
        with tempfile.TemporaryDirectory() as directory:
            manager = AudioCacheManager(Path(directory), lambda audio, _metadata: audio)
            manager.add_audio(wav_bytes(), {"text": "第二段", "sequence": 2}, "segment_002")
            manager.add_audio(wav_bytes(), {"text": "第一段", "sequence": 1}, "segment_001")
            first = manager.claim_next()
            self.assertEqual(first.id, "segment_001")
            completed = manager.ack(first.id)
            self.assertEqual(completed.status, "completed")
            self.assertEqual(manager.ack(first.id).status, "completed")
            self.assertEqual(manager.claim_next().id, "segment_002")

    def test_failed_processing_is_isolated(self):
        with tempfile.TemporaryDirectory() as directory:
            def fail(_audio, _metadata):
                raise ValueError("bad audio")

            manager = AudioCacheManager(Path(directory), fail)
            with self.assertRaises(ValueError):
                manager.add_audio(b"not wav", {"text": "失败"}, "segment_001")
            self.assertEqual(manager.stats()["failed"], 1)
            metadata = json.loads((Path(directory) / "failed/segment_001/metadata.json").read_text())
            self.assertEqual(metadata["status"], "failed")

    def test_session_stats_and_cleanup_are_scoped(self):
        with tempfile.TemporaryDirectory() as directory:
            manager = AudioCacheManager(Path(directory), lambda audio, _metadata: audio)
            manager.add_audio(wav_bytes(), {"session_id": "live_a"}, "segment_001")
            manager.add_audio(wav_bytes(), {"session_id": "live_b"}, "segment_002")
            self.assertEqual(manager.session_stats("live_a")["ready"], 1)
            self.assertEqual(manager.cleanup_session("live_a"), 1)
            self.assertFalse(manager.has("segment_001"))
            self.assertTrue(manager.has("segment_002"))

    def test_processing_limits_are_reported(self):
        config = AudioProcessingConfig()
        factor, warnings = calculate_speed_factor(40, 30, config)
        self.assertEqual(factor, 1.15)
        self.assertIn("speed_factor_out_of_range", warnings)
        gain, warnings = calculate_volume_gain(-30, config)
        self.assertEqual(gain, 3)
        self.assertIn("volume_gain_out_of_range", warnings)

    def test_session_speed_is_applied_on_mac_processor(self):
        from audio_cache.processing import AudioProcessor

        processor = AudioProcessor(AudioProcessingConfig(volume_enabled=False))
        for speed in (1.0, 1.05, 1.10):
            result = processor.process(tone_wav_bytes(), {"playback_speed": speed, "volume": 100})
            self.assertTrue(result.audio.startswith(b"RIFF"))
            self.assertAlmostEqual(result.duration, 1 / speed, delta=0.02)

    def test_processor_accepts_cosyvoice_float_wav(self):
        from audio_cache.processing import AudioProcessor

        result = AudioProcessor(AudioProcessingConfig(volume_enabled=False)).process(
            float_wav_bytes(), {"source": "live_session", "playback_speed": 1, "volume": 100}
        )
        self.assertTrue(result.audio.startswith(b"RIFF"))
        self.assertAlmostEqual(result.duration, 1.0, delta=0.01)

    def test_live_session_float_wav_fast_path_uses_default_processing_config(self):
        from audio_cache.processing import AudioProcessor

        source = float_wav_bytes()
        result = AudioProcessor().process(
            source, {"source": "live_session", "playback_speed": 1, "volume": 100}
        )
        self.assertEqual(result.audio, source)
        self.assertAlmostEqual(result.duration, 1.0, delta=0.01)

    def test_session_volume_is_applied_without_changing_source(self):
        from audio_cache.processing import AudioProcessor

        source = tone_wav_bytes()
        result = AudioProcessor(AudioProcessingConfig(speed_enabled=False)).process(source, {"playback_speed": 1, "volume": 50})
        self.assertNotEqual(result.audio, source)
        self.assertAlmostEqual(result.session_volume_gain, -6.0206, places=2)

    def test_http_client_downloads_and_acknowledges(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manager = AudioCacheManager(root / "server", lambda audio, _metadata: audio)
            manager.add_audio(wav_bytes(), {"sequence": 1}, "segment_001")
            server = AudioCacheServer(("127.0.0.1", 0), make_handler(manager))
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                client = AudioClient(f"http://127.0.0.1:{server.server_port}", root / "client")
                self.assertEqual(client.health()["status"], "ok")
                item = client.fetch_next()
                self.assertEqual(item.id, "segment_001")
                self.assertTrue(item.path.is_file())
                self.assertEqual(json.loads((root / "client/segment_001.json").read_text())["playback_status"], "cached")
                self.assertEqual(client.ack(item.id)["status"], "completed")
                self.assertEqual(manager.stats()["completed"], 1)
            finally:
                server.shutdown()
                server.server_close()

    def test_client_config_accepts_install_format_and_counts_local_state(self):
        with tempfile.TemporaryDirectory() as directory:
            cache = Path(directory)
            config = ClientConfig.from_dict({"server": "http://server:8000", "cache_dir": "./cache", "poll_interval": 1})
            self.assertEqual(ClientConfig.from_dict({"server": "http://server:8000", "poll_interval": 1000}).poll_interval, 1)
            (cache / "segment_001.wav").write_bytes(wav_bytes())
            (cache / "segment_001.json").write_text(json.dumps({"status": "completed"}), encoding="utf-8")
            self.assertEqual(AudioClient(config.server, cache).local_stats(), {"received": 1, "completed": 1, "failed": 0, "cache": 1})

    def test_local_playback_state_reports_newest_session_buffer_seconds(self):
        with tempfile.TemporaryDirectory() as directory:
            cache = Path(directory)
            write_client_item(cache, "old_001", "session_old", 9.0, "cached", "2026-09-13T10:00:00+00:00")
            write_client_item(cache, "new_001", "session_new", 7.5, "cached", "2026-09-13T10:10:00+00:00")
            write_client_item(cache, "new_002", "session_new", 4.0, "playing", "2026-09-13T10:10:01+00:00")
            write_client_item(cache, "new_003", "session_new", 5.5, "buffering", "2026-09-13T10:10:02+00:00")
            state = AudioClient("http://server:8000", cache).local_playback_state()
            self.assertEqual(state["session_id"], "session_new")
            self.assertEqual(state["buffered_segments"], 2)
            self.assertAlmostEqual(state["buffered_seconds"], 13.0, places=2)
            self.assertEqual(state["playback_status"], "playing")

    def test_local_playback_state_marks_played_only_session_as_rebuffering(self):
        with tempfile.TemporaryDirectory() as directory:
            cache = Path(directory)
            write_client_item(cache, "played_001", "session_rebuffer", 5.0, "played", "2026-09-13T10:00:00+00:00")
            state = AudioClient("http://server:8000", cache).local_playback_state()
            self.assertEqual(state["buffered_segments"], 0)
            self.assertEqual(state["playback_status"], "rebuffering")

    def test_next_poll_reports_client_buffer_even_when_server_has_no_audio(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manager = AudioCacheManager(root / "server", lambda audio, _metadata: audio)
            server = AudioCacheServer(("127.0.0.1", 0), make_handler(manager))
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                cache = root / "client"
                cache.mkdir()
                write_client_item(cache, "live_001", "live_session_1", 18.25, "cached", "2026-09-13T10:20:00+00:00")
                client = AudioClient(f"http://127.0.0.1:{server.server_port}", cache)
                self.assertIsNone(client.fetch_next())
                state = server.client_state("live_session_1")
                self.assertEqual(state["session_id"], "live_session_1")
                self.assertEqual(state["buffered_segments"], 1)
                self.assertAlmostEqual(state["buffered_seconds"], 18.25, places=2)
                self.assertTrue(server.client_connected())
            finally:
                server.shutdown()
                server.server_close()

    def test_live_startup_buffer_waits_for_12_seconds_before_playback(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manager = AudioCacheManager(root / "server", lambda audio, _metadata: audio)
            session_id = "live-startup-buffer"
            for index, duration in enumerate((5.0, 4.0, 4.0), 1):
                manager.add_audio(wav_bytes(), {
                    "sequence": index,
                    "session_id": session_id,
                    "duration": duration,
                    "source_audio_sha256": f"{index:064x}",
                    "playback_speed": 1.0,
                    "volume": 100.0,
                    "session_final": False,
                }, f"live_{index:03d}")
            server = AudioCacheServer(("127.0.0.1", 0), make_handler(manager))
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                client = AudioClient(f"http://127.0.0.1:{server.server_port}", root / "client")
                first = client.fetch_next()
                self.assertEqual(first.metadata["playback_status"], "buffering")
                client.ack(first.id)
                self.assertAlmostEqual(client.local_playback_state()["buffered_seconds"], 5.0, places=2)

                second = client.fetch_next()
                self.assertEqual(second.metadata["playback_status"], "buffering")
                client.ack(second.id)
                self.assertAlmostEqual(client.local_playback_state()["buffered_seconds"], 9.0, places=2)

                third = client.fetch_next()
                self.assertEqual(third.metadata["playback_status"], "cached")
                for item_id in ("live_001", "live_002", "live_003"):
                    metadata = json.loads((root / "client" / f"{item_id}.json").read_text(encoding="utf-8"))
                    self.assertEqual(metadata["playback_status"], "cached")
                self.assertAlmostEqual(client.local_playback_state()["buffered_seconds"], 13.0, places=2)
            finally:
                server.shutdown()
                server.server_close()

    def test_short_live_session_releases_startup_buffer_on_final_segment(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manager = AudioCacheManager(root / "server", lambda audio, _metadata: audio)
            manager.add_audio(wav_bytes(), {
                "sequence": 1,
                "session_id": "live-short",
                "duration": 6.0,
                "source_audio_sha256": "a" * 64,
                "playback_speed": 1.0,
                "volume": 100.0,
                "session_final": True,
            }, "short_001")
            server = AudioCacheServer(("127.0.0.1", 0), make_handler(manager))
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                client = AudioClient(f"http://127.0.0.1:{server.server_port}", root / "client")
                item = client.fetch_next()
                self.assertEqual(item.metadata["playback_status"], "cached")
                self.assertAlmostEqual(client.local_playback_state()["buffered_seconds"], 6.0, places=2)
            finally:
                server.shutdown()
                server.server_close()

    def test_duplicate_live_audio_reuses_windows_content_cache(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manager = AudioCacheManager(root / "server", lambda audio, metadata: audio)
            server = AudioCacheServer(("127.0.0.1", 0), make_handler(manager))
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                client = AudioClient(f"http://127.0.0.1:{server.server_port}", root / "client")
                encoded = base64.b64encode(wav_bytes()).decode("ascii")
                source_hash = "b" * 64
                for index, session_id in enumerate(("session-a", "session-b"), 1):
                    response = client._request_json("POST", "audio/enqueue", {
                        "id": f"duplicate_{index:03d}",
                        "sequence": index,
                        "session_id": session_id,
                        "source": "live_session",
                        "voice": "default",
                        "playback_speed": 1.0,
                        "volume": 100.0,
                        "source_audio_sha256": source_hash,
                        "round_position": 1,
                        "round_total": 1,
                        "looping": False,
                        "session_final": True,
                        "audio_base64": encoded,
                    })
                    self.assertEqual(response.status, 201)

                first = client.fetch_next()
                self.assertEqual(first.metadata["content_cache"], "miss")
                self.assertEqual(first.metadata["server_metadata"]["source_audio_sha256"], source_hash)
                self.assertTrue(first.metadata["server_metadata"]["session_final"])
                client.ack(first.id)

                second = client.fetch_next()
                self.assertEqual(second.metadata["content_cache"], "hit")
                self.assertEqual(first.path.read_bytes(), second.path.read_bytes())
                self.assertEqual(len(list((root / "client" / "blobs").glob("*.wav"))), 1)
            finally:
                server.shutdown()
                server.server_close()

    def test_duplicate_float_audio_reuses_prepared_content_cache(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manager = AudioCacheManager(root / "server", lambda audio, _metadata: audio)
            server = AudioCacheServer(("127.0.0.1", 0), make_handler(manager))
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                client = AudioClient(f"http://127.0.0.1:{server.server_port}", root / "client")
                encoded = base64.b64encode(float_wav_bytes()).decode("ascii")
                source_hash = "c" * 64
                for index, session_id in enumerate(("float-a", "float-b"), 1):
                    response = client._request_json("POST", "audio/enqueue", {
                        "id": f"float_duplicate_{index:03d}",
                        "sequence": index,
                        "session_id": session_id,
                        "source": "live_session",
                        "source_audio_sha256": source_hash,
                        "playback_speed": 1.0,
                        "volume": 100.0,
                        "session_final": True,
                        "audio_base64": encoded,
                    })
                    self.assertEqual(response.status, 201)

                first = client.fetch_next()
                client.ack(first.id)
                first_metadata = json.loads((root / "client" / f"{first.id}.json").read_text())
                prepared = root / "client" / first_metadata["prepared_path"]
                self.assertTrue(prepared.is_file())
                second = client.fetch_next()
                client.ack(second.id)
                second_metadata = json.loads((root / "client" / f"{second.id}.json").read_text())
                self.assertEqual(second_metadata["prepared_path"], first_metadata["prepared_path"])
            finally:
                server.shutdown()
                server.server_close()


if __name__ == "__main__":
    unittest.main()
