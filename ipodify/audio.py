"""Audio engine: librespot (Spotify) or ffmpeg (iPod files) → PCM → PipeWire, with a real FFT analyzer."""

from __future__ import annotations

import collections
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

import numpy as np

from .core import CACHE, CONFIG, ROOT, reap_with_us
from .spotify import DEVICE_NAME

RATE, CHANNELS = 44100, 2
BYTES_PER_SEC = RATE * CHANNELS * 2
LIBRESPOT = os.environ.get("IPODIFY_LIBRESPOT") or shutil.which("librespot")
URL_RE = re.compile(r"https://accounts\.spotify\.com/\S+")


class Output:
    """Raw s16le stereo into PipeWire (via pacat, falling back to pw-cat). Writes block = pacing."""

    LATENCY = 0.08

    def __init__(self) -> None:
        self.proc: subprocess.Popen | None = None
        self.lock = threading.Lock()

    def _start(self) -> None:
        if shutil.which("pacat"):
            cmd = ["pacat", "--playback", "--raw", "--format=s16le", f"--rate={RATE}", f"--channels={CHANNELS}",
                   f"--latency-msec={int(self.LATENCY * 1000)}", "--client-name=iPodify", "--stream-name=Wiedergabe"]
        else:
            cmd = ["pw-cat", "--playback", "--raw", "--format", "s16", "--rate", str(RATE),
                   "--channels", str(CHANNELS), "--latency", f"{int(self.LATENCY * 1000)}ms", "-"]
        self.proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    LEAD = 0.1  # how far we run ahead of the wall clock

    deadline = 0.0

    @classmethod
    def delay(cls) -> float:
        """Roughly how long until a sample written now is heard."""
        return cls.LEAD + cls.LATENCY

    def write(self, data: bytes) -> None:
        # pace in real time ourselves: pacat would otherwise swallow seconds of audio at once
        now = time.monotonic()
        self.deadline = max(self.deadline, now) + len(data) / BYTES_PER_SEC
        if (ahead := self.deadline - now - self.LEAD) > 0:
            time.sleep(ahead)
        with self.lock:
            for _ in range(2):
                if not self.proc or self.proc.poll() is not None:
                    self._start()
                try:
                    self.proc.stdin.write(data)
                    return
                except (BrokenPipeError, OSError):
                    self.proc = None

    def close(self) -> None:
        if self.proc:
            self.proc.kill()


class Analyzer:
    """Keeps the last second of audio and returns log-spaced band levels (0..1) for what is audible *now*."""

    WINDOW = 2048

    def __init__(self) -> None:
        self.ring = np.zeros(RATE, dtype=np.float32)
        self.pos = 0
        self.last_push = 0.0
        self.lock = threading.Lock()
        self.hann = np.hanning(self.WINDOW).astype(np.float32)
        self.delay: float | None = None  # None = audio we play ourselves (output latency applies)
        self._edges: dict[int, np.ndarray] = {}

    def push(self, mono: np.ndarray) -> None:
        with self.lock:
            n = len(mono)
            if n >= len(self.ring):
                self.ring[:] = mono[-len(self.ring):]
                self.pos = 0
            else:
                end = self.pos + n
                if end <= len(self.ring):
                    self.ring[self.pos:end] = mono
                else:
                    k = len(self.ring) - self.pos
                    self.ring[self.pos:] = mono[:k]
                    self.ring[:n - k] = mono[k:]
                self.pos = end % len(self.ring)
            self.last_push = time.monotonic()

    def bands(self, n: int, delay: float | None = None) -> np.ndarray:
        if time.monotonic() - self.last_push > 0.4 or n <= 0:
            return np.zeros(max(n, 0))
        if delay is None:
            delay = Output.delay() if self.delay is None else self.delay
        with self.lock:
            end = (self.pos - int(delay * RATE)) % len(self.ring)
            idx = (np.arange(end - self.WINDOW, end)) % len(self.ring)
            frame = self.ring[idx] * self.hann
        mag = np.abs(np.fft.rfft(frame)) / (self.WINDOW / 4)
        if n not in self._edges:
            freqs = np.geomspace(40, 16000, n + 1)
            self._edges[n] = np.clip((freqs * self.WINDOW / RATE).astype(int), 1, len(mag) - 1)
        e = self._edges[n]
        levels = np.array([mag[e[i]:max(e[i + 1], e[i] + 1)].max() for i in range(n)])
        centers = np.sqrt(np.geomspace(40, 16000, n + 1)[:-1] * np.geomspace(40, 16000, n + 1)[1:])
        db = 20 * np.log10(levels + 1e-9) + 3.0 * np.log2(centers / 1000)  # tilt: music falls off ~3 dB/oct
        return np.clip((db + 68) / 60, 0, 1)


class Engine:
    """One audio path, two sources. `on_event(name, data)` is called from worker threads."""

    def __init__(self, on_event) -> None:
        self.on_event = on_event
        self.out = Output()
        self.analyzer = Analyzer()
        self.volume = int(CONFIG.get("volume", 70))
        self.source: str | None = None  # "spotify" | "local"
        self.log: collections.deque[str] = collections.deque(maxlen=200)

        # spotify / librespot
        self.librespot: subprocess.Popen | None = None
        self.connected = False
        self.sp: dict = {}
        self.sp_playing = False
        self.sp_pos_ms = 0
        self.sp_pos_t = time.monotonic()

        # local file
        self.local_proc: subprocess.Popen | None = None
        self.local_gen = 0
        self.local_run = threading.Event()
        self.local_pos = 0.0
        self.local_path = ""
        self.local_info: dict = {}

        self.sock_path = Path(tempfile.gettempdir()) / f"ipodify-ev-{os.getpid()}.sock"
        self._sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        self.sock_path.unlink(missing_ok=True)
        self._sock.bind(str(self.sock_path))
        threading.Thread(target=self._event_loop, daemon=True).start()

    # shared PCM path ---------------------------------------------------------
    def _pcm(self, data: bytes) -> None:
        if len(data) % 4:
            data = data[: len(data) - len(data) % 4]
        s = np.frombuffer(data, dtype=np.int16).reshape(-1, 2)
        self.analyzer.push(s.mean(axis=1, dtype=np.float32) / 32768.0)
        gain = (self.volume / 100) ** 2
        self.out.write(data if gain >= 0.999 else (s * gain).astype(np.int16).tobytes())

    def set_volume(self, v: int) -> None:
        self.volume = max(0, min(100, v))

    # librespot ---------------------------------------------------------------
    @property
    def librespot_running(self) -> bool:
        return bool(self.librespot and self.librespot.poll() is None)

    def has_credentials(self) -> bool:
        return (CACHE / "librespot" / "credentials.json").exists()

    def start_librespot(self) -> None:
        """Spotify Connect needs librespot's own login: a Web-API token from a developer app is
        accepted by the access point but rejected for Connect (INVALID_CREDENTIALS). So the first
        start runs librespot's OAuth (it opens the browser itself) and caches the credentials."""
        if not LIBRESPOT:
            raise RuntimeError("librespot fehlt – ./setup.sh ausführen")
        if self.librespot_running:
            return
        cache = CACHE / "librespot"
        cache.mkdir(parents=True, exist_ok=True)
        hook = cache / "onevent"
        hook.write_text(f"#!/bin/sh\nexec '{sys.executable}' -I -S '{ROOT / 'ipodify' / 'onevent.py'}'\n")
        hook.chmod(0o755)
        cmd = [LIBRESPOT, "--name", DEVICE_NAME, "--device-type", "computer", "--backend", "pipe",
               "--format", "S16", "--bitrate", str(CONFIG.get("bitrate", 320)), "--cache", str(cache),
               "--disable-audio-cache", "--disable-discovery", "--volume-ctrl", "fixed",
               "--initial-volume", "100", "--onevent", str(hook)]
        if CONFIG.get("normalize", True):
            cmd.append("--enable-volume-normalisation")
        if not self.has_credentials():
            cmd.append("--enable-oauth")
            self.on_event("oauth_started", {})
        self.librespot = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, stdin=subprocess.DEVNULL,
            env=os.environ | {"IPODIFY_EVENT_SOCK": str(self.sock_path)}, bufsize=0)
        reap_with_us(self.librespot)
        threading.Thread(target=self._librespot_audio, args=(self.librespot,), daemon=True).start()
        threading.Thread(target=self._librespot_log, args=(self.librespot,), daemon=True).start()

    def stop_librespot(self) -> None:
        if self.librespot_running:
            self.librespot.terminate()
        self.connected = False

    def _librespot_audio(self, proc: subprocess.Popen) -> None:
        while chunk := proc.stdout.read(16384):
            if self.source == "spotify":
                self._pcm(chunk)
            else:  # we asked Spotify to pause; drop stray audio in real time
                time.sleep(len(chunk) / BYTES_PER_SEC)

    def _librespot_log(self, proc: subprocess.Popen) -> None:
        for raw in iter(proc.stderr.readline, b""):
            line = raw.decode(errors="replace").strip()
            self.log.append(line)
            if m := URL_RE.search(line):
                self.on_event("oauth_url", {"url": m.group(0)})
            if any(s in line for s in ("Bad credentials", "BadCredentials", "INVALID_CREDENTIALS")):
                (CACHE / "librespot" / "credentials.json").unlink(missing_ok=True)
                self.on_event("auth_failed", {"line": line})
        self.connected = False
        self.on_event("librespot_exit", {"code": proc.wait()})

    def _event_loop(self) -> None:
        while True:
            try:
                ev = json.loads(self._sock.recv(65536))
            except (OSError, ValueError):
                continue
            name = ev.get("PLAYER_EVENT", "")
            pos = ev.get("POSITION_MS")
            if name == "session_connected":
                self.connected = True
            elif name == "session_disconnected":
                self.connected = False
            elif name == "track_changed":
                self.sp = {
                    "uri": ev.get("URI", ""), "id": ev.get("TRACK_ID", "").split(":")[-1],
                    "title": ev.get("NAME", ""), "artist": ", ".join(ev.get("ARTISTS", "").split("\n")),
                    "album": ev.get("ALBUM", ""), "dur": int(ev.get("DURATION_MS") or 0) / 1000,
                }
                self.sp_pos_ms, self.sp_pos_t = 0, time.monotonic()
            elif name in ("playing", "paused", "seeked", "position_correction"):
                if pos is not None:
                    self.sp_pos_ms, self.sp_pos_t = int(pos), time.monotonic()
                if name in ("playing", "paused"):
                    self.sp_playing = name == "playing"
                    if self.sp_playing and self.source != "spotify":
                        self._stop_local()
                        self.source = "spotify"
            elif name == "stopped":
                self.sp_playing = False
            self.on_event(name, ev)

    # monitor (official Spotify app plays itself; we only listen for the analyzer) ---
    def start_monitor(self) -> None:
        if getattr(self, "_monitor", None) and self._monitor.poll() is None:
            return
        if shutil.which("parec"):
            cmd = ["parec", "--device=@DEFAULT_MONITOR@", "--raw", "--format=s16le", f"--rate={RATE}",
                   f"--channels={CHANNELS}", "--latency-msec=30", "--client-name=iPodify-Analyzer"]
        else:
            cmd = ["pw-record", "--raw", "-P", "stream.capture.sink=true", "--format", "s16",
                   "--rate", str(RATE), "--channels", str(CHANNELS), "-"]
        self._monitor = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                         stdin=subprocess.DEVNULL)
        reap_with_us(self._monitor)
        threading.Thread(target=self._monitor_loop, args=(self._monitor,), daemon=True).start()

    def _monitor_loop(self, proc: subprocess.Popen) -> None:
        while chunk := proc.stdout.read(4096):
            if self.source == "local" or len(chunk) % 4:
                continue  # our own output path feeds the analyzer then
            s = np.frombuffer(chunk, dtype=np.int16).reshape(-1, 2)
            self.analyzer.delay = 0.03
            self.analyzer.push(s.mean(axis=1, dtype=np.float32) / 32768.0)

    def stop_monitor(self) -> None:
        if getattr(self, "_monitor", None) and self._monitor.poll() is None:
            self._monitor.kill()

    # local files -------------------------------------------------------------
    def play_local(self, path: str, start: float = 0.0, info: dict | None = None) -> None:
        self._stop_local()
        self.analyzer.delay = None
        self.source = "local"
        self.local_gen += 1
        gen = self.local_gen
        self.local_pos = start
        self.local_path = path
        if info is not None:
            self.local_info = info
        self.local_run.set()
        self.local_proc = subprocess.Popen(
            ["ffmpeg", "-nostdin", "-loglevel", "error",
             *(["-reconnect", "1", "-reconnect_streamed", "1"] if path.startswith("http") else []),
             "-ss", f"{start:.2f}", "-i", path,
             "-f", "s16le", "-ac", "2", "-ar", str(RATE), "-"],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        threading.Thread(target=self._local_audio, args=(self.local_proc, gen), daemon=True).start()

    def _local_audio(self, proc: subprocess.Popen, gen: int) -> None:
        while gen == self.local_gen:
            self.local_run.wait()
            chunk = proc.stdout.read(8192)
            if not chunk:
                break
            if gen != self.local_gen:
                return
            self._pcm(chunk)
            self.local_pos += len(chunk) / BYTES_PER_SEC
        proc.wait()
        if gen == self.local_gen:
            self.local_info = {**self.local_info, "ended": True}
            self.on_event("local_end", {})

    def _stop_local(self) -> None:
        self.local_gen += 1
        self.local_run.set()
        if self.local_proc and self.local_proc.poll() is None:
            self.local_proc.kill()
        self.local_proc = None

    def pause_local(self, paused: bool) -> None:
        (self.local_run.clear if paused else self.local_run.set)()

    def suspend_local(self, under: str):
        """Stop ffmpeg if it is reading a file below `under` (e.g. the iPod mount), so the volume
        can be unmounted. Returns a callback that resumes at the same position, or None."""
        path = self.local_path
        if not (self.source == "local" and self.local_proc and self.local_proc.poll() is None
                and path and not path.startswith("http") and path.startswith(under.rstrip("/") + "/")):
            return None
        pos, paused, info = max(0.0, self.local_pos - Output.delay()), not self.local_run.is_set(), self.local_info
        proc = self.local_proc
        self._stop_local()
        try:
            proc.wait(timeout=3)  # the file must really be closed before the unmount
        except subprocess.TimeoutExpired:
            pass

        def resume() -> None:
            self.play_local(path, pos, info=info)
            if paused:
                self.pause_local(True)
        return resume

    def stop(self) -> None:
        self._stop_local()
        self.local_info = {}
        if self.source == "local":
            self.source = None

    # status for the UI ----------------------------------------------------------
    def status(self) -> dict:
        if self.source == "local" and self.local_info and not self.local_info.get("ended"):
            pos = max(0.0, self.local_pos - Output.delay())
            return {"source": "local", **self.local_info, "pos": pos, "playing": self.local_run.is_set(),
                    "paused": not self.local_run.is_set()}
        if self.source == "spotify" and self.sp:
            pos = self.sp_pos_ms / 1000
            if self.sp_playing:
                pos += time.monotonic() - self.sp_pos_t
            dur = self.sp.get("dur") or 0
            return {"source": "spotify", **self.sp, "pos": min(pos, dur) if dur else pos,
                    "playing": self.sp_playing, "paused": not self.sp_playing,
                    "kbps": int(CONFIG.get("bitrate", 320))}
        return {"source": None, "playing": False, "paused": False}

    def close(self) -> None:
        self._stop_local()
        self.stop_monitor()
        self.stop_librespot()
        self.out.close()
        self.sock_path.unlink(missing_ok=True)
