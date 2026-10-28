import pytest
import spotipy

from ipodify import spotify
from ipodify.spotify import SpotifyAPI

ALBUM = {"id": "al1", "uri": "spotify:album:al1", "name": "Rumours", "artists": [{"name": "Fleetwood Mac"}],
         "release_date": "1977-02-04", "total_tracks": 11, "external_urls": {"spotify": "https://open.spotify.com/album/al1"},
         "images": [{"url": "https://i/640.jpg"}, {"url": "https://i/300.jpg"}]}


def item(kind, i):
    base = {"id": f"{kind}{i}", "uri": f"spotify:{kind}:{i}", "name": f"{kind} {i}", "external_urls": {},
            "artists": [{"name": "A"}], "owner": {"display_name": "me"}, "tracks": {"total": 3}, "genres": [],
            "album": ALBUM, "duration_ms": 1000, "release_date": "2020", "total_tracks": 1}
    return base


class FakeSp:
    def __init__(self, fail_from=99):
        self.calls, self.fail_from = [], fail_from

    def search(self, q, type, market, limit, offset):
        self.calls.append((limit, offset))
        if offset >= self.fail_from:
            raise spotipy.SpotifyException(400, -1, "Invalid limit")
        n = offset // 10
        mk = lambda kind: {"items": [item(kind, n * 10 + i) for i in range(10)] + ([item(kind, 0)] if n else [])}
        return {"tracks": mk("track"), "albums": mk("album"), "artists": mk("artist"), "playlists": mk("playlist")}

    def album(self, sid, market=None):
        return ALBUM


@pytest.fixture
def api(monkeypatch):
    a = SpotifyAPI()
    fake = FakeSp()
    monkeypatch.setattr(SpotifyAPI, "sp", property(lambda self: fake))
    a.fake = fake
    return a


def test_track_mapping(api):
    t = api._track({"id": "t1", "uri": "spotify:track:t1", "name": "Dreams", "artists": [{"name": "Fleetwood Mac"}],
                    "duration_ms": 257000, "track_number": 2, "external_urls": {}}, ALBUM)
    assert (t.album, t.year, t.track_nr, t.cover) == ("Rumours", 1977, 2, "https://i/640.jpg")
    assert t.url == "https://open.spotify.com/track/t1"
    assert api._track({"id": "e1", "type": "episode"}) is None
    assert api._track(None) is None
    a = api._album(ALBUM)
    assert (a.name, a.artist, a.year, a.total) == ("Rumours", "Fleetwood Mac", 1977, 11)


def test_search_merges_pages_and_dedupes(api):
    r = api.search("fleetwood")
    assert sorted(api.fake.calls) == [(10, 0), (10, 10), (10, 20), (10, 30), (10, 40)]
    assert all(len(v) == 50 for v in r.values())  # the repeated item 0 on later pages was dropped
    assert [t.id for t in r["tracks"][:3]] == ["track0", "track1", "track2"]


def test_search_survives_failing_later_pages(api):
    api.fake.fail_from = 20
    r = api.search("x")
    assert len(r["albums"]) == 20


def test_search_first_page_error_propagates(api):
    api.fake.fail_from = 0
    with pytest.raises(spotipy.SpotifyException):
        api.search("x")


def test_search_resolves_links(api):
    r = api.search("https://open.spotify.com/intl-de/album/al1?si=abc")
    assert [a.name for a in r["albums"]] == ["Rumours"] and r["tracks"] == []
    assert api.fake.calls == []
