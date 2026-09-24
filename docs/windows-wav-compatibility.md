# Windows WAV compatibility

CosyVoice produces 24 kHz, mono IEEE Float32 WAV (`format code 3`). The Mac
keeps the existing live-session fast path: at speed 1 and volume 100 it sends
that original WAV to Audio Cache without FFmpeg. Windows MCI `waveaudio` does
not reliably open this format, so the Windows playback boundary converts it
just before `open`.

`audio_client/wav_compat.py` exposes `prepare_mci_wav(source: Path) -> Path`.
`WinMMPlayer.play()` calls it before issuing the MCI `open` command. PCM16 WAV
files are validated and returned unchanged. Float32 WAV files, including a
valid IEEE_FLOAT WAVE_FORMAT_EXTENSIBLE subtype, are converted to standard
little-endian PCM16 while preserving sample rate, channel count and frame
count. Samples are clipped to `[-1, 1]`; NaN, Infinity, malformed RIFF chunks,
truncated data, invalid alignment and unknown encodings fail with a concrete
`PlaybackError` before MCI sees the file.

The original download stays in the normal cache directory. A derived file is
stored below `pcm16/` with the source name, source size, source `mtime_ns` and
conversion version in its name. A valid file for the same source version is
reused. Conversion writes a temporary sibling, validates the completed PCM16
WAV, checks the source signature again and atomically replaces the destination.
No automatic cleanup removes originals, derived files, logs or failed items.

When the user presses `开始播放`, the playback worker checks failed Float32
items belonging to the currently selected/newest live session. A source
version is attempted at most once; the result and attempt count are recorded
as optional `wav_compat_recovery` metadata. Successful conversion changes the
item back to `cached` and lets the normal sequence queue play it. PCM16
failures, invalid files, played files and other sessions are not requeued.
Pause, resume, stop, ordering, download completion and server ACK semantics do
not change. Conversion runs in the existing playback worker, so the GUI and
download worker remain responsive.

## Tests

Run on any platform for parser, conversion, cache and controller coverage:

```bash
python3 -m unittest discover -s tests -p 'test_wav_compat.py'
python3 -m unittest discover -s tests -p 'test_playback.py'
python3 -m unittest discover -s tests -p 'test_winmm_smoke.py'
```

`test_winmm_smoke.py` includes a platform-independent MCI command seam test;
its real WinMM smoke test is skipped unless running on Windows. The actual
Windows MCI acceptance is not complete in this environment because no Windows
machine or MCI device is available. It must be run with the updated executable:

1. Preserve one real failed cache file and its metadata. Inspect it with
   `ffprobe` or a WAV tool and reproduce the original MCI open error.
2. Install the build from `build/windows/output/AI-Audio-Client-Setup.exe`,
   start the client and point it at that cache. Click `开始播放`; verify the
   derived `cache/pcm16/` file opens and plays through the same MCI player.
3. Exercise pause, resume, stop and close. Confirm at least ten segments play
   once and in `sequence` order, and that a second start reuses the derived
   file without a second conversion.
4. Start a live session from Text Studio and verify both newly downloaded
   Float32 segments and the preserved failed segment recover. Confirm the
   client log records source format, cache hit/miss, conversion duration,
   output format and error category without logging script text.
5. Verify the Mac default live fast path still avoids FFmpeg. The Mac-side
   Float32 WAV remains unchanged; only the Windows playback client adapts it.

The Windows package is built by the `Build Windows AI Audio Client` GitHub Actions workflow on `windows-latest`. A push to `main` that changes the client/build paths triggers it; it can also be started manually from the Actions page. The local Windows build flow remains:

```powershell
python -m pip install pyinstaller
.\build\windows\build.ps1
```

The expected deliverable is `build/windows/output/AI-Audio-Client-Setup.exe`.
The GitHub workflow uploads that installer as a 30-day Actions artifact. The
Windows MCI real-device acceptance must still be completed on a Windows client.
