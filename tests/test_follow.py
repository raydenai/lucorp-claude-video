"""Offline follow/storage tests: every library lives below pytest's tmp_path."""
from __future__ import annotations

import io
import json
import multiprocessing
import os
from pathlib import Path
import subprocess
from types import SimpleNamespace

import pytest

from claude_real_video import follow


@pytest.fixture(autouse=True)
def root(tmp_path, monkeypatch):
    monkeypatch.setenv("CRV_ROOT", str(tmp_path))
    return tmp_path


@pytest.mark.parametrize("url,kind,ident", [
    ("https://www.youtube.com/watch?v=abc", "video", "abc"),
    ("https://youtube.com/watch?v=abc&list=PL1", "video", "abc"),
    ("https://youtu.be/abc?t=5", "video", "abc"),
    ("https://m.youtube.com/shorts/abc", "video", "abc"),
    ("https://youtube.com/embed/abc", "video", "abc"),
    ("https://youtube.com/live/abc", "video", "abc"),
    ("https://youtube.com/@sample", "channel", "@sample"),
    ("https://youtube.com/@sample/videos", "channel", "@sample"),
    ("https://youtube.com/channel/UC1", "channel", "UC1"),
    ("https://youtube.com/c/sample", "channel", "sample"),
    ("https://youtube.com/user/sample", "channel", "sample"),
    ("https://youtube.com/playlist?list=PL1", "playlist", "PL1"),
    ("https://youtube.com/watch?list=PL1", "playlist", "PL1"),
])
def test_classify(url, kind, ident):
    assert follow.classify(url) == {"kind": kind, "id": ident, "title": None, "url": url}


def test_classify_non_youtube():
    for url in ("https://vimeo.com/123", "https://youtube.com.evil.test/watch?v=abc"):
        value = follow.classify(url)
        assert value["kind"] == "video"
        assert value["id"] == follow.classify(url)["id"]
        assert value["url"] == url


def entry(ident, title="AI Tutorial", duration=600, shorts=False):
    return {"id": ident, "title": title, "duration": duration,
            "url": "https://www.youtube.com/" + ("shorts/" if shorts else "watch?v=") + ident}


def channel(monkeypatch, entries):
    monkeypatch.setattr(follow, "_extract_info", lambda *a, **kw: {
        "channel_id": "UCsample", "uploader_id": "@sample", "channel": "Sample", "entries": entries})


def test_subscription_backfill_filters(monkeypatch, root):
    channel(monkeypatch, [entry("shorturl", duration=600, shorts=True),
                          entry("shortduration", duration=60), entry("brief", duration=90),
                          entry("boring", title="Cooking"), entry("first"),
                          entry("second", title="Learn aI"), entry("older")])
    sub = follow.add_subscription("https://youtube.com/@sample", "Learn/Sub", backfill=2,
                                  min_minutes=5, keywords=["AI"])
    assert sub["channel_id"] == "UCsample"
    assert sub["handle"] == "@sample" and not sub["paused"]
    assert set(sub) == {"id", "channel_id", "handle", "title", "url", "folder", "backfill",
                        "include_shorts", "min_minutes", "keywords", "paused", "last_checked",
                        "seen", "created_at"}
    assert {j["video_id"] for j in follow.list_queue()} == {"first", "second"}
    assert "shortduration" in sub["seen"]
    assert follow.list_subscriptions() == [sub]
    assert follow.update_subscription(sub["id"], paused=True)["paused"]
    follow.remove_subscription(sub["id"])
    assert follow.list_subscriptions() == []
    assert (root / "_crv/subscriptions.json").exists()


@pytest.mark.parametrize("backfill,shorts,expected", [(0, True, set()), (-1, True, {"long", "short"}),
                                                    (-1, False, {"long"})])
def test_backfill_zero_all(monkeypatch, backfill, shorts, expected):
    channel(monkeypatch, [entry("long"), entry("short", duration=60)])
    follow.add_subscription("https://youtube.com/@sample", backfill=backfill, include_shorts=shorts)
    assert {j["video_id"] for j in follow.list_queue()} == expected


def test_yt_dlp_command(monkeypatch):
    calls = []
    def run(command, **kwargs):
        calls.append(command)
        return SimpleNamespace(stdout='{"id": "UCsample", "entries": []}')
    monkeypatch.setattr(follow.subprocess, "run", run)
    assert follow._extract_info("https://youtube.com/@sample")["id"] == "UCsample"
    assert calls[0][1:3] == ["-m", "yt_dlp"]
    assert "--flat-playlist" in calls[0]


def test_enqueue_dedupe_all_active_statuses(root):
    job = follow.enqueue("https://youtu.be/abc", title="Title")
    assert set(job) == {"id", "video_id", "url", "title", "folder", "channel", "status", "error",
                        "out_dir", "created_at", "updated_at"}
    for status in ("queued", "processing", "done"):
        with follow._lock():
            jobs = follow._read("queue")
            jobs[0]["status"] = status
            follow._write("queue", jobs)
        assert follow.enqueue("https://youtube.com/watch?v=abc")["id"] == job["id"]
    with follow._lock():
        jobs[0]["status"] = "failed"
        follow._write("queue", jobs)
    replacement = follow.enqueue("https://youtube.com/shorts/abc")
    assert replacement["id"] != job["id"]
    assert follow.list_queue("queued") == [replacement]
    follow.remove_job(replacement["id"])
    assert len(follow.list_queue()) == 1
    assert follow.retry(job["id"])["status"] == "queued"


def test_rss_parser(monkeypatch):
    body = b'''<feed xmlns="http://www.w3.org/2005/Atom" xmlns:yt="http://www.youtube.com/xml/schemas/2015">
    <entry><yt:videoId>abc</yt:videoId><title>AI &amp; tools</title>
    <link rel="alternate" href="https://youtube.com/watch?v=abc"/></entry></feed>'''
    calls = []
    def open_feed(url, **kwargs):
        calls.append(url)
        return io.BytesIO(body)
    monkeypatch.setattr(follow, "urlopen", open_feed)
    assert follow._rss("UC1")[0]["title"] == "AI & tools"
    assert calls == ["https://www.youtube.com/feeds/videos.xml?channel_id=UC1"]


def test_check_only_unseen_and_filters(monkeypatch):
    channel(monkeypatch, [entry("old")])
    sub = follow.add_subscription("https://youtube.com/@sample", backfill=0, min_minutes=5,
                                  keywords=["AI"])
    monkeypatch.setattr(follow, "_rss", lambda _: [entry("old"), entry("new"), entry("short", duration=20),
                                                 entry("nomatch", title="Cooking")])
    calls = []
    def extract(url, **kwargs):
        ident = follow.classify(url)["id"]
        calls.append(ident)
        return entry(ident, title="Cooking" if ident == "nomatch" else "AI tools",
                     duration=20 if ident == "short" else 600)
    monkeypatch.setattr(follow, "_extract_info", extract)
    assert follow.check_all() == {"checked": 1, "new": 1}
    assert follow.check_all() == {"checked": 1, "new": 0}
    assert calls == ["new", "short", "nomatch"]
    assert follow.list_queue()[0]["video_id"] == "new"
    current = follow.list_subscriptions()[0]
    assert current["last_checked"] and set(current["seen"]) == {"old", "new", "short", "nomatch"}
    follow.update_subscription(sub["id"], paused=True)
    assert follow.check_all() == {"checked": 0, "new": 0}


@pytest.mark.parametrize("returncode,expected", [(0, "done"), (2, "failed")])
def test_work_status_and_output(monkeypatch, root, returncode, expected):
    first = follow.enqueue("https://youtu.be/first", folder="Learn/Sub", channel="@sample", title="Hello")
    second = follow.enqueue("https://youtu.be/second")
    calls = []
    def run(command, **kwargs):
        calls.append(command)
        if command[0] == "osascript":
            raise FileNotFoundError("no notifications")
        active = next(j for j in follow.list_queue() if j["id"] == first["id"])
        assert active["status"] == "processing"
        out = Path(kwargs["env"]["CRV_WATCH_OUT"])
        assert out == root / "Learn/Sub/sample/Hello-first"
        assert command[1:] == [first["url"], str(out.parent), out.name]
        assert command[0].endswith("contrib/crv-watch")
        return SimpleNamespace(returncode=returncode, stderr="x" * 5000, stdout="")
    monkeypatch.setattr(follow.subprocess, "run", run)
    result = follow.work_once()
    assert result["id"] == first["id"] and result["status"] == expected
    assert result["error"] == (None if returncode == 0 else "x" * 4000)
    assert calls[-1][0] == "osascript"
    assert next(j for j in follow.list_queue() if j["id"] == second["id"])["status"] == "queued"


def test_work_exception_single_layout_and_empty(monkeypatch, root):
    assert follow.work_once() is None
    follow.enqueue("https://youtu.be/one", folder="Inbox", title="One")
    def run(command, **kwargs):
        if command[0] != "osascript":
            assert kwargs["env"]["CRV_WATCH_OUT"] == str(root / "Inbox/One-one")
        raise OSError("pipeline missing")
    monkeypatch.setattr(follow.subprocess, "run", run)
    assert follow.work_once()["error"] == "pipeline missing"
    assert follow.work_once() is None


def test_folder_operations(monkeypatch, root):
    follow.create_folder("Learn/Sub")
    follow.create_folder("Other")
    video = root / "Learn/Sub/clip"
    video.mkdir()
    (video / "transcript.json").write_text('{}')
    (root / "_hidden").mkdir()
    channel(monkeypatch, [])
    sub = follow.add_subscription("https://youtube.com/@sample", folder="Learn/Sub", backfill=0)
    folders = follow.list_folders()
    assert [f["name"] for f in folders] == ["Learn", "Other"]
    assert folders[0]["videos"] == 1
    assert folders[0]["subfolders"][0]["path"] == "Learn/Sub"
    follow.rename_folder("Learn", "Renamed")
    assert follow.list_subscriptions()[0]["folder"] == "Renamed/Sub"
    assert (root / "Renamed/Sub/clip/transcript.json").exists()
    follow.move_video("Renamed/Sub/clip", "Other")
    assert (root / "Other/clip/transcript.json").exists()
    with pytest.raises(OSError):
        follow.delete_folder("Other")
    follow.delete_folder("Renamed/Sub")
    follow.delete_folder("Renamed")
    assert follow.update_subscription(sub["id"], folder="Other")["folder"] == "Other"


@pytest.mark.parametrize("name", ["../outside", "/tmp/outside", ".hidden", "_system", "A/../B",
                                 "A/_hidden", "A/.hidden", "A//B", "A/", "", "A\\B", "A..B"])
def test_invalid_paths(name):
    with pytest.raises(ValueError):
        follow.create_folder(name)
    with pytest.raises(ValueError):
        follow.enqueue("https://youtu.be/abc", folder=name)
    with pytest.raises(ValueError):
        follow.delete_folder(name)


def test_symlink_escape(root):
    (root / "escape").symlink_to(root.parent)
    with pytest.raises(ValueError):
        follow.create_folder("escape/outside")
    with pytest.raises(ValueError):
        follow.enqueue("https://youtu.be/abc", folder="escape/outside")
    assert follow.list_folders() == []


def append_jobs(root, prefix):
    os.environ["CRV_ROOT"] = root
    for index in range(20):
        follow.enqueue(f"https://youtu.be/{prefix}-{index}")


def test_atomic_concurrent_processes(root, monkeypatch):
    replacements = []
    original = follow.os.replace
    def replace(source, dest):
        # Atomic write source is complete valid JSON, in the destination directory.
        assert Path(source).parent == Path(dest).parent
        json.loads(Path(source).read_text())
        replacements.append(str(dest))
        return original(source, dest)
    monkeypatch.setattr(follow.os, "replace", replace)
    follow.enqueue("https://youtu.be/seed")
    assert replacements
    # Spawn avoids accidentally inheriting lock descriptors from the parent.
    context = multiprocessing.get_context("spawn")
    processes = [context.Process(target=append_jobs, args=(str(root), prefix)) for prefix in ("a", "b")]
    for process in processes:
        process.start()
    for process in processes:
        process.join(30)
        assert process.exitcode == 0
    data = json.loads((root / "_crv/queue.json").read_text())
    assert len(data) == 41 and len({j["video_id"] for j in data}) == 41
    assert not list((root / "_crv").glob("*.tmp"))


def test_cli(monkeypatch, capsys):
    channel(monkeypatch, [])
    follow.main(["add", "https://youtube.com/@sample", "--backfill", "0", "--shorts",
                 "--keywords", "AI,tools", "--folder", "Learning"])
    sub = json.loads(capsys.readouterr().out)
    assert sub["include_shorts"] and sub["keywords"] == ["AI", "tools"]
    follow.main(["pause", sub["id"]])
    assert json.loads(capsys.readouterr().out)["paused"]
    follow.main(["resume", sub["id"]])
    assert not json.loads(capsys.readouterr().out)["paused"]
    follow.main(["list"])
    assert len(json.loads(capsys.readouterr().out)) == 1
    follow.main(["queue", "--status", "queued"])
    assert json.loads(capsys.readouterr().out) == []
    follow.main(["work", "--loop"])
    assert json.loads(capsys.readouterr().out) == {"jobs": []}


def test_notification_best_effort(monkeypatch):
    def run(*args, **kwargs):
        raise subprocess.TimeoutExpired("osascript", 10)
    monkeypatch.setattr(follow.subprocess, "run", run)
    follow._notify({"video_id": "abc", "status": "done", "title": 'quote " and slash \\'})


def test_uploads_playlist_orders_mixed_types_without_dates(monkeypatch):
    calls = []
    def extract(url, **kwargs):
        calls.append(url)
        if "playlist?list=" in url:
            # YouTube's uploads playlist supplies newest-first ordering even
            # when flat entries expose no timestamps or upload dates.
            return {"entries": [entry("short", duration=30, shorts=True), entry("older")]}
        return {"id": "UCsample", "uploader_id": "@sample", "entries": [
            {"entries": [entry("older")]},
            {"entries": [entry("short", duration=30, shorts=True)]}]}
    monkeypatch.setattr(follow, "_extract_info", extract)
    follow.add_subscription("https://youtube.com/@sample/videos", backfill=1, include_shorts=True)
    assert calls == ["https://youtube.com/@sample",
                     "https://www.youtube.com/playlist?list=UUsample"]
    assert follow.list_queue()[0]["video_id"] == "short"


def test_output_components_do_not_alias():
    assert follow._unique_component("abc_def") != follow._unique_component("abc-def")
    assert follow._unique_component("_abc") != follow._unique_component("abc")
    assert follow._channel_name("@ab_cd") != follow._channel_name("@ab-cd")


def test_retry_uses_renamed_out_dir(monkeypatch, root):
    job = follow.enqueue("https://youtu.be/abc", folder="Inbox", channel="@sample")
    monkeypatch.setattr(follow, "_notify", lambda job: None)
    def run(command, **kwargs):
        Path(kwargs["env"]["CRV_WATCH_OUT"]).mkdir(parents=True, exist_ok=True)
        return SimpleNamespace(returncode=1, stderr="failed", stdout="")
    monkeypatch.setattr(follow.subprocess, "run", run)
    failed = follow.work_once()
    follow.rename_folder("Inbox/sample", "Inbox/Renamed")
    follow.retry(job["id"])
    retried = follow.work_once()
    assert Path(retried["out_dir"]).parent == root / "Inbox/Renamed"
    assert Path(retried["out_dir"]).name == Path(failed["out_dir"]).name
    assert not (root / "Inbox/sample").exists()


def test_work_title_with_ellipsis(monkeypatch, root):
    follow.enqueue("https://youtu.be/abc", title="Wait... what?")
    def run(command, **kwargs):
        if command[0] == "osascript":
            return SimpleNamespace(returncode=0)
        out = Path(kwargs["env"]["CRV_WATCH_OUT"])
        assert out.is_relative_to(root)
        assert ".." not in out.name
        return SimpleNamespace(returncode=0, stdout="", stderr="")
    monkeypatch.setattr(follow.subprocess, "run", run)
    assert follow.work_once()["status"] == "done"
    assert follow._unique_component("a..b") != follow._unique_component("a-b")
