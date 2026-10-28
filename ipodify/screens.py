"""Small modal dialogs: confirm, text input, tag editor, playlist picker, Spotify login."""

from __future__ import annotations

from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Grid, Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Label, OptionList, Static
from textual.widgets.option_list import Option

from .core import IPodPlaylist, IPodTrack

DIALOG_CSS = """
.dialog {
    width: 72; height: auto; max-height: 90%; padding: 1 2;
    background: $surface; border: tall $primary; border-title-color: $primary;
}
.dialog Label.hint { color: $text-muted; margin-bottom: 1; }
.dialog .buttons { height: 3; margin-top: 1; align-horizontal: right; }
.dialog .buttons Button { margin-left: 1; min-width: 12; }
.dialog Input { margin-bottom: 0; }
.dialog .field { height: auto; }
.dialog .field Label { color: $text-muted; height: 1; }
.dialog OptionList { height: auto; max-height: 14; margin-bottom: 1; background: $background; }
.dialog #url { color: $text-muted; margin: 1 0; }
.tags { grid-size: 2; grid-gutter: 0 2; height: auto; grid-rows: auto; }
"""


class _Dialog(ModalScreen):
    DEFAULT_CSS = "_Dialog { align: center middle; background: $background 60%; }" + DIALOG_CSS
    BINDINGS = [Binding("escape", "cancel", "Abbrechen")]

    def action_cancel(self) -> None:
        self.dismiss(None)


class ConfirmScreen(_Dialog):
    def __init__(self, title: str, message: str, yes: str = "Ja") -> None:
        super().__init__()
        self.title_, self.message, self.yes = title, message, yes

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog") as d:
            d.border_title = self.title_
            yield Static(self.message)
            with Horizontal(classes="buttons"):
                yield Button("Abbrechen", id="no")
                yield Button(self.yes, id="yes", variant="error")

    def on_mount(self) -> None:
        self.query_one("#yes").focus()

    @on(Button.Pressed)
    def pressed(self, e: Button.Pressed) -> None:
        self.dismiss(e.button.id == "yes")


class ChoiceScreen(_Dialog):
    """A question with several answers; dismisses with the chosen key, None when cancelled."""

    def __init__(self, title: str, message: str, choices: list[tuple[str, str]]) -> None:
        super().__init__()
        self.title_, self.message, self.choices = title, message, choices

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog") as d:
            d.border_title = self.title_
            yield Static(self.message)
            with Horizontal(classes="buttons"):
                yield Button("Abbrechen", id="no")
                for i, (_key, label) in enumerate(self.choices):
                    yield Button(label, id=f"c{i}", variant="primary" if i == 0 else "default")

    def on_mount(self) -> None:
        self.query_one("#c0").focus()

    @on(Button.Pressed)
    def pressed(self, e: Button.Pressed) -> None:
        bid = e.button.id or ""
        self.dismiss(self.choices[int(bid[1:])][0] if bid.startswith("c") else None)


class TextScreen(_Dialog):
    def __init__(self, title: str, label: str, value: str = "") -> None:
        super().__init__()
        self.title_, self.label, self.value = title, label, value

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog") as d:
            d.border_title = self.title_
            yield Label(self.label, classes="hint")
            yield Input(self.value, id="value")
            with Horizontal(classes="buttons"):
                yield Button("Abbrechen", id="no")
                yield Button("OK", id="ok", variant="primary")

    @on(Input.Submitted)
    @on(Button.Pressed, "#ok")
    def ok(self) -> None:
        v = self.query_one("#value", Input).value.strip()
        self.dismiss(v or None)

    @on(Button.Pressed, "#no")
    def no(self) -> None:
        self.dismiss(None)


class TagScreen(_Dialog):
    """Edit tags of one or many iPod tracks. With many, empty fields stay unchanged."""

    FIELDS = [("title", "Titel"), ("artist", "Künstler"), ("album", "Album"), ("albumartist", "Album-Künstler"),
              ("genre", "Genre"), ("year", "Jahr"), ("track_nr", "Track-Nr."), ("rating", "Bewertung (0–5 ★)")]

    def __init__(self, tracks: list[IPodTrack]) -> None:
        super().__init__()
        self.tracks = tracks

    def _common(self, f: str) -> str:
        vals = {str(getattr(t, f) or "") for t in self.tracks}
        v = vals.pop() if len(vals) == 1 else ""
        return "" if v == "0" else v

    def compose(self) -> ComposeResult:
        many = len(self.tracks) > 1
        with Vertical(classes="dialog") as d:
            d.border_title = f"✎ {len(self.tracks)} Titel bearbeiten" if many else "✎ Titel bearbeiten"
            if many:
                yield Label("Leere Felder bleiben unverändert.", classes="hint")
            with Grid(classes="tags"):
                for f, label in self.FIELDS:
                    if many and f in ("title", "track_nr"):
                        continue
                    with Vertical(classes="field"):
                        yield Label(label)
                        yield Input(self._common(f), id=f"f-{f}")
            with Horizontal(classes="buttons"):
                yield Button("Abbrechen", id="no")
                yield Button("Speichern", id="ok", variant="primary")

    @on(Input.Submitted)
    @on(Button.Pressed, "#ok")
    def ok(self) -> None:
        changes = {}
        for inp in self.query(Input):
            f = inp.id.removeprefix("f-")
            v = inp.value.strip()
            if v != self._common(f) and (v or len(self.tracks) == 1):
                changes[f] = v
        self.dismiss(changes or None)

    @on(Button.Pressed, "#no")
    def no(self) -> None:
        self.dismiss(None)


class PlaylistScreen(_Dialog):
    """Pick an iPod playlist or type a name for a new one → ("id", plid) | ("new", name)."""

    def __init__(self, playlists: list[IPodPlaylist], count: int) -> None:
        super().__init__()
        self.playlists = [p for p in playlists if p.kind == "normal"]
        self.count = count

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog") as d:
            d.border_title = f"≡ {self.count} Titel zu Playlist hinzufügen"
            if self.playlists:
                yield OptionList(*(Option(f"≡ {p.name}  ({len(p.dbids)})", id=p.id) for p in self.playlists))
            yield Label("…oder neue Playlist:", classes="hint")
            yield Input(placeholder="Name der neuen Playlist", id="new")
            with Horizontal(classes="buttons"):
                yield Button("Abbrechen", id="no")

    @on(OptionList.OptionSelected)
    def picked(self, e: OptionList.OptionSelected) -> None:
        self.dismiss(("id", e.option.id))

    @on(Input.Submitted)
    def new(self, e: Input.Submitted) -> None:
        if e.value.strip():
            self.dismiss(("new", e.value.strip()))

    @on(Button.Pressed, "#no")
    def no(self) -> None:
        self.dismiss(None)


class LoginScreen(_Dialog):
    def __init__(self, url: str) -> None:
        super().__init__()
        self.url = url

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog") as d:
            d.border_title = "♫ Bei Spotify anmelden"
            yield Static("Im Browser hat sich die Spotify-Anmeldung geöffnet. Nach dem Bestätigen geht es hier "
                         "automatisch weiter.\n\nFalls sich kein Browser geöffnet hat, diesen Link öffnen:")
            yield Static(self.url, id="url")
            with Horizontal(classes="buttons"):
                yield Button("Schließen", id="no")

    @on(Button.Pressed, "#no")
    def no(self) -> None:
        self.dismiss(None)


class SetupScreen(_Dialog):
    """Collects the user's own Spotify app credentials → (client_id, client_secret)."""

    def __init__(self, redirect_uri: str, dashboard_url: str, client_id: str = "") -> None:
        super().__init__()
        self.redirect_uri, self.dashboard_url, self.client_id = redirect_uri, dashboard_url, client_id

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog") as d:
            d.border_title = "♫ Spotify einrichten"
            yield Static(
                "iPodify braucht eine eigene (kostenlose) Spotify-Developer-App:\n\n"
                f"  1. {self.dashboard_url} öffnen → „Create app“\n"
                f"  2. Redirect URI:  {self.redirect_uri}\n"
                "     APIs:  „Web API“ und „Web Playback SDK“ anhaken → Save\n"
                "  3. Unter „Settings“ Client ID und Client Secret kopieren:\n")
            with Vertical(classes="field"):
                yield Label("Client ID")
                yield Input(self.client_id, id="cid")
            with Vertical(classes="field"):
                yield Label("Client Secret")
                yield Input(id="secret", password=True)
            with Horizontal(classes="buttons"):
                yield Button("Dashboard öffnen", id="open")
                yield Button("Abbrechen", id="no")
                yield Button("Speichern & anmelden", id="ok", variant="primary")

    @on(Button.Pressed, "#open")
    def open_dashboard(self) -> None:
        from .spotify import open_url

        open_url(self.dashboard_url)

    @on(Input.Submitted)
    @on(Button.Pressed, "#ok")
    def ok(self) -> None:
        cid = self.query_one("#cid", Input).value.strip()
        secret = self.query_one("#secret", Input).value.strip()
        if len(cid) == 32 and len(secret) == 32:
            self.dismiss((cid, secret))
        else:
            self.notify("Client ID und Secret sind je 32 Zeichen lang.", severity="warning")

    @on(Button.Pressed, "#no")
    def no(self) -> None:
        self.dismiss(None)


DEVICE_ICON = {"computer": "▣", "smartphone": "▯", "speaker": "◉", "tv": "▭", "tablet": "▯",
               "avr": "◉", "castaudio": "◉", "castvideo": "▭", "automobile": "◈", "gameconsole": "◆"}


class DeviceScreen(_Dialog):
    """Pick the Spotify Connect device to play on → device dict, or "launch" to start the local app."""

    def __init__(self, devices: list[dict], current: str | None, can_launch: bool) -> None:
        super().__init__()
        self.devices, self.current, self.can_launch = devices, current, can_launch

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog") as d:
            d.border_title = "⇢ Wiedergabe auf …"
            opts = []
            for dev in self.devices:
                icon = DEVICE_ICON.get(dev["type"].lower(), "♫")
                tags = [t for t, on in (("läuft gerade", dev["is_active"]), ("ausgewählt", dev["id"] == self.current))
                        if on]
                vol = f"  · {dev['volume_percent']} %" if dev.get("volume_percent") is not None else ""
                opts.append(Option(f"{icon}  {dev['name']}  ({dev['type']}){vol}"
                                   + (f"  ← {', '.join(tags)}" if tags else ""), id=dev["id"]))
            if self.can_launch:
                opts.append(Option("▶  Player auf diesem Rechner starten", id="launch"))
            if opts:
                yield OptionList(*opts)
            else:
                yield Static("Kein Spotify-Gerät gefunden. Öffne Spotify auf einem Gerät und versuch es nochmal.")
            with Horizontal(classes="buttons"):
                yield Button("Abbrechen", id="no")

    @on(OptionList.OptionSelected)
    def picked(self, e: OptionList.OptionSelected) -> None:
        self.dismiss("launch" if e.option.id == "launch" else next(d for d in self.devices if d["id"] == e.option.id))

    @on(Button.Pressed, "#no")
    def no(self) -> None:
        self.dismiss(None)
