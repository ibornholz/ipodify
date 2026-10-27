"""Winamp-flavoured display widgets: title strip, marquee, real spectrum analyzer, seek and volume bars."""

from __future__ import annotations

import numpy as np
from rich.text import Text
from textual.message import Message
from textual.widget import Widget
from textual.widgets import Static

from .core import IPodState


class TitleBar(Static):
    def update_bar(self, ipod: IPodState, account: str, player_ok: bool) -> None:
        t = self.app.current_theme
        w = max(self.size.width, 40)
        left = Text.assemble((" ▀▄▀▄ ", t.primary), ("iPODIFY", f"bold {t.foreground}"), (" ▄▀▄▀ ", t.primary))
        sp = Text.assemble(("● " if player_ok else "○ ", t.success if player_ok else t.warning),
                           (account or "nicht bei Spotify angemeldet", t.foreground), "   ")
        if ipod.mount:
            ok = ipod.status == "bereit"
            ip = Text.assemble(("● ", t.success if ok else t.warning), (f"iPod {ipod.model}".strip(), "bold"),
                               (f" · {len(ipod.tracks)} Titel · {ipod.free_gb:.1f} GB frei", t.foreground),
                               ("" if ok else f" · {ipod.status}", t.warning), " ")
        else:
            ip = Text.assemble(("○ ", t.error), ("kein iPod ", t.foreground))
        right = sp + ip
        fill = w - left.cell_len - right.cell_len
        self.update(left + Text(" " + "═" * max(fill - 2, 0) + " ", style=t.panel) + right)


class Marquee(Static):
    def on_mount(self) -> None:
        self.text = "★ iPodify ★"
        self.offset_ = 0
        self.set_interval(0.18, self.tick)

    def set_text(self, text: str) -> None:
        if text != self.text:
            self.text, self.offset_ = text, 0
            self.tick(advance=False)

    def tick(self, advance: bool = True) -> None:
        w = max(self.size.width, 1)
        s = self.text + "   ***   "
        if len(self.text) <= w:
            view = self.text.ljust(w)
        else:
            if advance:
                self.offset_ = (self.offset_ + 1) % len(s)
            view = (s[self.offset_:] + s)[:w]
        self.update(Text(view, style=f"bold {self.app.current_theme.success}"))


class Spectrum(Widget):
    """Bars come from a real FFT of the audio that is playing (see audio.Analyzer)."""

    BLOCKS = " ▁▂▃▄▅▆▇█"

    def __init__(self, source, **kw) -> None:
        super().__init__(**kw)
        self.source = source  # callable(n) -> np.ndarray of 0..1
        self.levels = np.zeros(0)
        self.peaks = np.zeros(0)
        self.hold = np.zeros(0)

    def on_mount(self) -> None:
        self.set_interval(1 / 24, self.tick)

    def tick(self) -> None:
        n = max(self.size.width // 2, 1)
        if len(self.levels) != n:
            self.levels, self.peaks, self.hold = np.zeros(n), np.zeros(n), np.zeros(n)
        new = self.source(n)
        if len(new) != n:
            new = np.zeros(n)
        self.levels = np.maximum(new, self.levels * 0.78)  # instant attack, smooth fall
        rising = self.levels >= self.peaks
        self.peaks = np.where(rising, self.levels, self.peaks)
        self.hold = np.where(rising, 12, self.hold - 1)
        self.peaks = np.where(self.hold <= 0, np.maximum(self.peaks - 0.025, 0), self.peaks)
        self.refresh()

    def render(self) -> Text:
        t = self.app.current_theme
        h = max(self.size.height, 1)
        rows = []
        for r in range(h, 0, -1):
            frac = r / h
            color = t.error if frac > 0.8 else t.warning if frac > 0.55 else t.success
            line = Text()
            for lv, pk in zip(self.levels, self.peaks):
                fill = lv * h * 8 - (r - 1) * 8
                if fill >= 8:
                    line.append("█ ", style=color)
                elif fill >= 1:
                    line.append(self.BLOCKS[int(fill)] + " ", style=color)
                elif pk > 0.04 and min(int(pk * h), h - 1) == r - 1:
                    line.append("▔ ", style=t.foreground)
                else:
                    line.append("  ")
            rows.append(line)
        return Text("\n").join(rows)


class SeekBar(Static):
    class Seek(Message):
        def __init__(self, frac: float) -> None:
            super().__init__()
            self.frac = frac

    def show(self, pos: float | None, dur: float | None) -> None:
        t = self.app.current_theme
        w = max(self.size.width, 10)
        frac = min(max((pos or 0) / dur, 0), 1) if dur else 0
        k = min(int(frac * (w - 1)), w - 1)
        self.update(Text.assemble(("━" * k, t.primary), ("◆", f"bold {t.accent}"), ("─" * (w - k - 1), t.panel)))

    def on_click(self, event) -> None:
        self.post_message(self.Seek(event.x / max(self.size.width - 1, 1)))


class VolumeBar(Static):
    class Changed(Message):
        def __init__(self, volume: int) -> None:
            super().__init__()
            self.volume = volume

    def show(self, vol: int) -> None:
        t = self.app.current_theme
        n = round(vol / 10)
        self.update(Text.assemble(("VOL ", t.foreground), ("▮" * n, t.success), ("▯" * (10 - n), t.panel),
                                  (f" {vol:>3}", t.foreground)))

    def on_click(self, event) -> None:
        if 4 <= event.x <= 14:
            self.post_message(self.Changed(min(100, (event.x - 3) * 10)))
