"""The Textual UI itself is not driven here; only its pure helpers."""

from ipodify.app import View
from ipodify.core import Track


def mk(i, title, artist, ms):
    return Track(id=str(i), uri="", title=title, artists=[artist], album="A", duration_ms=ms, url="")


def test_view_filter_and_sort():
    v = View("tracks", "t", items=[mk(1, "Beta", "Zed", 300), mk(2, "alpha", "Amy", 100), mk(3, "Gamma", "Bob", 200)])
    assert [t.title for t in v.visible()] == ["Beta", "alpha", "Gamma"]
    v.filter = "AL"
    assert [t.title for t in v.visible()] == ["alpha"]
    v.filter = ""
    v.sort = ("title", False)
    assert [t.title for t in v.visible()] == ["alpha", "Beta", "Gamma"]  # case-insensitive
    v.sort = ("time", True)
    assert [t.duration_ms for t in v.visible()] == [300, 200, 100]
