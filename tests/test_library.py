"""API tests for the Video Library server (library.py) on a temp copy of a library.

Never touches crv-out: every test builds its own tiny library in tmp_path and
points library.ROOT at it. Covers the exact contract the themed index.html
depends on: /api/library, /api/video, /api/search, POST /api/my-notes,
/media with HTTP Range, and the two HTML pages.
"""
from __future__ import annotations

import http.client
import http.server
import json
import threading
import urllib.parse

import pytest

from claude_real_video import library

VIDEO = bytes(range(256)) * 40          # 10,240 bytes of known content
NOTES = """# call-09: Pricing (Test coaching)

## Summary
- Lead with the outcome [@1:02:10] and not the price.

## Action steps
- [ ] Rewrite the offer lines [@0:30]
- [x] Book the next call [@2:00]
"""
SEGS = {"segments": [
    {"start": 0.0, "end": 4.0, "speaker": "Bala", "text": "Welcome to the pricing call."},
    {"start": 4.5, "end": 9.0, "speaker": "John", "text": "Let us talk about the guarantee first."},
    {"start": 9.5, "end": 12.0, "speaker": "Bala", "text": "A guarantee changes the whole offer."},
]}


@pytest.fixture()
def lib(tmp_path, monkeypatch):
    vid = tmp_path / "coaching" / "acme" / "call-09-pricing"
    vid.mkdir(parents=True)
    (vid / "transcript.json").write_text(json.dumps(SEGS), encoding="utf-8")
    (vid / "notes.md").write_text(NOTES, encoding="utf-8")
    (vid / "source.mp4").write_bytes(VIDEO)
    bare = tmp_path / "seo" / "01-no-video"          # no video file, no notes
    bare.mkdir(parents=True)
    (bare / "transcript.json").write_text(json.dumps({"segments": [
        {"start": 1, "end": 2, "speaker": "", "text": "Only a transcript here."}]}), encoding="utf-8")
    monkeypatch.setattr(library, "ROOT", tmp_path.resolve())
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), library.H)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield {"port": srv.server_address[1], "dir": vid, "root": tmp_path}
    srv.shutdown()
    srv.server_close()


def call(lib, method, path, body=None, headers=None):
    c = http.client.HTTPConnection("127.0.0.1", lib["port"], timeout=10)
    c.request(method, path, body=body, headers=headers or {})
    r = c.getresponse()
    data = r.read()
    c.close()
    return r.status, dict(r.getheaders()), data


def jget(lib, path):
    st, _, data = call(lib, "GET", path)
    return st, json.loads(data)


ID = "coaching/acme/call-09-pricing"
Q = urllib.parse.quote(ID)


def test_library_shape(lib):
    st, j = jget(lib, "/api/library")
    assert st == 200
    by = {v["id"]: v for v in j["videos"]}
    v = by[ID]
    assert set(v) >= {"id", "topic", "sub", "title", "duration", "speakers", "has_video",
                      "has_notes", "has_actions", "has_my_notes"}
    assert (v["topic"], v["sub"]) == ("coaching", "acme")
    assert v["title"].startswith("call-09")
    assert v["duration"] == 12.0 and v["speakers"] == ["Bala", "John"]
    assert v["has_video"] and v["has_notes"] and v["has_actions"] and not v["has_my_notes"]
    b = by["seo/01-no-video"]
    assert not b["has_video"] and not b["has_notes"] and not b["has_actions"] and b["sub"] == ""


def test_video_payload(lib):
    st, j = jget(lib, f"/api/video?id={Q}")
    assert st == 200
    assert j["segments"][1]["speaker"] == "John" and len(j["segments"]) == 3
    assert "[@1:02:10]" in j["notes"]
    assert j["actions"].startswith("## Action steps") and "- [x] Book the next call" in j["actions"]
    assert j["my_notes"] == ""


def test_video_bad_ids(lib):
    assert jget(lib, "/api/video?id=../../etc")[0] == 400
    assert jget(lib, "/api/video?id=nope/none")[0] == 400
    assert jget(lib, "/api/video")[0] == 400


def test_search(lib):
    st, hits = jget(lib, "/api/search?q=guarantee")
    assert st == 200
    kinds = {h["kind"] for h in hits}
    assert kinds == {"transcript"}
    assert [h["t"] for h in hits] == [4.5, 9.5]
    assert hits[0]["speaker"] == "John" and set(hits[0]) == {"id", "title", "kind", "t", "speaker", "text"}
    st, hits = jget(lib, "/api/search?q=outcome+price")           # every word, any order, in the notes
    assert [h["kind"] for h in hits] == ["ai notes"] and hits[0]["t"] == 3730
    assert jget(lib, "/api/search?q=guarantee&topic=seo")[1] == []
    assert jget(lib, "/api/search?q=guarantee&topic=coaching")[1] != []
    assert jget(lib, "/api/search?q=")[1] == []


def test_my_notes_round_trip_and_search(lib):
    text = "My thought [@0:12]\nsecond line é中"
    st, _, data = call(lib, "POST", f"/api/my-notes?id={Q}", body=text.encode("utf-8"),
                       headers={"Content-Type": "text/markdown"})
    assert st == 200 and json.loads(data)["saved"] is True
    assert (lib["dir"] / "my-notes.md").read_text(encoding="utf-8") == text
    assert not (lib["dir"] / "my-notes.md.tmp").exists()          # atomic replace left nothing behind
    st, j = jget(lib, f"/api/video?id={Q}")
    assert j["my_notes"] == text
    v = {v["id"]: v for v in jget(lib, "/api/library")[1]["videos"]}[ID]
    assert v["has_my_notes"] is True
    hits = jget(lib, "/api/search?q=thought")[1]
    assert [(h["kind"], h["t"]) for h in hits] == [("my notes", 12)]
    # saving empty text clears the flag but keeps the file (no deletion path in the API)
    call(lib, "POST", f"/api/my-notes?id={Q}", body=b"")
    assert jget(lib, f"/api/video?id={Q}")[1]["my_notes"] == ""


def test_my_notes_rejects_bad_requests(lib):
    assert call(lib, "POST", "/api/my-notes?id=../x", body=b"x")[0] == 400
    assert call(lib, "POST", "/api/my-notes", body=b"x")[0] == 400
    assert call(lib, "POST", "/api/nope?id=" + Q, body=b"x")[0] == 404
    big = {"Content-Length": "5000001"}
    c = http.client.HTTPConnection("127.0.0.1", lib["port"], timeout=10)
    c.request("POST", f"/api/my-notes?id={Q}", headers=big)
    assert c.getresponse().status == 413
    c.close()
    assert not (lib["dir"] / "my-notes.md").exists()


def test_existing_my_notes_are_read_unchanged(lib):
    old = "# my notes\n- from before the redesign [@5:00]\n"
    (lib["dir"] / "my-notes.md").write_text(old, encoding="utf-8")
    assert jget(lib, f"/api/video?id={Q}")[1]["my_notes"] == old


def test_media_full_and_range(lib):
    url = f"/media?id={Q}"
    st, h, data = call(lib, "GET", url)
    assert st == 200 and data == VIDEO
    assert h["Accept-Ranges"] == "bytes" and h["Content-Type"] == "video/mp4"
    assert h["Content-Length"] == str(len(VIDEO))

    st, h, data = call(lib, "GET", url, headers={"Range": "bytes=100-199"})
    assert st == 206 and data == VIDEO[100:200]
    assert h["Content-Range"] == f"bytes 100-199/{len(VIDEO)}" and h["Content-Length"] == "100"

    st, h, data = call(lib, "GET", url, headers={"Range": "bytes=10000-"})     # open ended
    assert st == 206 and data == VIDEO[10000:]

    st, h, data = call(lib, "GET", url, headers={"Range": "bytes=-50"})        # suffix
    assert st == 206 and data == VIDEO[-50:]
    assert h["Content-Range"] == f"bytes {len(VIDEO) - 50}-{len(VIDEO) - 1}/{len(VIDEO)}"

    st, h, data = call(lib, "GET", url, headers={"Range": "bytes=0-999999"})   # end past EOF is clamped
    assert st == 206 and data == VIDEO

    st, h, _ = call(lib, "GET", url, headers={"Range": "bytes=999999-"})       # unsatisfiable
    assert st == 416 and h["Content-Range"] == f"bytes */{len(VIDEO)}"


def test_media_missing_video_and_bad_id(lib):
    assert call(lib, "GET", "/media?id=seo/01-no-video")[0] == 404
    assert call(lib, "GET", "/media?id=../..")[0] == 400


def test_pages(lib):
    st, h, data = call(lib, "GET", "/")
    html = data.decode("utf-8")
    assert st == 200 and h["Content-Type"].startswith("text/html")
    # the themed single file: six themes, the picker, no external requests
    for t in ("paper", "studio", "index", "overworld", "dungeon", "pocket"):
        assert f'data-theme=' in html and t in html
    for needle in ("vl.theme", "vl.mode", "/api/library", "/api/video?id=", "/api/search?q=",
                   "/api/my-notes?id=", "/media?id="):
        assert needle in html, needle
    import re
    assert not re.search(r'(src|href)=["\']https?://', html)          # offline: no CDN, no external font
    assert "@import" not in html
    st, _, classic = call(lib, "GET", "/classic")
    assert st == 200 and b"crvlib" in classic                           # the old UI stays reachable
    assert call(lib, "GET", "/index.html")[0] == 200
    assert call(lib, "GET", "/nope")[0] == 404


def test_best_moment_points_unstamped_note_at_its_transcript_part():
    segs = [{"start": 0, "text": "welcome to the show today"},
            {"start": 30, "text": "build citations first then reviews on the business profile"},
            {"start": 60, "text": "thanks for watching"}]
    assert library.best_moment("- Build citations and reviews on the Business Profile first", segs) == 30
    assert library.best_moment("# Build citations reviews business profile", segs) is None   # headings never matched
    assert library.best_moment("- Totally unrelated pricing tiers story", segs) is None      # weak overlap = no time
