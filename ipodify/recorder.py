"""Record tracks from the Spotify web player in real time, as a last resort for songs no download
source has.

The headless Chromium is restarted with PULSE_SINK pointing at a private null sink, so its audio
never reaches the speakers; `parec` captures the sink's monitor while the Web API plays one track
at a time. The SDK's player_state_changed events mark start and end. Spotify's volume is raised to
100 % for the session (the SDK scales samples before output) and restored afterwards.
"""

from __future__ import annotations

import contextlib
import queue
import subprocess
import threading
import time
import urllib.request
from pathlib import Path

from .core import CACHE, FORMAT, Track, fmt_time, reap_with_us

SINK = "ipodify-rec"
RATE = 44100
REC_DIR = CACHE / "rec"


def _pactl(*args: str) -> str:
    r = subprocess.run(["pactl", *args], capture_output=True, text=True, timeout=20)
    if r.returncode:
        raise RuntimeError(f"pactl {args[0]}: {(r.stderr or r.stdout).strip()}")
    return r.stdout


class Recorder:
    START_TIMEOUT = 40  # seconds until the SDK must report the track playing
    END_SLACK = 120  # seconds past the track's length until we give up waiting for the end

    def __init__(self, api, webplayer, notify, volume) -> None:
        """api: SpotifyAPI · webplayer: WebPlayer · notify(msg, **kw) · volume(): the user's volume."""
        self.api, self.webplayer, self.notify, self.volume = api, webplayer, notify, volume
        self.module = ""
        self.active = False

    # session ---------------------------------------------------------------------
    @contextlib.contextmanager
    def session(self):
        from .spotify import PLAYER

        if PLAYER != "sdk":
            raise RuntimeError('braucht player = "sdk" in config.toml')
        if not self.api.logged_in:
            raise RuntimeError("bei Spotify anmelden")
        self._unload_stale()
        self.module = _pactl("load-module", "module-null-sink", f"sink_name={SINK}", f"rate={RATE}", "channels=2",
                             "sink_properties=device.description=iPodify-Mitschnitt").strip()
        self.active = True
        prev_volume = self.volume()  # before the poller sees the 100 % we set below
        self.notify("Mitschnitt läuft – der Spotify-Player ist währenddessen stumm.", title="⏺ Mitschnitt",
                    timeout=10)
        try:
            self._restart_player({"PULSE_SINK": SINK})
            self._set_volume(100)
            yield
        finally:
            self.active = False
            with contextlib.suppress(Exception):
                self.api.pause()
            with contextlib.suppress(Exception):
                self._restart_player(None)
            with contextlib.suppress(Exception):
                _pactl("unload-module", self.module)
            self.module = ""
            threading.Thread(target=self._set_volume, args=(prev_volume,), daemon=True).start()
            self.notify("Mitschnitt beendet – Spotify-Player wieder normal.", title="⏺ Mitschnitt")

    def _unload_stale(self) -> None:
        for line in _pactl("list", "short", "modules").splitlines():
            f = line.split("\t")
            if len(f) >= 3 and f"sink_name={SINK}" in f[2]:
                with contextlib.suppress(Exception):
                    _pactl("unload-module", f[0])

    def _restart_player(self, env: dict[str, str] | None) -> None:
        self.webplayer.stop()
        self.webplayer.start(env)
        for _ in range(45):
            if self.webplayer.device_id:
                break
            time.sleep(1)
        else:
            raise RuntimeError("Spotify-Player meldet sich nicht")
        for _ in range(20):  # the Web API lists the device a moment later
            with contextlib.suppress(Exception):
                if self.api.find_device(local_only=True) == self.webplayer.device_id:
                    return
            time.sleep(1)
        self.api.device_id = self.webplayer.device_id

    def _set_volume(self, percent: int) -> None:
        for _ in range(10):
            try:
                self.api.set_volume(percent)
                return
            except Exception:  # noqa: BLE001
                time.sleep(2)

    # one track ---------------------------------------------------------------------
    def record(self, track: Track, on_progress=None) -> Path:
        REC_DIR.mkdir(parents=True, exist_ok=True)
        raw = REC_DIR / f"{track.id}.raw"
        events: queue.Queue[dict] = queue.Queue()
        self.webplayer.state_hook = events.put
        dur = track.duration_ms / 1000
        with raw.open("wb") as f:
            parec = subprocess.Popen(["parec", f"--device={SINK}.monitor", "--raw", "--format=s16le",
                                      f"--rate={RATE}", "--channels=2"], stdout=f, stderr=subprocess.DEVNULL)
            reap_with_us(parec)
            try:
                self._play_and_wait(track, events, dur, on_progress)
            finally:
                self.webplayer.state_hook = None
                time.sleep(0.5)  # tail
                parec.terminate()
                with contextlib.suppress(subprocess.TimeoutExpired):
                    parec.wait(5)
        try:
            return self._encode(track, raw, dur)
        finally:
            raw.unlink(missing_ok=True)

    def _play_and_wait(self, track: Track, events: queue.Queue, dur: float, on_progress) -> None:
        self.api.play(uris=[track.uri])
        t0 = time.monotonic()
        started, last, last_t, last_report = False, None, t0, 0.0
        while True:
            try:
                ev = events.get(timeout=1)
            except queue.Empty:
                ev = None
            now = time.monotonic()
            if ev:
                same = ev.get("track") == track.id
                if same and not ev.get("paused"):
                    started = True
                if started and ((ev.get("paused") and not ev.get("position")) or (ev.get("track") and not same)):
                    return
                last, last_t = ev, now
            if not started and now - t0 > self.START_TIMEOUT:
                raise RuntimeError("Wiedergabe startet nicht")
            if now - t0 > dur + self.END_SLACK:
                raise RuntimeError("Spotify meldet kein Ende des Titels")
            if on_progress and now - last_report > 2 and last:
                pos = last.get("position", 0) / 1000 + (0 if last.get("paused") else now - last_t)
                on_progress(f"⏺ {fmt_time(min(pos, dur))} / {fmt_time(dur)}")
                last_report = now

    def _encode(self, track: Track, raw: Path, dur: float) -> Path:
        import mutagen
        from mutagen.id3 import APIC, ID3, TALB, TDRC, TIT2, TPE1, TRCK
        from mutagen.mp4 import MP4, MP4Cover

        out_dir = CACHE / "tracks"
        out_dir.mkdir(parents=True, exist_ok=True)
        tmp = out_dir / f"{track.id}.part.{FORMAT}"
        codec = ["-codec:a", "aac", "-movflags", "+faststart"] if FORMAT == "m4a" else ["-codec:a", "libmp3lame"]
        # drop the silence before playback started and after it ended, nothing else
        trim = ("silenceremove=start_periods=1:start_threshold=-55dB:start_silence=0.05,"
                "areverse,silenceremove=start_periods=1:start_threshold=-55dB:start_silence=0.4,areverse")
        subprocess.run(["ffmpeg", "-nostdin", "-loglevel", "error", "-y", "-f", "s16le", "-ar", str(RATE), "-ac", "2",
                        "-i", str(raw), "-af", trim, *codec, "-b:a", "256k", str(tmp)], check=True, timeout=600)
        length = getattr(getattr(mutagen.File(tmp), "info", None), "length", 0) or 0
        if length < dur * 0.8 - 2:
            tmp.unlink(missing_ok=True)
            raise RuntimeError(f"zu kurz ({fmt_time(length)} statt {fmt_time(dur)}) – lief parallel etwas anderes?")
        cover = b""
        if track.cover:
            with contextlib.suppress(Exception):
                with urllib.request.urlopen(track.cover, timeout=20) as r:
                    cover = r.read()
        if FORMAT == "m4a":
            m = MP4(tmp)
            m["\xa9nam"], m["\xa9ART"], m["\xa9alb"] = track.title, track.artist, track.album
            m["trkn"] = [(track.track_nr or 1, 0)]
            if track.year:
                m["\xa9day"] = str(track.year)
            if cover:
                m["covr"] = [MP4Cover(cover, imageformat=MP4Cover.FORMAT_JPEG)]
            m.save()
        else:
            tags = ID3()
            tags.add(TIT2(encoding=3, text=track.title))
            tags.add(TPE1(encoding=3, text=track.artist))
            tags.add(TALB(encoding=3, text=track.album))
            tags.add(TRCK(encoding=3, text=str(track.track_nr or 1)))
            if track.year:
                tags.add(TDRC(encoding=3, text=str(track.year)))
            if cover:
                tags.add(APIC(encoding=3, mime="image/jpeg", type=3, desc="Cover", data=cover))
            tags.save(tmp, v2_version=3)
        final = out_dir / f"{track.id}.{FORMAT}"
        tmp.replace(final)
        return final
