"""Podcasts: search (Apple Podcasts directory), RSS feeds, subscriptions and episode downloads."""

from __future__ import annotations

import email.utils
import hashlib
import json
import re
import subprocess
import threading
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .core import CACHE, CONFIG_DIR, norm

UA = {"User-Agent": "iPodify/1.0 (podcast client)"}
ITUNES = "{http://www.itunes.com/dtds/podcast-1.0.dtd}"
SUBS_FILE = CONFIG_DIR / "podcasts.json"
POD_DIR = CACHE / "podcasts"


def _get(url: str, timeout: int = 20) -> bytes:
    with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=timeout) as r:
        return r.read()


@dataclass
class Podcast:
    feed: str
    name: str
    author: str = ""
    artwork: str = ""
    genre: str = ""
    count: int = 0

    @property
    def id(self) -> str:
        return self.feed

    @property
    def title(self) -> str:  # for list filtering
        return self.name


@dataclass
class Episode:
    guid: str
    title: str
    podcast: str
    feed: str
    url: str
    pub: int = 0  # unix time
    duration: int = 0  # seconds
    description: str = ""
    artwork: str = ""
    author: str = ""

    @property
    def id(self) -> str:
        return hashlib.sha1((self.guid or self.url).encode()).hexdigest()[:16]

    @property
    def display(self) -> str:
        return f"{self.podcast} - {self.title}"

    @property
    def artist(self) -> str:
        return self.podcast

    @property
    def album(self) -> str:
        return self.podcast

    @property
    def ext(self) -> str:
        path = urllib.parse.urlparse(self.url).path.lower()
        return "m4a" if path.endswith((".m4a", ".mp4", ".aac")) else "mp3"

    @property
    def cached_file(self) -> Path | None:
        for ext in ("mp3", "m4a"):
            p = POD_DIR / f"{self.id}.{ext}"
            if p.exists() and p.stat().st_size > 0:
                return p
        return None


def search(term: str, limit: int = 30) -> list[Podcast]:
    q = urllib.parse.urlencode({"term": term, "media": "podcast", "entity": "podcast", "limit": limit,
                                "country": "DE"})
    data = json.loads(_get(f"https://itunes.apple.com/search?{q}"))
    return [Podcast(r["feedUrl"], r.get("collectionName", ""), r.get("artistName", ""),
                    r.get("artworkUrl600") or r.get("artworkUrl100", ""), r.get("primaryGenreName", ""),
                    r.get("trackCount", 0))
            for r in data.get("results", []) if r.get("feedUrl")]


def _duration(s: str | None) -> int:
    if not s:
        return 0
    s = s.strip()
    if s.isdigit():
        return int(s)
    parts = [int(p) for p in re.findall(r"\d+", s)][-3:]
    secs = 0
    for p in parts:
        secs = secs * 60 + p
    return secs


def _text(el, tag: str) -> str:
    x = el.find(tag)
    return (x.text or "").strip() if x is not None and x.text else ""


def _strip_html(s: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", s)).strip()


def fetch(feed: str) -> tuple[Podcast, list[Episode]]:
    root = ET.fromstring(_get(feed, timeout=30))
    ch = root.find("channel")
    if ch is None:
        raise RuntimeError("kein gültiger RSS-Feed")
    img = ch.find(f"{ITUNES}image")
    artwork = img.get("href", "") if img is not None else _text(ch, "image/url")
    pod = Podcast(feed, _text(ch, "title"), _text(ch, f"{ITUNES}author"), artwork, _text(ch, "category"))
    episodes = []
    for it in ch.findall("item"):
        enc = it.find("enclosure")
        if enc is None or not enc.get("url"):
            continue
        if (t := enc.get("type", "")) and not t.startswith("audio"):
            continue
        try:
            pub = int(email.utils.parsedate_to_datetime(_text(it, "pubDate")).timestamp())
        except (TypeError, ValueError):
            pub = 0
        ep_img = it.find(f"{ITUNES}image")
        episodes.append(Episode(
            guid=_text(it, "guid") or enc.get("url"), title=_text(it, "title"), podcast=pod.name, feed=feed,
            url=enc.get("url"), pub=pub, duration=_duration(_text(it, f"{ITUNES}duration")),
            description=_strip_html(_text(it, f"{ITUNES}summary") or _text(it, "description"))[:600],
            artwork=(ep_img.get("href", "") if ep_img is not None else "") or artwork,
            author=pod.author))
    episodes.sort(key=lambda e: e.pub, reverse=True)
    pod.count = len(episodes)
    return pod, episodes


def download(ep: Episode) -> Path:
    """Fetch the episode; formats the iPod can't play (opus, ogg …) are converted to mp3."""
    POD_DIR.mkdir(parents=True, exist_ok=True)
    tmp = POD_DIR / f"{ep.id}.part"
    with urllib.request.urlopen(urllib.request.Request(ep.url, headers=UA), timeout=60) as r, tmp.open("wb") as f:
        ctype = r.headers.get("Content-Type", "")
        while chunk := r.read(1 << 16):
            f.write(chunk)
    ext = "m4a" if ("mp4" in ctype or "m4a" in ctype or "aac" in ctype or ep.ext == "m4a") else "mp3"
    if "mpeg" not in ctype and ext == "mp3" and not ep.url.lower().split("?")[0].endswith(".mp3"):
        out = POD_DIR / f"{ep.id}.mp3"  # unknown/other codec → mp3 via ffmpeg
        subprocess.run(["ffmpeg", "-nostdin", "-loglevel", "error", "-y", "-i", str(tmp), "-vn",
                        "-codec:a", "libmp3lame", "-q:a", "4", str(out)], check=True)
        tmp.unlink(missing_ok=True)
        return out
    out = POD_DIR / f"{ep.id}.{ext}"
    tmp.rename(out)
    return out


def artwork_file(pod_feed: str, url: str) -> str:
    """Cached JPEG of the show artwork for the iPod (one per podcast)."""
    if not url:
        return ""
    p = POD_DIR / f"{hashlib.sha1(pod_feed.encode()).hexdigest()[:12]}.jpg"
    if not p.exists():
        try:
            POD_DIR.mkdir(parents=True, exist_ok=True)
            p.write_bytes(_get(url))
        except OSError:
            return ""
    return str(p)


def on_ipod(ep: Episode, state) -> bool:
    return ep.guid in state.pod_urls or ep.url in state.pod_urls or (norm(ep.podcast), norm(ep.title)) in state.pod_keys


# ── subscriptions ────────────────────────────────────────────────────────────


@dataclass
class Sub:
    feed: str
    name: str
    author: str = ""
    artwork: str = ""
    keep: int = 3  # newest N episodes go to the iPod
    added: dict[str, str] = field(default_factory=dict)  # episode guid → iPod dbid (only what we put there)


class Subscriptions:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.subs: dict[str, Sub] = {}
        try:
            for d in json.loads(SUBS_FILE.read_text()):
                self.subs[d["feed"]] = Sub(**d)
        except (OSError, ValueError, TypeError, KeyError):
            pass

    def save(self) -> None:
        with self.lock:
            SUBS_FILE.parent.mkdir(parents=True, exist_ok=True)
            SUBS_FILE.write_text(json.dumps([asdict(s) for s in self.subs.values()], indent=1, ensure_ascii=False))

    def subscribe(self, pod: Podcast) -> Sub:
        sub = self.subs.get(pod.feed) or Sub(pod.feed, pod.name, pod.author, pod.artwork)
        self.subs[pod.feed] = sub
        self.save()
        return sub

    def unsubscribe(self, feed: str) -> None:
        self.subs.pop(feed, None)
        self.save()

    def podcasts(self) -> list[Podcast]:
        return [Podcast(s.feed, s.name, s.author, s.artwork) for s in self.subs.values()]
