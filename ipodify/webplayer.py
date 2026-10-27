"""Spotify Web Playback SDK in a headless Chromium: a lightweight Connect device named like DEVICE_NAME.

A tiny local HTTP server serves the SDK page, hands it fresh tokens and receives its events.
Chromium runs with its own profile (only the Widevine CDM is copied in from the user's Chromium).
"""

from __future__ import annotations

import http.server
import json
import os
import shutil
import signal
import subprocess
import threading
import time
from pathlib import Path

from .core import CACHE, reap_with_us

PAGE = """<!doctype html><meta charset="utf-8"><title>iPodify Player</title>
<script>
const post = (kind, data) => fetch('/event', {method: 'POST', body: JSON.stringify({kind, ...data})});
window.onSpotifyWebPlaybackSDKReady = () => {
  const player = new Spotify.Player({
    name: %(name)s,
    getOAuthToken: cb => fetch('/token').then(r => r.text()).then(cb),
    volume: 1.0,
  });
  player.addListener('ready', ({device_id}) => post('ready', {device_id}));
  player.addListener('not_ready', ({device_id}) => post('not_ready', {device_id}));
  for (const e of ['initialization_error', 'authentication_error', 'account_error', 'playback_error'])
    player.addListener(e, ({message}) => post('error', {type: e, message}));
  player.addListener('autoplay_failed', () => post('error', {type: 'autoplay_failed', message: ''}));
  player.connect().then(ok => post('connected', {ok}));
  window.player = player;
};
window.onerror = (msg) => post('error', {type: 'js', message: String(msg)});
</script>
<script src="https://sdk.scdn.co/spotify-player.js"></script>
"""


# Keep the invisible browser small: one renderer, no site isolation, no background services.
LEAN_FLAGS = [
    "--renderer-process-limit=1", "--disable-site-isolation-trials", "--disable-sync",
    "--disable-background-networking", "--disable-default-apps",
    "--disable-breakpad", "--no-pings", "--metrics-recording-only",
    "--disable-features=Translate,MediaRouter,OptimizationHints,AutofillServerCommunication,"
    "CalculateNativeWinOcclusion,InterestFeedContentSuggestions,SpareRendererForSitePerProcess",
    "--js-flags=--max-old-space-size=128",
]


def chromium_binary() -> str | None:
    return next((p for p in map(shutil.which, ("chromium", "chromium-browser", "google-chrome-stable")) if p), None)


class WebPlayer:
    def __init__(self, name: str, get_token, on_event) -> None:
        self.name, self.get_token, self.on_event = name, get_token, on_event
        self.proc: subprocess.Popen | None = None
        self.server: http.server.HTTPServer | None = None
        self.device_id: str | None = None
        self.profile = CACHE / "chromium-player"

    @property
    def running(self) -> bool:
        return bool(self.proc and self.proc.poll() is None)

    def _prepare_profile(self) -> None:
        """Copy only the Widevine CDM from the user's Chromium so DRM playback works at once."""
        self.profile.mkdir(parents=True, exist_ok=True)
        dest = self.profile / "WidevineCdm"
        src = Path.home() / ".config" / "chromium" / "WidevineCdm"
        if not dest.exists() and src.is_dir():
            shutil.copytree(src, dest, symlinks=True)

    def _serve(self) -> int:
        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def _send(self, body: bytes, ctype: str = "text/plain") -> None:
                self.send_response(200)
                self.send_header("Content-Type", ctype)
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):  # noqa: N802
                if self.path == "/":
                    self._send((PAGE % {"name": json.dumps(outer.name)}).encode(), "text/html; charset=utf-8")
                elif self.path == "/token":
                    self._send((outer.get_token() or "").encode())
                else:
                    self.send_error(404)

            def do_POST(self):  # noqa: N802
                body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
                self._send(b"ok")
                try:
                    ev = json.loads(body)
                except ValueError:
                    return
                if ev.get("kind") == "ready":
                    outer.device_id = ev.get("device_id")
                outer.on_event(ev.get("kind", ""), ev)

            def log_message(self, *args):
                pass

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        return self.server.server_address[1]

    def _kill_stale(self) -> None:
        """A Chromium left over from a crashed iPodify would grab our profile (one instance per profile)."""
        marker = f"--user-data-dir={self.profile}".encode()
        stale = []
        for d in Path("/proc").iterdir():
            if d.name.isdigit():
                try:
                    if marker in (d / "cmdline").read_bytes():
                        stale.append(int(d.name))
                except OSError:
                    pass
        for sig in (signal.SIGTERM, signal.SIGKILL):
            for pid in stale:
                try:
                    os.kill(pid, sig)
                except ProcessLookupError:
                    pass
            if stale and sig == signal.SIGTERM:
                time.sleep(1)
        for lock in ("SingletonLock", "SingletonSocket", "SingletonCookie"):
            (self.profile / lock).unlink(missing_ok=True)

    def start(self) -> None:
        if self.running:
            return
        exe = chromium_binary()
        if not exe:
            raise RuntimeError("Chromium fehlt für den Web-Player")
        self._prepare_profile()
        self._kill_stale()
        port = self._serve()
        self.proc = subprocess.Popen(
            [exe, "--headless=new", f"--user-data-dir={self.profile}", "--no-first-run",
             "--no-default-browser-check", "--disable-extensions", "--disable-gpu",
             "--autoplay-policy=no-user-gesture-required", "--disable-background-timer-throttling",
             "--disable-renderer-backgrounding", *LEAN_FLAGS,
             # without a copied Widevine CDM, let Chromium's component updater fetch one
             *(["--disable-component-update"] if (self.profile / "WidevineCdm").exists() else []),
             f"http://127.0.0.1:{port}/"],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        reap_with_us(self.proc, group=True)  # renderer, GPU, audio … go down with it

    def stop(self) -> None:
        if self.proc:
            for sig in (signal.SIGTERM, signal.SIGKILL):  # the whole group: renderer, GPU, audio …
                try:
                    os.killpg(self.proc.pid, sig)
                except ProcessLookupError:
                    break
                try:
                    self.proc.wait(3)
                    break
                except subprocess.TimeoutExpired:
                    pass
            self.proc = None
        if self.server:
            self.server.shutdown()
            self.server = None
        self.device_id = None
