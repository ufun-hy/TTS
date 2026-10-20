from email.message import Message
from email.utils import formatdate
import http.client
import io
import socket
import ssl
import unittest
from unittest.mock import patch
from urllib.error import HTTPError, URLError

from timeline.tts_client import RetryableTTSClientError, TTSClient, TTSClientError


def http_error(status, retry_after=None):
    headers = Message()
    if retry_after is not None:
        headers["Retry-After"] = retry_after
    return HTTPError("http://gateway/synthesize", status, "upstream error", headers, io.BytesIO())


class TTSClientTests(unittest.TestCase):
    def test_429_waits_for_retry_after_before_next_request(self):
        events = []

        def urlopen(*_args, **_kwargs):
            events.append("request")
            if len(events) == 1:
                raise http_error(429, "1")
            return io.BytesIO(b"RIFFtest")

        with patch("timeline.tts_client.request.urlopen", side_effect=urlopen), patch(
            "timeline.tts_client.time.sleep", side_effect=lambda delay: events.append(delay)
        ):
            audio, _ = TTSClient().synthesize("text", "default")
        self.assertEqual(audio, b"RIFFtest")
        self.assertEqual(events, ["request", 1.0, "request"])

    def test_retry_after_http_date_and_invalid_header_fallback(self):
        cases = [(formatdate(1060, usegmt=True), 60), (None, None), ("bad", None),
                 ("-1", None), ("nan", None), ("inf", None), ("0", 0)]
        for header, expected in cases:
            with self.subTest(header=header), patch("timeline.tts_client.time.time", return_value=1000), patch(
                "timeline.tts_client.request.urlopen", side_effect=http_error(429, header)
            ):
                with self.assertRaises(RetryableTTSClientError) as raised:
                    TTSClient().synthesize("text", "default", retries=0)
                self.assertEqual(raised.exception.retry_after, expected)
                self.assertGreaterEqual(raised.exception.retry_delay(1), 1)

    def test_missing_retry_after_uses_bounded_exponential_backoff(self):
        with patch("timeline.tts_client.request.urlopen", side_effect=lambda *_a, **_k: (_ for _ in ()).throw(http_error(503))), patch(
            "timeline.tts_client.time.sleep"
        ) as sleep:
            with self.assertRaises(RetryableTTSClientError):
                TTSClient().synthesize("text", "default", retries=7)
        self.assertEqual([call.args[0] for call in sleep.call_args_list], [1, 2, 4, 8, 16, 30, 30])

    def test_transient_http_and_transport_failures_remain_typed_after_exhaustion(self):
        failures = [http_error(status) for status in (429, 502, 503, 504)] + [
            URLError(TimeoutError("timeout")), socket.timeout("socket timed out"),
            ConnectionResetError("reset"),
            URLError(ConnectionRefusedError("refused")),
            URLError(socket.gaierror(socket.EAI_AGAIN, "temporary DNS")),
            http.client.IncompleteRead(b"partial"), http.client.RemoteDisconnected("closed"),
        ]
        for failure in failures:
            with self.subTest(failure=failure), patch("timeline.tts_client.request.urlopen", side_effect=failure), patch(
                "timeline.tts_client.time.sleep"
            ):
                with self.assertRaises(RetryableTTSClientError) as raised:
                    TTSClient().synthesize("text", "default")
                self.assertTrue(raised.exception.retryable)

    def test_permanent_errors_are_not_retried(self):
        failures = [http_error(status) for status in (400, 401, 403, 404)] + [
            URLError(ssl.SSLCertVerificationError("bad certificate")),
            URLError(socket.gaierror(socket.EAI_NONAME, "unknown host")),
        ]
        for failure in failures:
            with self.subTest(failure=failure), patch("timeline.tts_client.request.urlopen", side_effect=failure) as call, patch(
                "timeline.tts_client.time.sleep"
            ) as sleep:
                with self.assertRaises(TTSClientError) as raised:
                    TTSClient().synthesize("text", "default", retries=5)
                self.assertFalse(raised.exception.retryable)
                call.assert_called_once()
                sleep.assert_not_called()

    def test_non_wav_response_fails_without_retry(self):
        with patch("timeline.tts_client.request.urlopen", return_value=io.BytesIO(b"invalid")) as call:
            with self.assertRaisesRegex(TTSClientError, "non-WAV"):
                TTSClient().synthesize("text", "default")
            call.assert_called_once()


if __name__ == "__main__":
    unittest.main()
