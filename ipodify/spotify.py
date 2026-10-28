"""Spotify Web API: login (OAuth), search, library, and remote control of our librespot device."""

from __future__ import annotations

import http.server
import re
import socket
import subprocess
import threading
import urllib.parse

import spotipy
from spotipy.cache_handler import CacheFileHandler
from spotipy.oauth2 import SpotifyOAuth

from .core import CONFIG, CONFIG_DIR, Album, Artist, Playlist, Track, load_config, save_config

# Your own app from https://developer.spotify.com/dashboard (see README / the setup dialog).
REDIRECT_URI = CONFIG.get("redirect_uri", "http://127.0.0.1:8898/callback")
DEVICE_NAME = CONFIG.get("device_name", "iPodify")
# "sdk": Web Playback SDK in a headless Chromium (default, light) · "app": remote-control the official
# Spotify app · "librespot": play inside iPodify (Spotify currently refuses its audio keys for many accounts)
PLAYER = CONFIG.get("player", "sdk")
DASHBOARD_URL = "https://developer.spotify.com/dashboard"
SCOPES = " ".join([
    "streaming", "user-read-email", "user-read-private",
    "user-read-playback-state", "user-modify-playback-state", "user-read-currently-playing",
    "user-library-read", "playlist-read-private", "playlist-read-collaborative",
    "user-follow-read", "user-top-read", "user-read-recently-played",
])
URL_RE = re.compile(r"open\.spotify\.com/(?:intl-\w+/)?(track|album|playlist|artist)/(\w+)")
TOKEN_FILE = CONFIG_DIR / "spotify-token.json"


class _TokenAuth:
    """Lets spotipy pull a fresh user token without ever starting an interactive flow."""

    def __init__(self, api: "SpotifyAPI") -> None:
        self.api = api

    def get_access_token(self, as_dict: bool = False):
        return self.api.token()


class SpotifyAPI:
    def __init__(self) -> None:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        # retries=0: a 429 must surface as an error instead of sleeping for Retry-After hours
        self._user_sp = spotipy.Spotify(auth_manager=_TokenAuth(self), requests_timeout=15, retries=0,
                                        status_retries=0)
        self.me: dict | None = None
        self.device_id: str | None = None
        self.device_name = ""
        self._lock = threading.Lock()
        self._oauth: SpotifyOAuth | None = None

    # app credentials -----------------------------------------------------------
    @staticmethod
    def credentials() -> tuple[str, str]:
        c = load_config()
        return c.get("client_id", ""), c.get("client_secret", "")

    @property
    def configured(self) -> bool:
        return all(self.credentials())

    def set_credentials(self, client_id: str, client_secret: str) -> None:
        save_config(client_id=client_id.strip(), client_secret=client_secret.strip())
        self._oauth = None

    @property
    def oauth(self) -> SpotifyOAuth:
        if self._oauth is None:
            cid, secret = self.credentials()
            self._oauth = SpotifyOAuth(
                client_id=cid, client_secret=secret, redirect_uri=REDIRECT_URI, scope=SCOPES,
                cache_handler=CacheFileHandler(cache_path=str(TOKEN_FILE)), open_browser=False,
                requests_timeout=15)
        return self._oauth

    # auth ---------------------------------------------------------------------
    def token(self) -> str | None:
        with self._lock:
            info = self.oauth.validate_token(self.oauth.cache_handler.get_cached_token())
        return info["access_token"] if info else None

    @property
    def logged_in(self) -> bool:
        return self.configured and TOKEN_FILE.exists()

    def login(self, on_url) -> dict:
        """Opens the browser, waits for Spotify's redirect, stores the token."""
        url = self.oauth.get_authorize_url()
        on_url(url)
        code = _wait_for_code(REDIRECT_URI)
        self.oauth.get_access_token(code, as_dict=True, check_cache=False)
        self.me = self._user_sp.current_user()
        return self.me

    def logout(self) -> None:
        TOKEN_FILE.unlink(missing_ok=True)
        self.me, self.device_id = None, None

    def profile(self) -> dict | None:
        if self.me is None and self.logged_in:
            self.me = self._user_sp.current_user()
        return self.me

    @property
    def sp(self) -> spotipy.Spotify:
        if not self.logged_in:
            raise RuntimeError("Bitte zuerst bei Spotify anmelden (L)")
        return self._user_sp

    @property
    def market(self) -> str:
        return "from_token"

    # parsing ------------------------------------------------------------------
    @staticmethod
    def _year(d: str | None) -> int:
        return int(d[:4]) if d and d[:4].isdigit() else 0

    def _track(self, t: dict, album: dict | None = None) -> Track | None:
        if not t or not t.get("id") or t.get("type", "track") != "track":
            return None
        album = album or t.get("album") or {}
        return Track(
            id=t["id"], uri=t["uri"], title=t["name"], artists=[a["name"] for a in t.get("artists", [])],
            album=album.get("name", ""), duration_ms=t.get("duration_ms", 0),
            url=t.get("external_urls", {}).get("spotify", f"https://open.spotify.com/track/{t['id']}"),
            album_uri=album.get("uri", ""), year=self._year(album.get("release_date")),
            track_nr=t.get("track_number", 0),
            cover=next((i["url"] for i in album.get("images", []) if i.get("url")), ""),
        )

    def _album(self, a: dict) -> Album:
        return Album(a["id"], a["uri"], a["name"], [x["name"] for x in a.get("artists", [])],
                     self._year(a.get("release_date")), a.get("total_tracks", 0),
                     a.get("external_urls", {}).get("spotify", ""))

    @staticmethod
    def _playlist(p: dict) -> Playlist:
        total = (p.get("tracks") or p.get("items") or {}).get("total", 0)
        return Playlist(p["id"], p["uri"], p["name"], (p.get("owner") or {}).get("display_name", ""), total,
                        p.get("external_urls", {}).get("spotify", ""))

    @staticmethod
    def _artist(a: dict) -> Artist:
        return Artist(a["id"], a["uri"], a["name"], a.get("genres", []), a.get("external_urls", {}).get("spotify", ""))

    def _pages(self, page: dict | None, limit: int = 5000):
        n = 0
        while page:
            for item in page.get("items", []):
                if item is not None:
                    yield item
                    n += 1
            if n >= limit or not page.get("next"):
                return
            page = self.sp.next(page)

    @staticmethod
    def _limited(fn, *args, limits=(50, 10), **kw):
        """Development-mode apps get a smaller max page size on some endpoints; step down on 400."""
        for i, limit in enumerate(limits):
            try:
                return fn(*args, limit=limit, **kw)
            except spotipy.SpotifyException as e:
                if e.http_status != 400 or i == len(limits) - 1:
                    raise

    # browsing -----------------------------------------------------------------
    def search(self, q: str) -> dict[str, list]:
        if m := URL_RE.search(q):
            kind, sid = m.groups()
            item = self.resolve(kind, sid)
            return {"tracks": [item] if kind == "track" else [], "albums": [item] if kind == "album" else [],
                    "artists": [item] if kind == "artist" else [], "playlists": [item] if kind == "playlist" else []}
        r = self._limited(self.sp.search, q, type="track,album,artist,playlist", market=self.market,
                          limits=(10,))
        items = lambda k: [x for x in (r.get(k) or {}).get("items", []) if x]  # noqa: E731
        return {
            "tracks": [t for t in (self._track(x) for x in items("tracks")) if t],
            "albums": [self._album(x) for x in items("albums")],
            "artists": [self._artist(x) for x in items("artists")],
            "playlists": [self._playlist(x) for x in items("playlists")],
        }

    def resolve(self, kind: str, sid: str):
        if kind == "track":
            return self._track(self.sp.track(sid, market=self.market))
        if kind == "album":
            return self._album(self.sp.album(sid, market=self.market))
        if kind == "artist":
            return self._artist(self.sp.artist(sid))
        return self._playlist(self.sp.playlist(sid, fields="id,uri,name,owner,tracks.total,external_urls"))

    def album_tracks(self, album: Album) -> list[Track]:
        a = self.sp.album(album.id, market=self.market)
        return [t for t in (self._track(x, a) for x in self._pages(a["tracks"])) if t]

    def playlist_tracks(self, pl: Playlist) -> list[Track]:
        try:
            page = self._limited(self.sp.playlist_items, pl.id, market=self.market, additional_types=("track",),
                                 limits=(100, 50))
        except spotipy.SpotifyException as e:
            if e.http_status == 403:
                raise RuntimeError("Spotify erlaubt eigenen Developer-Apps nur Playlists, die dir gehören "
                                   "oder bei denen du mitarbeitest.") from None
            raise
        out = []
        for item in self._pages(page):
            if t := self._track(item.get("track") or item.get("item")):
                out.append(t)
        return out

    def artist_page(self, artist: Artist) -> tuple[list[Track], list[Album]]:
        top: list[Track] = []
        try:
            top = [t for t in (self._track(x) for x in self.sp.artist_top_tracks(artist.id)["tracks"]) if t]
        except spotipy.SpotifyException:
            pass  # not available to development-mode apps
        seen, albums = set(), []
        page = self._limited(self.sp.artist_albums, artist.id, include_groups="album,single", limits=(50, 10))
        for a in self._pages(page, 200):
            if (k := a["name"].lower()) not in seen:
                seen.add(k)
                albums.append(self._album(a))
        return top, albums

    def saved_tracks(self) -> list[Track]:
        page = self.sp.current_user_saved_tracks(limit=50, market=self.market)
        return [t for t in (self._track(i["track"]) for i in self._pages(page, 3000)) if t]

    def saved_albums(self) -> list[Album]:
        return [self._album(i["album"]) for i in self._pages(self.sp.current_user_saved_albums(limit=50))]

    def my_playlists(self) -> list[Playlist]:
        return [self._playlist(p) for p in self._pages(self.sp.current_user_playlists(limit=50))]

    def tracks_of(self, item) -> list[Track]:
        if isinstance(item, Track):
            return [item]
        if isinstance(item, Album):
            return self.album_tracks(item)
        if isinstance(item, Playlist):
            return self.playlist_tracks(item)
        if isinstance(item, Artist):
            return self.artist_page(item)[0]
        return []

    # playback (remote control of the librespot device) -------------------------
    def find_device(self, local_only: bool = False) -> str | None:
        """The device chosen in the output menu, else our own player (sdk/librespot) or the Spotify app
        on this machine, else (unless local_only) whatever is active, e.g. the phone."""
        devs = [d for d in self._user_sp.devices().get("devices", []) if not d.get("is_restricted")]
        own = DEVICE_NAME if PLAYER in ("sdk", "librespot") else socket.gethostname()
        pick = None
        for want in (CONFIG.get("spotify_device", ""), own):
            if pick := next((d for d in devs if want and d["name"].lower() == want.lower()), None):
                break
        if not pick and not local_only:
            pick = next((d for d in devs if d["is_active"]), None) or (devs[0] if devs else None)
        self.device_id = pick["id"] if pick else None
        self.device_name = pick["name"] if pick else ""
        return self.device_id

    @staticmethod
    def is_local(device: dict) -> bool:
        own = DEVICE_NAME if PLAYER in ("sdk", "librespot") else socket.gethostname()
        return device["name"].lower() == own.lower()

    def devices(self) -> list[dict]:
        return [d for d in self._user_sp.devices().get("devices", []) if not d.get("is_restricted")]

    def transfer(self, device: dict, play: bool) -> None:
        self._user_sp.transfer_playback(device["id"], force_play=play)
        self.device_id, self.device_name = device["id"], device["name"]

    def playback(self) -> dict | None:
        return self._user_sp.current_playback(market=self.market, additional_types="track")

    def set_volume(self, percent: int) -> None:
        self._user_sp.volume(max(0, min(100, int(percent))), device_id=self.device_id)

    def play(self, *, context_uri: str | None = None, uris: list[str] | None = None,
             offset: dict | None = None) -> None:
        dev = self.device_id or self.find_device()
        if not dev:
            raise RuntimeError("Kein Spotify-Gerät gefunden – Spotify-App öffnen und einmal anmelden")
        self._user_sp.start_playback(device_id=dev, context_uri=context_uri, uris=uris, offset=offset)

    def pause(self) -> None:
        self._user_sp.pause_playback(device_id=self.device_id)

    def resume(self) -> None:
        self._user_sp.start_playback(device_id=self.device_id)

    def next(self) -> None:
        self._user_sp.next_track(device_id=self.device_id)

    def previous(self) -> None:
        self._user_sp.previous_track(device_id=self.device_id)

    def seek(self, ms: int) -> None:
        self._user_sp.seek_track(max(0, int(ms)), device_id=self.device_id)


def _wait_for_code(redirect_uri: str, timeout: int = 300) -> str:
    """Minimal one-shot HTTP server that catches Spotify's OAuth redirect."""
    u = urllib.parse.urlparse(redirect_uri)
    result: dict[str, str] = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            if "code" in q or "error" in q:
                result.update({k: v[0] for k, v in q.items()})
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            ok = "code" in result
            self.wfile.write((
                "<body style='background:#1a1b26;color:#c0caf5;font:20px monospace;display:grid;"
                "place-items:center;height:90vh'><div>"
                + ("✓ iPodify ist mit Spotify verbunden – du kannst dieses Fenster schließen." if ok
                   else f"✗ Anmeldung abgebrochen: {result.get('error', '?')}")
                + "</div></body>").encode())

        def log_message(self, *args):
            pass

    srv = http.server.HTTPServer((u.hostname or "127.0.0.1", u.port or 80), Handler)
    srv.timeout = 2
    waited = 0
    while not result and waited < timeout:
        srv.handle_request()
        waited += 2
    srv.server_close()
    if "code" not in result:
        raise RuntimeError(result.get("error", "Anmeldung abgebrochen oder Zeit abgelaufen"))
    return result["code"]


def open_url(url: str) -> None:
    subprocess.Popen(["xdg-open", url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
