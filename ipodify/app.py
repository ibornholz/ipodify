"""iPodify — a retro music box: browse & play Spotify, push songs/albums/playlists to an iPod, manage the iPod."""

from __future__ import annotations

import shutil
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field

from rich.text import Text
from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.theme import Theme
from textual.widgets import (Button, DataTable, Digits, Footer, Input, OptionList, Static, TabbedContent,
                             TabPane)
from textual.widgets.option_list import Option

from . import core, podcasts
from .core import save_config
from .podcasts import Episode, Podcast, Subscriptions
from .audio import Engine
from .core import Album, Artist, IPod, IPodTrack, Job, Playlist, Track, Transfers, fmt_time
from .screens import ConfirmScreen, DeviceScreen, LoginScreen, PlaylistScreen, SetupScreen, TagScreen, TextScreen
from .spotify import DASHBOARD_URL, DEVICE_NAME, PLAYER, REDIRECT_URI, SpotifyAPI, open_url
from .webplayer import WebPlayer
from .widgets import Marquee, SeekBar, Spectrum, TitleBar, VolumeBar

JOB_ICON = {"wartet": "…", "lädt": "⇣", "bereit": "⇣", "wartet auf iPod": "◌", "überträgt": "⇪",
            "fertig": "✓", "fehler": "✗"}
JOB_INFO = {"wartet": "in der Warteschlange", "lädt": "spotDL sucht auf YouTube & lädt …",
            "bereit": "geladen, wartet aufs Schreiben", "wartet auf iPod": "iPod anschließen",
            "überträgt": "wird auf den iPod geschrieben …", "fertig": f"auf dem iPod · Playlist „{core.PLAYLIST}“"}

# width > 0: fixed, width < 0: share of the remaining space
COLUMNS = {
    "tracks": [("", "now", 2), ("#", "nr", 3), ("Titel", "title", -4), ("Künstler", "artist", -3),
               ("Album", "album", -3), ("Zeit", "time", 5), ("iPod", "ipod", 4)],
    "albums": [("Album", "name", -4), ("Künstler", "artist", -3), ("Jahr", "year", 4), ("Titel", "total", 5),
               ("iPod", "ipod", 6)],
    "playlists": [("Playlist", "name", -4), ("Von", "owner", -2), ("Titel", "total", 6)],
    "artists": [("Künstler", "name", -2), ("Genres", "genres", -3)],
    "ipod": [("", "mark", 1), ("", "now", 2), ("Titel", "title", -4), ("Künstler", "artist", -3),
             ("Album", "album", -3), ("Genre", "genre", 12), ("Zeit", "time", 5), ("★", "rating", 5)],
    "jobs": [("", "icon", 2), ("Status", "state", 15), ("Titel", "title", -3), ("Info", "info", -4)],
    "podcasts": [("Podcast", "name", -4), ("Von", "author", -3), ("Genre", "genre", 16), ("Folgen", "count", 6),
                 ("Abo", "sub", 10)],
    "episodes": [("", "now", 2), ("Datum", "pub", 10), ("Folge", "title", -5), ("Dauer", "duration", 7),
                 ("iPod", "ipod", 4)],
}
SORT_KEYS = {"time": "ms", "nr": "track_nr", "sub": "name"}
SEARCH_TABS = (("tracks", "♫ Songs"), ("albums", "◉ Alben"), ("artists", "☺ Künstler"), ("playlists", "≡ Playlists"))


@dataclass
class View:
    kind: str  # search · tracks · albums · playlists · artists · ipod · jobs
    title: str
    subtitle: str = ""
    items: list = field(default_factory=list)
    source: object = None  # the Album / Playlist / Artist / iPod playlist id this view shows
    context_uri: str = ""  # Spotify context for playback
    nav: str = ""  # sidebar entry
    filter: str = ""
    sort: tuple[str, bool] | None = None
    cursor: int = 0

    def visible(self) -> list:
        items = self.items
        if self.filter:
            f = self.filter.lower()
            items = [i for i in items if f in " ".join(str(getattr(i, a, "")) for a in
                                                       ("title", "name", "artist", "album", "owner")).lower()]
        if self.sort:
            col, rev = self.sort
            attr = SORT_KEYS.get(col, col)

            def key(i):
                v = getattr(i, attr, "")
                return (0, v.lower()) if isinstance(v, str) else (1, v or 0)

            items = sorted(items, key=key, reverse=rev)
        return items


def make_theme(c: dict[str, str], name: str) -> Theme:
    return Theme(
        name=name, dark=c.get("mode", "dark") != "light",
        primary=c["accent"], secondary=c["magenta"], accent=c["yellow"],
        warning=c["yellow"], error=c["red"], success=c["green"],
        foreground=c["foreground"], background=c["background"],
        surface=c["dark_background"], panel=c["lighter_background"],
        variables={"block-cursor-background": c["accent"], "block-cursor-foreground": c["background"],
                   "footer-key-foreground": c["accent"], "input-selection-background": c["selection"]},
    )


class IPodify(App):
    TITLE = "iPodify"
    CSS = """
    Screen { background: $background; }
    TitleBar { height: 1; background: $surface; }

    #deck { height: 9; margin: 0 1; }
    #lcd { width: 32; border: tall $panel-lighten-1; background: $surface-darken-1; padding: 0 1;
           border-title-color: $text-muted; }
    #lcd-state { color: $success; height: 1; text-style: bold; }
    #lcd-time { color: $success; width: auto; }
    #lcd-info { color: $success 70%; height: 1; }
    #lcd-track { color: $success 50%; height: 1; }
    #lcd-out { color: $accent; height: 1; text-style: bold; }
    #lcd-out:hover { background: $accent 25%; }
    #display { border: tall $panel-lighten-1; background: $surface-darken-1; padding: 0 1;
               border-title-color: $text-muted; }
    Marquee { height: 1; }
    Spectrum { height: 1fr; }
    SeekBar { height: 1; }

    #transport { height: 1; margin: 0 1 1 1; }
    #transport Button { height: 1; min-width: 5; width: 5; border: none; margin-right: 1;
                        background: $panel; color: $foreground; text-style: bold; }
    #transport Button:hover { background: $primary; color: $background; }
    #transport Button.-active { background: $accent; color: $background; }
    #transport .spacer { width: 1fr; }
    VolumeBar { width: 19; height: 1; }

    #main { height: 1fr; margin: 0 1; }
    #sidebar { width: 30; height: 1fr; border: tall $panel-lighten-1; background: $surface;
               border-title-color: $text-muted; padding: 0; }
    #sidebar:focus { border: tall $primary; }
    #sidebar > .option-list--option-highlighted { background: $primary 30%; }
    #sidebar:focus > .option-list--option-highlighted { background: $primary; color: $background; }
    #sidebar > .option-list--option-disabled { color: $primary; text-style: bold; }

    #content { height: 1fr; margin-left: 1; }
    #view-head { height: 2; padding: 0 1; }
    #view-title { text-style: bold; color: $foreground; height: 1; }
    #view-sub { color: $text-muted; height: 1; }
    #actions { height: 1; margin: 0 0 1 0; }
    #actions Button { height: 1; min-width: 6; border: none; margin-right: 1; padding: 0 1;
                      background: $panel; color: $foreground; }
    #actions Button:hover { background: $primary; color: $background; }
    #actions Button.primary { background: $primary 50%; color: $foreground; text-style: bold; }
    #actions Button.ipod { background: $secondary 45%; color: $foreground; text-style: bold; }
    #actions Button.danger { background: $error 35%; }
    #actions Button.back { background: $surface; color: $accent; text-style: bold; }
    #actions Button.ipod:hover, #actions Button.primary:hover { background: $secondary; color: $background; }

    #search, #filter { border: tall $panel-lighten-1; background: $surface; height: 3; }
    #search:focus, #filter:focus { border: tall $primary; }
    #search-view { height: 1fr; }
    #search-view TabPane { padding: 0; }
    DataTable { height: 1fr; background: $surface; scrollbar-size-vertical: 1; }
    DataTable > .datatable--header { background: $panel; color: $primary; text-style: bold; }
    DataTable > .datatable--cursor { background: $primary 35%; color: $foreground; text-style: bold; }
    DataTable:focus > .datatable--cursor { background: $primary; color: $background; }
    Footer { background: $surface; }
    """

    BINDINGS = [
        Binding("slash", "focus_input", "Suchen/Filtern"),
        Binding("s", "nav('search')", "Spotify-Suche", show=False),
        Binding("escape", "back", "Zurück"),
        Binding("o", "choose_output", "Ausgabe"),
        Binding("backspace", "back", "Zurück", show=False),
        Binding("i", "to_ipod", "→ iPod"),
        Binding("I", "view_to_ipod", "Alles → iPod"),
        Binding("space", "pause", "Play/Pause", show=False),
        Binding("z", "prev", "⏮", show=False),
        Binding("x", "play", "▶", show=False),
        Binding("c", "pause", "⏸", show=False),
        Binding("v", "stop", "⏹", show=False),
        Binding("b", "next", "⏭", show=False),
        Binding("comma", "seek(-10)", "−10s", show=False),
        Binding("full_stop", "seek(10)", "+10s", show=False),
        Binding("plus", "volume(5)", "Lauter", show=False),
        Binding("minus", "volume(-5)", "Leiser", show=False),
        Binding("m", "mark", "Markieren"),
        Binding("ctrl+a", "mark_all", "Alle markieren", show=False),
        Binding("e", "edit", "Bearbeiten"),
        Binding("p", "add_to_playlist", "→ Playlist"),
        Binding("r", "remove_from_playlist", "Aus Playlist"),
        Binding("d", "delete", "Löschen"),
        Binding("n", "new_playlist", "Neue Playlist"),
        Binding("R", "rename_playlist", "Umbenennen", show=False),
        Binding("D", "delete_playlist", "Playlist löschen", show=False),
        Binding("ctrl+r", "retry", "Wiederholen", show=False),
        Binding("L", "login", "Login", show=False),
        Binding("ctrl+e", "eject", "Auswerfen"),
        Binding("q", "quit", "Ende"),
    ]

    def __init__(self) -> None:
        super().__init__()
        self.api = SpotifyAPI()
        self.engine = Engine(lambda n, d: self._ui(self.on_engine_event, n, d))
        self.ipod = IPod()
        self.ipod.before_remount = self.engine.suspend_local  # pause iPod playback for a remount
        self.subs = Subscriptions()
        self.transfers = Transfers(self.ipod, lambda job: self._ui(self.on_job, job), self.subs)
        self.view = View("search", "Spotify-Suche", "Songs, Alben, Künstler, Playlists – oder einen Spotify-Link",
                         nav="search")
        self.history: list[View] = []
        self.rows: list = []
        self.search_results: dict[str, list] = {k: [] for k, _ in SEARCH_TABS}
        self.marked: set[str] = set()
        self.local_queue: list[IPodTrack] = []
        self.local_idx = 0
        self.account = ""
        self.player_ready = False
        self.librespot_tries = 0
        self.now_key = ""
        self._theme_mtime = 0.0
        self._sidebar_sig = None
        self._main_thread = threading.get_ident()
        self._volume_pending = False
        self.webplayer = WebPlayer(DEVICE_NAME, self.api.token, lambda k, e: self._ui(self.on_webplayer, k, e))

    def _ui(self, fn, *args, **kwargs) -> None:
        """Run fn on the UI thread, from wherever we are (and ignore calls after shutdown)."""
        if threading.get_ident() == self._main_thread:
            fn(*args, **kwargs)
            return
        try:
            self.call_from_thread(fn, *args, **kwargs)
        except RuntimeError:
            pass

    # ── layout ───────────────────────────────────────────────────────────────
    def compose(self) -> ComposeResult:
        yield TitleBar()
        with Horizontal(id="deck"):
            with Vertical(id="lcd") as lcd:
                lcd.border_title = "◖ LCD ◗"
                yield Static("⇢ Ausgabe wählen ▾", id="lcd-out")
                yield Static("■ STOP", id="lcd-state")
                yield Digits("00:00", id="lcd-time")
                yield Static("", id="lcd-info")
                yield Static("", id="lcd-track")
            with Vertical(id="display") as d:
                d.border_title = "◖ iPodify ◗"
                yield Marquee()
                yield Spectrum(lambda n: self.engine.analyzer.bands(n))
                yield SeekBar()
        with Horizontal(id="transport"):
            yield Button("⏮", id="prev", tooltip="Zurück (z)")
            yield Button("▶", id="play", tooltip="Play (x)")
            yield Button("⏸", id="pause", tooltip="Pause (c / Leertaste)")
            yield Button("⏹", id="stop", tooltip="Stop (v)")
            yield Button("⏭", id="next", tooltip="Weiter (b)")
            yield Static(classes="spacer")
            yield VolumeBar()
        with Horizontal(id="main"):
            sidebar = OptionList(id="sidebar")
            sidebar.border_title = "◖ Bibliothek ◗"
            yield sidebar
            with Vertical(id="content"):
                with Vertical(id="view-head"):
                    yield Static("", id="view-title")
                    yield Static("", id="view-sub")
                yield Horizontal(id="actions")
                yield Input(placeholder="♫  Spotify durchsuchen – oder Track/Album/Playlist/Künstler-Link einfügen",
                            id="search")
                yield Input(placeholder="Filtern …", id="filter")
                with TabbedContent(id="search-view"):
                    for key, label in SEARCH_TABS:
                        with TabPane(label, id=f"tab-{key}"):
                            yield DataTable(id=f"s-{key}", cursor_type="row")
                yield DataTable(id="list", cursor_type="row")
        yield Footer()

    def on_mount(self) -> None:
        self.apply_theme()
        for key, _ in SEARCH_TABS:
            self._columns(self.query_one(f"#s-{key}", DataTable), key)
        self.query_one(VolumeBar).show(self.engine.volume)
        self.rebuild_sidebar()
        self.show_view(self.view)
        self.query_one("#search", Input).focus()
        self.set_interval(0.25, self.refresh_player)
        self.set_interval(2, self.check_theme)
        threading.Thread(target=self._poll_ipod, daemon=True).start()
        if self.api.logged_in:
            self.start_player()
        else:
            self.query_one(Marquee).set_text("Willkommen bei iPodify  ·  L = bei Spotify anmelden  ·  / = suchen")
            self.action_login()

    # ── theme ────────────────────────────────────────────────────────────────
    def apply_theme(self) -> None:
        self._theme_mtime = core.theme_mtime()
        name = f"omarchy-{int(self._theme_mtime)}"
        self.register_theme(make_theme(core.load_colors(), name))
        self.theme = name

    def check_theme(self) -> None:
        if core.theme_mtime() != self._theme_mtime:
            self.apply_theme()
            self.update_titlebar()
            self.query_one(VolumeBar).show(self.engine.volume)

    def update_titlebar(self) -> None:
        self.query_one(TitleBar).update_bar(self.ipod.state, self.account, self.player_ready)

    def on_resize(self) -> None:
        self.update_titlebar()
        self.call_after_refresh(self.render_search if self.view.kind == "search" else self.render_list)

    # ── sidebar / navigation ─────────────────────────────────────────────────
    def rebuild_sidebar(self) -> None:
        st = self.ipod.state
        pls = [p for p in st.playlists if p.kind != "master"]
        jobs = self.transfers.active
        sig = (self.api.logged_in, self.account, len(st.tracks), len(self.subs.subs), tuple((p.id, p.name, len(p.dbids)) for p in pls),
               jobs, bool(st.mount))
        if sig == self._sidebar_sig:
            return
        self._sidebar_sig = sig
        sb = self.query_one("#sidebar", OptionList)
        hl = sb.highlighted_option.id if sb.highlighted_option else None
        opts: list = [Option("SPOTIFY", disabled=True), Option(" ⌕  Suche", id="search")]
        if self.api.logged_in:
            opts += [Option(" ♥  Lieblingssongs", id="liked"), Option(" ◉  Gespeicherte Alben", id="albums"),
                     Option(" ≡  Meine Playlists", id="playlists")]
        opts += [None, Option("PODCASTS", disabled=True), Option(" ⌕  Podcasts suchen", id="podsearch"),
                 Option(f" ◎  Meine Podcasts  ({len(self.subs.subs)})", id="pods"),
                 Option(" ⟳  Mit iPod synchronisieren", id="podsync")]
        opts += [None, Option("iPOD", disabled=True)]
        if st.mount:
            opts.append(Option(f" ▤  Alle Titel  ({len(st.tracks)})", id="ipod:all"))
            for p in pls:
                icon = "◎" if p.kind == "podcast" else "✦" if p.kind == "smart" else "≡"
                opts.append(Option(f" {icon}  {p.name}  ({len(p.dbids)})", id=f"ipod:{p.id}"))
        else:
            opts.append(Option(" ○  nicht angeschlossen", disabled=True))
        opts.append(Option(f" ⇄  Übertragungen{f'  ({jobs})' if jobs else ''}", id="jobs"))
        opts += [None, Option("KONTO", disabled=True),
                 Option(" ⏻  Abmelden" if self.api.logged_in
                        else " ⏻  Bei Spotify anmelden", id="account")]
        sb.clear_options()
        sb.add_options(opts)
        ids = {o.id for o in opts if o is not None and o.id}
        target = hl if hl in ids else self.view.nav if self.view.nav in ids else None
        if target:
            sb.highlighted = sb.get_option_index(target)

    @on(OptionList.OptionSelected, "#sidebar")
    def sidebar_selected(self, e: OptionList.OptionSelected) -> None:
        self.action_nav(e.option.id)

    def action_nav(self, nav: str) -> None:
        self.history.clear()
        if nav == "search":
            self.show_view(View("search", "Spotify-Suche", "Songs, Alben, Künstler, Playlists – oder einen Link",
                                nav="search"))
            self.query_one("#search", Input).focus()
        elif nav == "liked":
            self.load_view(View("tracks", "♥ Lieblingssongs", nav="liked"), self.api.saved_tracks)
        elif nav == "albums":
            self.load_view(View("albums", "◉ Gespeicherte Alben", nav="albums"), self.api.saved_albums)
        elif nav == "playlists":
            self.load_view(View("playlists", "≡ Meine Playlists", nav="playlists"), self.api.my_playlists)
        elif nav == "podsearch":
            self.show_view(View("podcasts", "⌕ Podcasts suchen", "Apple-Podcasts-Verzeichnis · Name, Thema oder Sender",
                                nav="podsearch", source="search"))
            self.query_one("#search", Input).focus()
        elif nav == "pods":
            self.show_view(self.pods_view())
            self.focus_table()
        elif nav == "podsync":
            self.sync_podcasts()
        elif nav.startswith("ipod:"):
            self.show_view(self.ipod_view(nav.split(":", 1)[1]))
            self.focus_table()
        elif nav == "jobs":
            self.show_view(View("jobs", "⇄ Übertragungen", "Spotify → spotDL (YouTube) → iPod",
                                items=list(self.transfers.jobs.values()), nav="jobs"))
        elif nav == "account":
            self.action_logout() if self.api.logged_in else self.action_login()

    def pods_view(self) -> View:
        return View("podcasts", "◎ Meine Podcasts",
                    f"{len(self.subs.subs)} Abos · ⟳ synchronisiert die neuesten Folgen auf den iPod",
                    items=self.subs.podcasts(), nav="pods", source="subs")

    def ipod_view(self, plid: str) -> View:
        st = self.ipod.state
        if plid == "all":
            items = sorted(st.tracks.values(), key=lambda t: (t.artist.lower(), t.album.lower(), t.track_nr))
            return View("ipod", "▤ iPod · Alle Titel", f"{len(items)} Titel", items=items, source="all",
                        nav="ipod:all")
        pl = st.playlist(plid)
        items = [st.tracks[d] for d in (pl.dbids if pl else []) if d in st.tracks]
        mins = sum(t.ms for t in items) / 60000
        return View("ipod", f"≡ iPod · {pl.name if pl else '?'}", f"Playlist · {len(items)} Titel · {mins:.0f} min",
                    items=items, source=plid, nav=f"ipod:{plid}")

    def open_item(self, item) -> None:
        self.history.append(self.view)
        if isinstance(item, Podcast):
            v = View("episodes", f"◎ {item.name}", f"Podcast · {item.author}", source=item, nav=self.view.nav)

            def load():
                pod, eps = podcasts.fetch(item.feed)
                item.artwork, item.count = item.artwork or pod.artwork, pod.count
                return eps

            self.load_view(v, load, keep_history=True)
            return
        if isinstance(item, Album):
            v = View("tracks", f"◉ {item.name}", f"Album · {item.artist} · {item.year or ''} · {item.total} Titel",
                     source=item, context_uri=item.uri, nav=self.view.nav)
            self.load_view(v, lambda: self.api.album_tracks(item), keep_history=True)
        elif isinstance(item, Playlist):
            v = View("tracks", f"≡ {item.name}", f"Playlist · von {item.owner} · {item.total} Titel",
                     source=item, context_uri=item.uri, nav=self.view.nav)
            self.load_view(v, lambda: self.api.playlist_tracks(item), keep_history=True)
        elif isinstance(item, Artist):
            v = View("albums", f"☺ {item.name}", "Künstler · " + (", ".join(item.genres[:3]) or "Alben & Singles"),
                     source=item, nav=self.view.nav)
            self.load_view(v, lambda: self.api.artist_page(item)[1], keep_history=True)

    def open_artist_top(self) -> None:
        artist = self.view.source
        if not isinstance(artist, Artist):
            return
        self.history.append(self.view)
        v = View("tracks", f"☺ {artist.name} · Top-Songs", "Die beliebtesten Songs", source=artist, nav=self.view.nav)
        self.load_view(v, lambda: self.api.artist_page(artist)[0] or self._no_top(), keep_history=True)

    def _no_top(self) -> list:
        self._ui(self.notify, "Top-Songs gibt Spotify an eigene Developer-Apps nicht heraus.", severity="warning")
        return []

    @work(thread=True, exclusive=True, group="load")
    def load_view(self, view: View, loader, keep_history: bool = False) -> None:
        if not keep_history:
            self._ui(self.history.clear)
        self._ui(self.show_view, View(view.kind, view.title, "lädt …", nav=view.nav))
        try:
            view.items = loader()
        except Exception as e:  # noqa: BLE001
            self._ui(self.notify, str(e)[:300], title="Spotify", severity="error")
            view.subtitle = "konnte nicht geladen werden"
        if isinstance(view.source, (Album, Playlist)) and view.items:
            mins = sum(t.duration_ms for t in view.items) / 60000
            view.subtitle = view.subtitle.rsplit("·", 1)[0] + f"· {len(view.items)} Titel · {mins:.0f} min"
        elif view.kind == "episodes":
            view.subtitle = f"{view.subtitle} · {len(view.items)} Folgen" + self._sub_hint(view.source)
        elif not view.subtitle:
            view.subtitle = f"{len(view.items)} Einträge"
        self._ui(self.show_view, view)
        self._ui(self.focus_table)

    def _sub_hint(self, pod) -> str:
        sub = self.subs.subs.get(getattr(pod, "feed", ""))
        return f" · ★ abonniert, neueste {sub.keep} auf den iPod" if sub else ""

    def action_back(self) -> None:
        if isinstance(self.focused, Input):
            self.focus_table()
        elif self.history:
            self.show_view(self.history.pop())
            self.focus_table()

    # ── rendering views ──────────────────────────────────────────────────────
    def show_view(self, view: View) -> None:
        if view is not self.view and self.view.kind != "search":
            t = self.query_one("#list", DataTable)
            self.view.cursor = t.cursor_row if t.row_count else 0
        self.view = view
        if view.kind != "ipod":
            self.marked.clear()
        self.query_one("#view-title", Static).update(view.title)
        self.query_one("#view-sub", Static).update(view.subtitle)
        is_search = view.kind == "search"
        pod_search = view.nav == "podsearch" and view.source == "search"
        search_in = self.query_one("#search", Input)
        search_in.display = is_search or pod_search
        search_in.placeholder = ("◎  Podcast suchen – Name, Thema oder Sender" if pod_search else
                                 "♫  Spotify durchsuchen – oder Track/Album/Playlist/Künstler-Link einfügen")
        self.query_one("#search-view").display = is_search
        self.query_one("#filter").display = view.kind in ("tracks", "ipod", "albums", "playlists", "episodes") \
            or (view.kind == "podcasts" and not pod_search)
        self.query_one("#list").display = not is_search
        flt = self.query_one("#filter", Input)
        if flt.value != view.filter:
            with flt.prevent(Input.Changed):
                flt.value = view.filter
        self.render_actions()
        if is_search:
            self.render_search()
        else:
            self.render_list(keep_cursor=False)
        self.refresh_bindings()

    def _columns(self, table: DataTable, kind: str) -> None:
        table.clear(columns=True)
        cols = COLUMNS[kind]
        avail = (self.query_one("#content").size.width or 110) - 3  # scrollbar + slack
        fixed = sum(w for _, _, w in cols if w > 0) + 2 * len(cols)  # cell padding
        weights = sum(-w for _, _, w in cols if w < 0)
        rest = max(avail - fixed, 0)
        for label, key, w in cols:
            table.add_column(label, key=key, width=w if w > 0 else max(8, rest * -w // weights))

    def render_list(self, keep_cursor: bool = True) -> None:
        t = self.query_one("#list", DataTable)
        cur = t.cursor_row if keep_cursor and t.row_count else self.view.cursor
        self._columns(t, self.view.kind)
        self.rows = self.view.visible()
        for i, item in enumerate(self.rows):
            t.add_row(*self.cells(item, i), key=str(i))
        if self.rows:
            t.move_cursor(row=min(cur, len(self.rows) - 1))

    def render_search(self) -> None:
        for key, items in self.search_results.items():
            t = self.query_one(f"#s-{key}", DataTable)
            self._columns(t, key)
            for i, item in enumerate(items):
                t.add_row(*self.cells(item, i), key=str(i))
        tabs = self.query_one("#search-view", TabbedContent)
        for key, label in SEARCH_TABS:
            n = len(self.search_results[key])
            tabs.get_tab(f"tab-{key}").label = f"{label} ({n})" if n else label

    def cells(self, item, i: int) -> tuple:
        th = self.current_theme
        st = self.ipod.state
        if isinstance(item, Track):
            job = self.transfers.jobs.get(item.id)
            if item.key in st.keys or (job and job.state == "fertig"):
                mark = Text(" ✓", style=f"bold {th.success}")
            elif job:
                mark = Text(" " + JOB_ICON[job.state], style=th.error if job.state == "fehler" else th.warning)
            else:
                mark = ""
            now = Text("▶", style=f"bold {th.success}") if self.now_key == item.id else ""
            nr = item.track_nr if isinstance(self.view.source, Album) else i + 1
            return now, str(nr), item.title, item.artist, item.album, fmt_time(item.duration_ms / 1000), mark
        if isinstance(item, Album):
            n = st.album_count(item.name, item.artists[0] if item.artists else "")
            full = item.total and n >= item.total
            mark = Text(f"✓ {n}" if full else f"◐ {n}", style=th.success if full else th.warning) if n else ""
            return item.name, item.artist, str(item.year or ""), str(item.total), mark
        if isinstance(item, Playlist):
            return item.name, item.owner, str(item.total)
        if isinstance(item, Artist):
            return item.name, ", ".join(item.genres[:4])
        if isinstance(item, IPodTrack):
            mark = Text("●", style=f"bold {th.accent}") if item.dbid in self.marked else ""
            now = Text("▶", style=f"bold {th.success}") if self.now_key == item.dbid else ""
            return (mark, now, item.title, item.artist, item.album, item.genre, fmt_time(item.ms / 1000),
                    "★" * item.rating)
        if isinstance(item, Podcast):
            sub = self.subs.subs.get(item.feed)
            return (item.name, item.author, item.genre, str(item.count or ""),
                    Text(f"★ {sub.keep}", style=f"bold {th.accent}") if sub else "")
        if isinstance(item, Episode):
            job = self.transfers.jobs.get(item.id)
            if podcasts.on_ipod(item, st) or (job and job.state == "fertig"):
                mark = Text(" ✓", style=f"bold {th.success}")
            elif job:
                mark = Text(" " + JOB_ICON[job.state], style=th.error if job.state == "fehler" else th.warning)
            else:
                mark = ""
            now = Text("▶", style=f"bold {th.success}") if self.now_key == item.id else ""
            date = time.strftime("%d.%m.%Y", time.localtime(item.pub)) if item.pub else ""
            return now, date, item.title, fmt_time(item.duration) if item.duration else "", mark
        if isinstance(item, Job):
            color = {"fertig": th.success, "fehler": th.error}.get(item.state, th.warning)
            return (Text(JOB_ICON[item.state], style=f"bold {color}"), Text(item.state, style=color),
                    item.track.display, item.error or JOB_INFO.get(item.state, ""))
        return ()

    def render_actions(self) -> None:
        bar = self.query_one("#actions", Horizontal)
        bar.remove_children()
        v, buttons = self.view, []
        if self.history:
            back = self.history[-1].title.split(" ", 1)[-1][:24]
            buttons.append(("back", f"← {back}", "back"))
        if v.kind == "tracks":
            buttons.append(("play_all", "▶ Abspielen", "primary"))
            what = "Album" if isinstance(v.source, Album) else "Playlist" if isinstance(v.source, Playlist) else "Alle"
            buttons.append(("view_to_ipod", f"⇪ {what} auf den iPod", "ipod"))
            buttons.append(("to_ipod", "⇪ Song auf den iPod", ""))
        elif v.kind == "albums":
            if isinstance(v.source, Artist):
                buttons.append(("artist_top", "♫ Top-Songs", "primary"))
            buttons.append(("to_ipod", "⇪ Album auf den iPod", "ipod"))
        elif v.kind == "playlists":
            buttons.append(("to_ipod", "⇪ Playlist auf den iPod", "ipod"))
        elif v.kind == "search":
            buttons.append(("to_ipod", "⇪ Auswahl auf den iPod", "ipod"))
        elif v.kind == "ipod":
            buttons += [("play_all", "▶ Abspielen", "primary"), ("edit", "✎ Bearbeiten", ""),
                        ("add_to_playlist", "≡ Zu Playlist", "")]
            if self._current_playlist():
                buttons.append(("remove_from_playlist", "− Aus Playlist", ""))
            buttons += [("delete", "✗ Vom iPod löschen", "danger"), ("new_playlist", "+ Neue Playlist", "")]
            if self._current_playlist():
                buttons += [("rename_playlist", "✎ Umbenennen", ""), ("delete_playlist", "✗ Playlist löschen", "danger")]
        elif v.kind == "jobs":
            buttons += [("retry", "↻ Fehlgeschlagene wiederholen", ""), ("clear_jobs", "Erledigte ausblenden", "")]
        elif v.kind == "podcasts":
            if v.source == "subs":
                buttons.append(("sync_podcasts", "⟳ Alle mit dem iPod synchronisieren", "ipod"))
            buttons.append(("toggle_sub", "★ Abonnieren / beenden", ""))
        elif v.kind == "episodes":
            sub = self.subs.subs.get(v.source.feed) if isinstance(v.source, Podcast) else None
            buttons.append(("play_all", "▶ Abspielen", "primary"))
            if sub:
                buttons += [("sync_this", "⟳ Synchronisieren", "ipod"),
                            ("cycle_keep", f"Neueste {sub.keep} auf den iPod ▾", ""),
                            ("toggle_sub", "☆ Abo beenden", "")]
            else:
                buttons.append(("toggle_sub", "★ Abonnieren", "ipod"))
            buttons.append(("to_ipod", "⇪ Folge auf den iPod", ""))
        bar.mount_all([Button(label, name=name, classes=cls) for name, label, cls in buttons])

    @on(Button.Pressed, "#actions Button")
    def action_button(self, e: Button.Pressed) -> None:
        name = e.button.name
        {"artist_top": self.open_artist_top, "clear_jobs": self.clear_jobs, "sync_podcasts": self.sync_podcasts,
         "sync_this": lambda: self.sync_podcasts(self.view.source.feed), "toggle_sub": self.toggle_sub,
         "cycle_keep": self.cycle_keep}.get(
            name, getattr(self, f"action_{name}", lambda: None))()

    def current_table(self) -> DataTable:
        if self.view.kind == "search":
            tab = self.query_one("#search-view", TabbedContent).active or "tab-tracks"
            return self.query_one(f"#s-{tab.removeprefix('tab-')}", DataTable)
        return self.query_one("#list", DataTable)

    def focus_table(self) -> None:
        self.current_table().focus()

    def current_items(self) -> list:
        if self.view.kind == "search":
            tab = self.query_one("#search-view", TabbedContent).active or "tab-tracks"
            return self.search_results[tab.removeprefix("tab-")]
        return self.rows

    def selected(self):
        t, items = self.current_table(), self.current_items()
        if not items or t.cursor_row is None or t.cursor_row >= len(items):
            return None
        return items[t.cursor_row]

    def refresh_rows(self, match=None) -> None:
        """Re-render the cells of visible rows (all, or those where match(item) is true)."""
        tables = [(self.query_one("#list", DataTable), self.rows)]
        if self.view.kind == "search":
            tables = [(self.query_one(f"#s-{k}", DataTable), v) for k, v in self.search_results.items()]
        for t, items in tables:
            keys = [c.key for c in t.ordered_columns]
            for i, item in enumerate(items):
                if (match and not match(item)) or str(i) not in t.rows:
                    continue
                for key, val in zip(keys, self.cells(item, i)):
                    t.update_cell(str(i), key, val)

    # ── search & filter ──────────────────────────────────────────────────────
    def action_focus_input(self) -> None:
        if self.view.kind == "search":
            self.query_one("#search", Input).focus()
        elif self.query_one("#filter").display:
            self.query_one("#filter", Input).focus()

    @on(Input.Submitted, "#search")
    def on_search(self, e: Input.Submitted) -> None:
        if self.view.nav == "podsearch":
            if q := e.value.strip():
                self.query_one("#view-sub", Static).update(f"Suche nach „{q}“ …")
                self.load_view(View("podcasts", "⌕ Podcasts suchen", f"„{q}“", nav="podsearch", source="search"),
                               lambda: podcasts.search(q))
            return
        if not self.api.logged_in:
            self.notify("Zum Suchen bei Spotify anmelden.", severity="warning")
            self.action_login()
        elif q := e.value.strip():
            self.query_one("#view-sub", Static).update(f"Suche nach „{q}“ …")
            self.search(q)

    @work(thread=True, exclusive=True, group="search")
    def search(self, q: str) -> None:
        try:
            res = self.api.search(q)
        except Exception as e:  # noqa: BLE001
            self._ui(self.notify, str(e)[:300], title="Suche fehlgeschlagen", severity="error")
            return

        def show() -> None:
            self.search_results = res
            self.view.subtitle = f"„{q}“ · {sum(len(v) for v in res.values())} Treffer"
            self.query_one("#view-sub", Static).update(self.view.subtitle)
            self.render_search()
            first = next((k for k, _ in SEARCH_TABS if res[k]), "tracks")
            self.query_one("#search-view", TabbedContent).active = f"tab-{first}"
            self.query_one(f"#s-{first}", DataTable).focus()

        self._ui(show)

    @on(Input.Changed, "#filter")
    def on_filter(self, e: Input.Changed) -> None:
        self.view.filter = e.value
        self.render_list(keep_cursor=False)

    @on(Input.Submitted, "#filter")
    def on_filter_submit(self) -> None:
        self.focus_table()

    @on(DataTable.HeaderSelected, "#list")
    def on_header(self, e: DataTable.HeaderSelected) -> None:
        col = str(e.column_key.value)
        if col in ("now", "mark", "icon", "ipod"):
            return
        rev = bool(self.view.sort and self.view.sort[0] == col and not self.view.sort[1])
        self.view.sort = (col, rev)
        self.render_list(keep_cursor=False)

    # ── activate rows ────────────────────────────────────────────────────────
    @on(DataTable.RowSelected)
    def on_row(self, e: DataTable.RowSelected) -> None:
        self.activate(self.current_items(), e.cursor_row)

    def activate(self, items: list, idx: int) -> None:
        if idx is None or idx >= len(items):
            return
        item = items[idx]
        if isinstance(item, Track):
            self.play_spotify(items, idx)
        elif isinstance(item, (IPodTrack, Episode)):
            self.play_local(items, idx)
        elif isinstance(item, (Album, Playlist, Artist, Podcast)):
            self.open_item(item)

    # ── playback ─────────────────────────────────────────────────────────────
    def play_spotify(self, items: list, idx: int) -> None:
        if not self.api.logged_in:
            self.notify("Zum Abspielen bei Spotify anmelden (L).", severity="warning")
            return
        track = items[idx]
        if self.view.context_uri and self.view.kind == "tracks" and not self.view.filter and not self.view.sort:
            kw = {"context_uri": self.view.context_uri, "offset": {"uri": track.uri}}
        else:
            tracks = [t for t in items if isinstance(t, Track)]
            j = tracks.index(track)
            lo = max(0, j - 20)
            kw = {"uris": [t.uri for t in tracks[lo:lo + 100]], "offset": {"position": j - lo}}
        self.engine.stop()
        self.engine.source = "spotify"
        self.query_one(Marquee).set_text(f"{track.display}  ({fmt_time(track.duration_ms / 1000)})")
        self._spotify(lambda: self.api.play(**kw), wait_device=True)

    @work(thread=True, group="spotify-ctl")
    def _spotify(self, fn, wait_device: bool = False) -> None:
        try:
            if wait_device and not self.api.device_id:
                if PLAYER == "librespot":
                    for _ in range(30):
                        if self.api.find_device():
                            break
                        time.sleep(0.5)
                elif not self.ensure_device():
                    return
            fn()
        except Exception as e:  # noqa: BLE001
            msg = str(e)
            if "PREMIUM_REQUIRED" in msg:
                msg = "Spotify verlangt Premium für die Wiedergabe."
            self._ui(self.notify, msg[:300], title="Spotify", severity="error")

    def play_local(self, items: list, idx: int) -> None:
        self.local_queue = [t for t in items if isinstance(t, (IPodTrack, Episode))]
        self.local_idx = self.local_queue.index(items[idx])
        self._play_local_current()

    def _play_local_current(self, start: float = 0.0) -> None:
        t = self.local_queue[self.local_idx]
        if self.engine.source == "spotify" and self.engine.sp_playing:
            self._spotify(self.api.pause)
        if isinstance(t, Episode):
            path, info = str(t.cached_file or t.url), {"id": t.id, "dur": t.duration, "kind": "podcast"}
        else:
            path, info = t.path, {"id": t.dbid, "dur": t.ms / 1000, "kbps": t.kbps, "kind": "ipod"}
        self.engine.play_local(path, start, info=info | {
            "title": t.title, "artist": t.artist, "album": t.album, "n": self.local_idx + 1,
            "of": len(self.local_queue)})

    def action_play_all(self) -> None:
        if items := self.current_items():
            self.activate(items, 0)

    def action_play(self) -> None:
        st = self.engine.status()
        if st["source"] and st.get("paused"):
            self.action_pause()
        elif isinstance(self.selected(), (Track, IPodTrack)):
            self.activate(self.current_items(), self.current_table().cursor_row)

    def action_pause(self) -> None:
        st = self.engine.status()
        if st["source"] == "local":
            self.engine.pause_local(st["playing"])
        elif st["source"] == "spotify":
            self.engine.sp_playing = not st["playing"]  # optimistic, events will correct
            self._spotify(self.api.pause if st["playing"] else self.api.resume)
        else:
            self.action_play()

    def action_stop(self) -> None:
        if self.engine.source == "spotify":
            self._spotify(self.api.pause)
        self.engine.stop()
        self.engine.source = None

    def action_next(self) -> None:
        if self.engine.source == "local" and self.local_queue:
            if self.local_idx + 1 < len(self.local_queue):
                self.local_idx += 1
                self._play_local_current()
            else:
                self.engine.stop()
        elif self.engine.source == "spotify":
            self._spotify(self.api.next)

    def action_prev(self) -> None:
        st = self.engine.status()
        if st["source"] == "local" and self.local_queue:
            if st["pos"] < 5 and self.local_idx > 0:
                self.local_idx -= 1
            self._play_local_current()
        elif st["source"] == "spotify":
            self._spotify(self.api.previous)

    def seek_to(self, seconds: float) -> None:
        st = self.engine.status()
        seconds = max(0.0, min(seconds, (st.get("dur") or 0) - 1))
        if st["source"] == "local":
            self._play_local_current(seconds)
        elif st["source"] == "spotify":
            self.engine.sp_pos_ms, self.engine.sp_pos_t = int(seconds * 1000), time.monotonic()
            self._spotify(lambda: self.api.seek(seconds * 1000))

    def action_seek(self, delta: int) -> None:
        self.seek_to((self.engine.status().get("pos") or 0) + delta)

    @on(SeekBar.Seek)
    def on_seekbar(self, e: SeekBar.Seek) -> None:
        if dur := self.engine.status().get("dur"):
            self.seek_to(e.frac * dur)

    def action_volume(self, delta: int) -> None:
        self._set_volume(self.engine.volume + delta)

    @on(VolumeBar.Changed)
    def on_volume(self, e: VolumeBar.Changed) -> None:
        self._set_volume(e.volume)

    def _set_volume(self, v: int) -> None:
        self.engine.set_volume(v)
        self.query_one(VolumeBar).show(self.engine.volume)
        if PLAYER != "librespot" and self.engine.source == "spotify":
            self._volume_pending = True
            self.set_timer(0.4, self._push_volume)  # coalesce key repeats into one API call

    @work(thread=True, exclusive=True, group="volume")
    def _push_volume(self) -> None:
        try:
            self.api.set_volume(self.engine.volume)
        except Exception as e:  # noqa: BLE001
            self._ui(self.notify, f"Lautstärke: {e}"[:200], severity="warning")
        time.sleep(1)
        self._volume_pending = False

    @on(Button.Pressed, "#transport Button")
    def on_transport(self, e: Button.Pressed) -> None:
        {"prev": self.action_prev, "play": self.action_play, "pause": self.action_pause,
         "stop": self.action_stop, "next": self.action_next}[e.button.id]()

    def refresh_player(self) -> None:
        st = self.engine.status()
        src, playing, paused = st["source"], st["playing"], st["paused"]
        self.query_one("#lcd-state", Static).update("■ STOP" if not src else "❚❚ PAUSE" if paused else "▶ PLAY")
        pos = st.get("pos") if src else 0
        self.query_one("#lcd-time", Digits).update(fmt_time(pos or 0))
        if PLAYER == "librespot":
            out = "⇢ iPodify (librespot)"
        elif src == "local":
            out = "⇢ dieser Rechner"
        else:
            out = f"⇢ {st.get('device') or self.api.device_name or 'Ausgabe wählen'} ▾"
        self.query_one("#lcd-out", Static).update(out[:28])
        if src == "spotify" and PLAYER != "librespot":
            info = "SPOTIFY CONNECT"
        elif src == "spotify":
            info = f"{st.get('kbps', 320)} kbps · 44 kHz · SPOTIFY"
        elif src == "local" and st.get("kind") == "podcast":
            info = "PODCAST"
        elif src == "local":
            info = f"{st.get('kbps') or '?'} kbps · 44 kHz · iPOD"
        else:
            info = "SPOTIFY BEREIT" if self.player_ready else ""
        self.query_one("#lcd-info", Static).update(info)
        self.query_one("#lcd-track", Static).update(f"TRACK {st['n']:02d}/{st['of']:02d}" if src == "local" else "")
        self.query_one(SeekBar).show(pos, st.get("dur") if src else None)
        for bid, active in (("play", src and playing), ("pause", src and paused), ("stop", not src)):
            self.query_one(f"#{bid}", Button).set_class(bool(active), "-active")
        if src and st.get("title"):
            self.query_one(Marquee).set_text(f"{st.get('artist', '')} - {st['title']}  ({fmt_time(st.get('dur'))})"
                                             + ("  ·  vom iPod" if st.get("kind") == "ipod" else ""))
        key = st.get("id", "") if src else ""
        if key != self.now_key:
            old, self.now_key = self.now_key, key
            self.refresh_rows(lambda it: getattr(it, "id", getattr(it, "dbid", None)) in (old, key))

    def on_engine_event(self, name: str, data: dict) -> None:
        if name == "local_end":
            self.action_next()
        elif name == "oauth_started":
            self.notify("Im Browser öffnet sich Spotify – bitte den Zugriff für den Player „iPodify“ bestätigen.",
                        title="♫ Spotify-Player anmelden", timeout=30)
        elif name == "session_connected":
            self.wait_for_device()
        elif name == "librespot_exit":
            self.player_ready = False
            self.update_titlebar()
            if self.api.logged_in and self.librespot_tries < 2:
                self.librespot_tries += 1
                log = "\n".join(list(self.engine.log)[-2:])
                self.notify(f"Spotify-Player wird neu gestartet\n{log[:300]}", severity="warning")
                self.start_player()

    def on_click(self, event) -> None:
        if getattr(event.widget, "id", None) == "lcd-out":
            self.action_choose_output()

    @work(thread=True, exclusive=True, group="devices")
    def action_choose_output(self) -> None:
        if PLAYER == "librespot" or not self.api.logged_in:
            return
        try:
            devs = self.api.devices()
        except Exception as e:  # noqa: BLE001
            self._ui(self.notify, f"Geräte: {e}"[:200], severity="error")
            return
        can_launch = (PLAYER == "sdk" or bool(shutil.which("spotify-launcher") or shutil.which("spotify"))) \
            and not any(self.api.is_local(d) for d in devs)
        self._ui(self.push_screen, DeviceScreen(devs, self.api.device_id, can_launch), self._output_chosen)

    def _output_chosen(self, choice) -> None:
        if choice == "launch":
            save_config(spotify_device="")
            core.CONFIG["spotify_device"] = ""
            self.api.device_id = None
            self._spotify(lambda: None, wait_device=True)
        elif choice:
            save_config(spotify_device=choice["name"])
            core.CONFIG["spotify_device"] = choice["name"]
            self.engine.stop()  # local iPod playback hands over to Spotify
            self._spotify(lambda: self.api.transfer(choice, play=self.engine.sp_playing))
            self.notify(f"Wiedergabe auf „{choice['name']}“", title="⇢ Ausgabe")

    def launch_local_player(self) -> bool:
        if PLAYER == "sdk":
            try:
                self.webplayer.start()
            except Exception as e:  # noqa: BLE001
                self._ui(self.notify, str(e), title="Spotify-Player", severity="error")
                return False
            return True
        exe = shutil.which("spotify-launcher") or shutil.which("spotify")
        if not exe:
            self._ui(self.notify, "Spotify-App fehlt: sudo pacman -S spotify-launcher", severity="error")
            return False
        self._ui(self.notify, "Starte die Spotify-App … (beim ersten Mal dort anmelden)", title="♫ Spotify",
                 timeout=15)
        subprocess.Popen([exe], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         start_new_session=True)
        return True

    def on_webplayer(self, kind: str, ev: dict) -> None:
        if kind == "ready":
            self.api.device_id, self.api.device_name = ev.get("device_id"), DEVICE_NAME
            self.player_ready = True
            self.update_titlebar()
        elif kind == "not_ready":
            self.player_ready = False
            self.update_titlebar()
        elif kind == "error":
            msg = ev.get("message") or ev.get("type")
            if ev.get("type") == "account_error":
                msg = "Spotify Premium wird für den Web-Player benötigt."
            self.notify(f"{msg}"[:300], title="Spotify-Player", severity="error")

    def ensure_device(self, launch: bool = True) -> bool:
        """Find our Spotify device; if there is none, start the local player and wait for it."""
        try:
            if self.api.find_device(local_only=True):
                self._device_ready()
                return True
        except Exception:  # noqa: BLE001
            pass
        if not launch:
            return False
        if not self.launch_local_player():
            return False
        for _ in range(45):
            time.sleep(1)
            try:
                if self.api.find_device(local_only=True):
                    self._device_ready()
                    return True
            except Exception:  # noqa: BLE001
                pass
        try:  # fall back to any other device (phone, speaker …)
            if self.api.find_device():
                self._ui(self.notify, f"Spiele auf „{self.api.device_name}“ ab.", title="♫ Spotify")
                self._device_ready()
                return True
        except Exception:  # noqa: BLE001
            pass
        self._ui(self.notify, "Die Spotify-App meldet sich nicht als Gerät – ist sie angemeldet?", severity="error")
        return False

    def _device_ready(self) -> None:
        if not self.player_ready:
            self.player_ready = True
            self._ui(self.update_titlebar)

    def _poll_spotify(self) -> None:
        """App mode: the official app sends us no events, so ask the Web API what's playing."""
        while True:
            playing = False
            if self.api.logged_in and PLAYER != "librespot":
                try:
                    pb = self.api.playback()
                except Exception:  # noqa: BLE001
                    pb = None
                e = self.engine
                item = (pb or {}).get("item")
                if item:
                    playing = bool(pb.get("is_playing"))
                    e.sp = {"uri": item["uri"], "id": item.get("id", ""), "title": item["name"],
                            "artist": ", ".join(a["name"] for a in item.get("artists", [])),
                            "album": (item.get("album") or {}).get("name", ""),
                            "dur": item.get("duration_ms", 0) / 1000, "device": pb["device"]["name"]}
                    e.sp_playing = playing
                    e.sp_pos_ms, e.sp_pos_t = pb.get("progress_ms") or 0, time.monotonic()
                    if playing and e.source != "spotify":
                        e.stop()
                        e.source = "spotify"
                    if pb["device"].get("id"):
                        self.api.device_id, self.api.device_name = pb["device"]["id"], pb["device"]["name"]
                    if (v := pb["device"].get("volume_percent")) is not None and not self._volume_pending:
                        e.volume = v
                        self._ui(self.query_one(VolumeBar).show, v)
                    self._device_ready()
                elif self.engine.source == "spotify":
                    e.sp_playing = False
            time.sleep(1.5 if playing else 4)

    @work(thread=True, exclusive=True, group="device")
    def wait_for_device(self) -> None:
        """librespot reports the session before Connect is up – ready means Spotify lists the device."""
        for _ in range(20):
            try:
                if self.api.find_device():
                    self.player_ready = True
                    self.librespot_tries = 0
                    self._ui(self.update_titlebar)
                    self._ui(self.notify, "Spotify-Player „iPodify“ ist bereit.", title="♫ Spotify")
                    return
            except Exception:  # noqa: BLE001
                pass
            if not self.engine.librespot_running:
                return
            time.sleep(1)

    # ── Spotify login ────────────────────────────────────────────────────────
    def action_login(self) -> None:
        if self.api.configured:
            self.login()
            return

        def done(creds) -> None:
            if creds:
                self.api.set_credentials(*creds)
                self.login()

        self.push_screen(SetupScreen(REDIRECT_URI, DASHBOARD_URL, self.api.credentials()[0]), done)

    @work(thread=True, exclusive=True, group="login")
    def login(self) -> None:
        def on_url(url: str) -> None:
            open_url(url)
            self._ui(self.push_screen, LoginScreen(url))

        try:
            me = self.api.login(on_url)
        except Exception as e:  # noqa: BLE001
            self._ui(self.notify, str(e)[:300], title="Anmeldung fehlgeschlagen", severity="error")
            return
        self._ui(self._after_login, me)

    def _after_login(self, me: dict) -> None:
        if isinstance(self.screen, LoginScreen):
            self.pop_screen()
        self.notify(f"Angemeldet als {me.get('display_name') or me.get('id')}", title="♫ Spotify")
        self.start_player()

    @work(thread=True, exclusive=True, group="player")
    def start_player(self) -> None:
        try:
            me = self.api.profile() or {}
            self.account = me.get("display_name") or me.get("id") or "Spotify"
            if me.get("product") and me["product"] != "premium":
                self._ui(self.notify, "Für die Wiedergabe braucht Spotify ein Premium-Konto.", severity="warning")
        except Exception as e:  # noqa: BLE001
            self._ui(self.notify, f"Spotify-Profil: {e}"[:300], severity="error")
        self._ui(self.rebuild_sidebar)
        self._ui(self.update_titlebar)
        if PLAYER == "librespot":
            try:
                self.engine.start_librespot()
            except Exception as e:  # noqa: BLE001
                self._ui(self.notify, str(e), title="Spotify-Player", severity="error")
            return
        self.engine.start_monitor()
        if not getattr(self, "_polling", False):
            self._polling = True
            threading.Thread(target=self._poll_spotify, daemon=True).start()
        self.ensure_device(launch=PLAYER == "sdk")

    def action_logout(self) -> None:
        def done(ok: bool | None) -> None:
            if ok:
                self.librespot_tries = 99
                self.engine.stop_librespot()
                (core.CACHE / "librespot" / "credentials.json").unlink(missing_ok=True)
                self.api.logout()
                self.account, self.player_ready = "", False
                self.rebuild_sidebar()
                self.update_titlebar()

        self.push_screen(ConfirmScreen("⏻ Abmelden", "Von Spotify abmelden?", "Abmelden"), done)

    # ── to the iPod ──────────────────────────────────────────────────────────
    def action_to_ipod(self) -> None:
        item = self.selected()
        if isinstance(item, Episode):
            if podcasts.on_ipod(item, self.ipod.state):
                self.notify("Die Folge ist schon auf dem iPod.", severity="warning")
            elif self.transfers.add(item):
                self.notify(item.display, title="⇪ Auf den iPod")
        elif isinstance(item, Podcast):
            self.sync_podcasts(self.toggle_sub(item, subscribe_only=True))
        elif item is not None and not isinstance(item, (IPodTrack, Job)):
            self.queue_items([item])

    def action_view_to_ipod(self) -> None:
        v = self.view
        if v.kind == "episodes" and isinstance(v.source, Podcast):
            self.toggle_sub(v.source, subscribe_only=True)
            self.sync_podcasts(v.source.feed)
        elif v.kind == "tracks":
            self.queue_items(list(v.visible()), label=v.title.lstrip("◉≡♥☺ "))
        else:
            self.action_to_ipod()

    @work(thread=True, group="queue")
    def queue_items(self, items: list, label: str = "") -> None:
        tracks: list[Track] = []
        for item in items:
            try:
                tracks += self.api.tracks_of(item)
            except Exception as e:  # noqa: BLE001
                self._ui(self.notify, str(e)[:300], severity="error")
        on_ipod = [t for t in tracks if t.key in self.ipod.state.keys]
        added = [t for t in tracks if t not in on_ipod and self.transfers.add(t)]
        if not label:
            one = items[0] if len(items) == 1 else None
            label = getattr(one, "name", None) or (one.display if isinstance(one, Track) else f"{len(tracks)} Titel")
        msg = f"{label}\n{len(added)} neu in der Warteschlange"
        if on_ipod:
            msg += f", {len(on_ipod)} schon auf dem iPod"
        self._ui(self.notify, msg, title="⇪ Auf den iPod")

    def on_job(self, job: Job) -> None:
        if self.view.kind == "jobs":
            if job not in self.view.items:
                self.view.items.append(job)
                self.render_list()
            else:
                self.refresh_rows(lambda it: it is job)
        else:
            self.refresh_rows(lambda it: isinstance(it, (Track, Episode)) and it.id == job.track.id)
        self.rebuild_sidebar()
        if job.state == "fehler":
            self.notify(f"{job.track.display}: {job.error}"[:300], severity="error", title="Übertragung")
        elif job.state == "fertig" and not self.transfers.active:
            self.notify("Alles auf dem iPod.", title="✓ Übertragungen fertig")
            self.refresh_ipod()

    # ── podcasts ─────────────────────────────────────────────────────────────
    def toggle_sub(self, pod: Podcast | None = None, subscribe_only: bool = False) -> str:
        pod = pod or (self.view.source if self.view.kind == "episodes" else self.selected())
        if not isinstance(pod, Podcast):
            return ""
        if pod.feed in self.subs.subs and not subscribe_only:
            self.subs.unsubscribe(pod.feed)
            self.notify(f"Abo „{pod.name}“ beendet. Folgen auf dem iPod bleiben.", title="◎ Podcasts")
        elif pod.feed not in self.subs.subs:
            self.subs.subscribe(pod)
            self.notify(f"„{pod.name}“ abonniert", title="◎ Podcasts")
        self._refresh_pod_view()
        return pod.feed

    def cycle_keep(self) -> None:
        if isinstance(pod := self.view.source, Podcast) and (sub := self.subs.subs.get(pod.feed)):
            steps = [1, 3, 5, 10]
            sub.keep = steps[(steps.index(sub.keep) + 1) % len(steps)] if sub.keep in steps else 3
            self.subs.save()
            for b in self.query("#actions Button"):  # relabel in place – re-mounting the bar can race
                if b.name == "cycle_keep":
                    b.label = f"Neueste {sub.keep} auf den iPod ▾"
            self.view.subtitle = self.view.subtitle.split(" · ★")[0] + self._sub_hint(pod)
            self.query_one("#view-sub", Static).update(self.view.subtitle)

    def _refresh_pod_view(self) -> None:
        self.rebuild_sidebar()
        if self.view.kind == "episodes":
            self.view.subtitle = self.view.subtitle.split(" · ★")[0] + self._sub_hint(self.view.source)
            self.query_one("#view-sub", Static).update(self.view.subtitle)
        elif self.view.source == "subs":
            self.view.items = self.subs.podcasts()
        self.render_actions()
        self.refresh_rows() if self.view.kind != "podcasts" else self.render_list()

    @work(thread=True, exclusive=True, group="podsync")
    def sync_podcasts(self, feed: str = "") -> None:
        """Newest N of each subscription onto the iPod; older ones iPodify put there come off again."""
        subs = [s for s in self.subs.subs.values() if not feed or s.feed == feed]
        if not subs:
            self._ui(self.notify, "Noch keine Podcasts abonniert – über „Podcasts suchen“ ★ abonnieren.",
                     severity="warning")
            return
        self._ui(self.notify, f"Prüfe {len(subs)} Podcast(s) …", title="⟳ Synchronisieren")
        queued, remove, failed = 0, [], []
        st = self.ipod.state
        for sub in subs:
            try:
                _pod, eps = podcasts.fetch(sub.feed)
            except Exception as e:  # noqa: BLE001
                failed.append(f"{sub.name}: {e}")
                continue
            newest = eps[:sub.keep]
            queued += sum(1 for e in newest if not podcasts.on_ipod(e, st) and self.transfers.add(e))
            keep = {e.guid for e in newest}
            for guid, dbid in list(sub.added.items()):
                if dbid not in st.tracks:
                    del sub.added[guid]  # deleted on the iPod meanwhile
                elif guid not in keep:
                    remove.append(["rm", dbid])
                    del sub.added[guid]
        self.subs.save()
        if remove:
            try:
                self.ipod.apply(remove)
                self.ipod.refresh()
                self._ui(self.on_ipod)
            except Exception as e:  # noqa: BLE001
                failed.append(f"Alte Folgen entfernen: {e}")
        msg = f"{queued} neue Folge(n) in der Warteschlange · {len(remove)} alte entfernt"
        self._ui(self.notify, msg + ("\n" + "\n".join(failed) if failed else ""), title="⟳ Podcasts",
                 severity="warning" if failed else "information")

    def action_retry(self) -> None:
        self.notify(f"{self.transfers.retry_failed()} Titel erneut in der Warteschlange")

    def clear_jobs(self) -> None:
        self.transfers.clear_done()
        self.view.items = list(self.transfers.jobs.values())
        self.render_list()
        self.rebuild_sidebar()

    # ── iPod polling & management ────────────────────────────────────────────
    def _poll_ipod(self) -> None:
        last, last_full = None, 0.0
        while True:
            _part, mount, _rw = core.find_ipod()
            sig = (mount, core.HELPER.exists())
            try:
                released = self.ipod.release_if_idle()
            except Exception:  # noqa: BLE001
                released = False
            if sig != last or released or time.time() - last_full > 120:
                self.ipod.refresh()
                last, last_full = sig, time.time()
                self._ui(self.on_ipod)
            time.sleep(3)

    @work(thread=True, group="ipod-refresh")
    def refresh_ipod(self) -> None:
        self.ipod.refresh()
        self._ui(self.on_ipod)

    def on_ipod(self) -> None:
        self.update_titlebar()
        self.rebuild_sidebar()
        if self.view.kind == "ipod":
            nv = self.ipod_view(self.view.source)
            nv.filter, nv.sort = self.view.filter, self.view.sort
            self.marked &= set(self.ipod.state.tracks)
            self.view = nv
            self.query_one("#view-title", Static).update(nv.title)
            self.query_one("#view-sub", Static).update(nv.subtitle)
            self.render_actions()
            self.render_list()
        else:
            self.refresh_rows(lambda it: isinstance(it, (Track, Album, Episode)))

    def targets(self) -> list[IPodTrack]:
        if self.view.kind != "ipod":
            return []
        if self.marked:
            return [t for t in self.rows if t.dbid in self.marked]
        item = self.selected()
        return [item] if isinstance(item, IPodTrack) else []

    @work(thread=True, exclusive=True, group="ipod-write")
    def ipod_apply(self, ops: list[list], done_msg: str) -> None:
        try:
            self.ipod.apply(ops)
        except Exception as e:  # noqa: BLE001
            self._ui(self.notify, str(e)[:300], title="iPod", severity="error")
            return
        self.ipod.refresh()
        self._ui(self.on_ipod)
        self._ui(self.notify, done_msg, title="iPod")

    def _marked_sub(self) -> None:
        self.query_one("#view-sub", Static).update(
            f"{self.view.subtitle}  ·  {len(self.marked)} markiert" if self.marked else self.view.subtitle)

    def action_mark(self) -> None:
        if isinstance(item := self.selected(), IPodTrack):
            self.marked ^= {item.dbid}
            t = self.current_table()
            self.refresh_rows(lambda it: it is item)
            if t.cursor_row + 1 < t.row_count:
                t.move_cursor(row=t.cursor_row + 1)
            self._marked_sub()

    def action_mark_all(self) -> None:
        if self.view.kind == "ipod":
            ids = {t.dbid for t in self.rows}
            self.marked = set() if ids <= self.marked else ids
            self.refresh_rows()
            self._marked_sub()

    def action_delete(self) -> None:
        if not (tracks := self.targets()):
            return
        what = f"„{tracks[0].display}“" if len(tracks) == 1 else f"{len(tracks)} Titel"

        def done(ok: bool | None) -> None:
            if ok:
                self.marked.clear()
                self.ipod_apply([["rm", t.dbid] for t in tracks], f"{what} gelöscht")

        self.push_screen(ConfirmScreen("✗ Vom iPod löschen", f"{what} endgültig vom iPod löschen?", "Löschen"), done)

    def action_edit(self) -> None:
        if not (tracks := self.targets()):
            return

        def done(changes: dict | None) -> None:
            if changes:
                ops = [["set", t.dbid, f, v] for t in tracks for f, v in changes.items()]
                self.ipod_apply(ops, f"{len(tracks)} Titel aktualisiert")

        self.push_screen(TagScreen(tracks), done)

    def action_add_to_playlist(self) -> None:
        if not (tracks := self.targets()):
            return

        def done(choice) -> None:
            if not choice:
                return
            kind, val = choice
            if kind == "id":
                self.ipod_apply([["pladd", val, t.dbid] for t in tracks], f"{len(tracks)} Titel hinzugefügt")
            else:
                self.new_playlist_with(val, tracks)

        self.push_screen(PlaylistScreen(self.ipod.state.playlists, len(tracks)), done)

    @work(thread=True, exclusive=True, group="ipod-write")
    def new_playlist_with(self, name: str, tracks: list[IPodTrack]) -> None:
        try:
            out = self.ipod.apply([["plnew", name]])
            plid = next(o[1] for o in out if o[0] == "pl")
            if tracks:
                self.ipod.apply([["pladd", plid, t.dbid] for t in tracks])
        except Exception as e:  # noqa: BLE001
            self._ui(self.notify, str(e)[:300], title="iPod", severity="error")
            return
        self.ipod.refresh()
        self._ui(self.on_ipod)
        self._ui(self.notify, f"Playlist „{name}“ angelegt", title="iPod")

    def action_new_playlist(self) -> None:
        if not self.ipod.state.mount:
            return
        tracks = self.targets() if self.marked else []
        hint = f" – die {len(tracks)} markierten Titel kommen hinein" if tracks else ""
        self.push_screen(TextScreen("+ Neue iPod-Playlist", "Name der Playlist" + hint),
                         lambda name: name and self.new_playlist_with(name, tracks))

    def _current_playlist(self):
        if self.view.kind == "ipod" and self.view.source != "all":
            pl = self.ipod.state.playlist(self.view.source)
            return pl if pl and pl.kind == "normal" else None
        return None

    def action_remove_from_playlist(self) -> None:
        pl, tracks = self._current_playlist(), self.targets()
        if pl and tracks:
            self.marked.clear()
            self.ipod_apply([["plrm", pl.id, t.dbid] for t in tracks], f"{len(tracks)} Titel aus „{pl.name}“ entfernt")

    def action_rename_playlist(self) -> None:
        if pl := self._current_playlist():
            self.push_screen(TextScreen("✎ Playlist umbenennen", "Neuer Name", pl.name),
                             lambda name: name and self.ipod_apply([["plren", pl.id, name]], f"Umbenannt in „{name}“"))

    def action_delete_playlist(self) -> None:
        if pl := self._current_playlist():
            def done(ok: bool | None) -> None:
                if ok:
                    self.ipod_apply([["pldel", pl.id]], f"Playlist „{pl.name}“ gelöscht")
                    self.action_nav("ipod:all")

            self.push_screen(ConfirmScreen("✗ Playlist löschen",
                                           f"Playlist „{pl.name}“ löschen? Die Titel bleiben auf dem iPod.",
                                           "Löschen"), done)

    @work(thread=True, group="ipod-write")
    def action_eject(self) -> None:
        if self.transfers.active:
            self._ui(self.notify, "Erst warten, bis die Übertragungen fertig sind.", severity="warning")
            return
        if self.engine.source == "local":
            self._ui(self.action_stop)
            time.sleep(0.3)
        try:
            self.ipod.eject()
            self._ui(self.notify, "Du kannst den iPod jetzt abziehen.", title="⏏ Ausgeworfen")
        except Exception as e:  # noqa: BLE001
            self._ui(self.notify, str(e)[:300], title="Auswerfen fehlgeschlagen", severity="error")

    # ── context-sensitive footer ─────────────────────────────────────────────
    def check_action(self, action: str, parameters: tuple) -> bool | None:
        kind = self.view.kind
        if action in ("mark", "mark_all", "edit", "add_to_playlist", "delete", "new_playlist"):
            return kind == "ipod"
        if action in ("remove_from_playlist", "rename_playlist", "delete_playlist"):
            return self._current_playlist() is not None
        if action in ("to_ipod", "view_to_ipod"):
            return kind in ("search", "tracks", "albums", "playlists", "episodes", "podcasts")
        if action == "retry":
            return kind == "jobs"
        if action == "back":
            return bool(self.history) or isinstance(self.focused, Input)
        if action == "choose_output":
            return PLAYER != "librespot" and self.api.logged_in
        return True

    def on_unmount(self) -> None:
        self.librespot_tries = 99
        self.engine.close()
        self.webplayer.stop()


def main() -> None:
    app = IPodify()
    # Closing the terminal window sends SIGHUP: shut down cleanly instead of dying mid-way.
    for sig in (signal.SIGHUP, signal.SIGTERM):
        signal.signal(sig, lambda *_: app.call_later(app.exit) if app.is_running else sys.exit(0))
    try:
        app.run()
    finally:
        app.webplayer.stop()
        app.engine.close()
