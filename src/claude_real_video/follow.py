"""Channel subscriptions, a persistent processing queue, and library folders."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
from urllib.parse import parse_qs, urlparse
from urllib.request import urlopen
import uuid
import xml.etree.ElementTree as ET


def _now():
    return datetime.now(timezone.utc).isoformat()


def _root():
    return Path(os.environ.get("CRV_ROOT", "~/crv-out")).expanduser().resolve()


def _path(name):
    if not isinstance(name, str) or not name or "\\" in name:
        raise ValueError("invalid relative library path")
    parts = name.split("/")
    if any(not p or p.startswith((".", "_")) or ".." in p or
           any(ord(c) < 32 for c in p) for p in parts):
        raise ValueError("invalid relative library path")
    path = (_root() / name).resolve()
    if not path.is_relative_to(_root()) or path == _root():
        raise ValueError("outside library")
    return path


def _store():
    root = _root()
    store = root / "_crv"
    if store.is_symlink():
        raise ValueError("storage must not be a symlink")
    store.mkdir(parents=True, exist_ok=True)
    return store


@contextmanager
def _lock(worker=False):
    path = _store() / (".worker.lock" if worker else ".lock")
    if path.is_symlink():
        raise ValueError("lock must not be a symlink")
    with path.open("a") as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(stream, fcntl.LOCK_UN)


def _read(name):
    path = _store() / (name + ".json")
    if path.is_symlink():
        raise ValueError("storage must not be a symlink")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return []


def _write(name, value):
    """Caller holds .lock; replace the complete file, never truncate it in place."""
    directory = _store()
    fd, tmp = tempfile.mkstemp(prefix="." + name, suffix=".tmp", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, directory / (name + ".json"))
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def classify(url):
    url = str(url).strip()
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    youtube = host == "youtu.be" or host == "youtube.com" or host.endswith(".youtube.com")
    parts = [p for p in parsed.path.split("/") if p]
    query = parse_qs(parsed.query)
    kind, ident = "video", hashlib.sha256(url.encode()).hexdigest()[:20]
    if youtube:
        if query.get("v"):
            ident = query["v"][0]
        elif host == "youtu.be" and parts:
            ident = parts[0]
        elif len(parts) > 1 and parts[0] in ("shorts", "embed", "live"):
            ident = parts[1]
        elif query.get("list"):
            kind, ident = "playlist", query["list"][0]
        elif parts and (parts[0].startswith("@") or
                        (parts[0] in ("channel", "c", "user") and len(parts) > 1)):
            kind, ident = "channel", parts[0] if parts[0].startswith("@") else parts[1]
    return {"kind": kind, "id": ident, "title": None, "url": url}


def _extract_info(url, flat=True):
    command = [sys.executable, "-m", "yt_dlp", "--dump-single-json", "--skip-download",
               "--no-warnings", "--quiet"]
    if flat:
        command.append("--flat-playlist")
    else:
        command.append("--no-playlist")
    result = subprocess.run(command + ["--", url], capture_output=True, text=True, check=True)
    return json.loads(result.stdout)


def _entries(info):
    """Flatten channel tab playlists while retaining yt-dlp's newest-first order."""
    entries = []
    for entry in info.get("entries") or []:
        if not entry:
            continue
        if "entries" in entry:
            entries.extend(_entries(entry))
        elif entry.get("_type") in ("url", "url_transparent") and classify(_entry_url(entry))["kind"] == "channel":
            entries.extend(_entries(_extract_info(_entry_url(entry))))
        else:
            entries.append(entry)
    # When tab metadata exposes dates, merge tabs by date rather than tab order.
    if entries and all(e.get("timestamp") or e.get("upload_date") for e in entries):
        def published(entry):
            if entry.get("timestamp"):
                return float(entry["timestamp"])
            return datetime.strptime(entry["upload_date"], "%Y%m%d").replace(tzinfo=timezone.utc).timestamp()
        entries.sort(key=published, reverse=True)
    return entries


def _entry_url(entry):
    url = entry.get("webpage_url") or entry.get("url")
    if url and url.startswith(("http://", "https://")):
        return url
    return "https://www.youtube.com/watch?v=" + str(entry.get("id") or url)


def playlist_entries(url):
    return [{"url": _entry_url(e), "title": e.get("title")}
            for e in _entries(_extract_info(url)) if e]


def _keywords(keywords):
    if keywords is None:
        return []
    if isinstance(keywords, str):
        keywords = keywords.split(",")
    return [str(word).strip() for word in keywords if str(word).strip()]


def _filters(entry, sub):
    duration = entry.get("duration")
    if duration is not None:
        try:
            duration = float(duration)
        except (TypeError, ValueError):
            duration = None
    if not sub["include_shorts"] and ("/shorts/" in _entry_url(entry) or
                                        (duration is not None and duration < 61)):
        return False
    if sub["min_minutes"] and (duration is None or duration < sub["min_minutes"] * 60):
        return False
    title = str(entry.get("title") or "").casefold()
    return not sub["keywords"] or any(word.casefold() in title for word in sub["keywords"])


def _new_job(url, folder, channel, title, jobs):
    ident = classify(url)["id"]
    for job in jobs:
        if job["video_id"] == ident and job["status"] in ("queued", "processing", "done"):
            return job, False
    now = _now()
    job = {"id": uuid.uuid4().hex, "video_id": ident, "url": url, "title": title,
           "folder": folder, "channel": channel, "status": "queued", "error": None,
           "out_dir": None, "created_at": now, "updated_at": now}
    jobs.append(job)
    return job, True


def enqueue(url, folder="Inbox", channel=None, title=None):
    if urlparse(str(url)).scheme not in ("http", "https"):
        raise ValueError("an http(s) URL is required")  # never hand a '-flag' to yt-dlp
    _path(folder).mkdir(parents=True, exist_ok=True)  # visible in the folder tree while queued
    if channel:
        _path(folder + "/" + _channel_name(channel))
    with _lock():
        jobs = _read("queue")
        job, added = _new_job(url, folder, channel, title, jobs)
        if added:
            _write("queue", jobs)
        return job


def add_subscription(url, folder="Inbox", backfill=10, include_shorts=False,
                     min_minutes=0, keywords=None):
    _path(folder).mkdir(parents=True, exist_ok=True)
    if classify(url)["kind"] != "channel":
        raise ValueError("a channel URL is required")
    if not isinstance(backfill, int) or backfill < -1:
        raise ValueError("backfill must be -1 or nonnegative")
    min_minutes = float(min_minutes)
    if not math.isfinite(min_minutes) or min_minutes < 0:
        raise ValueError("min_minutes must be nonnegative")
    # Resolve identity once; the uploads playlist orders all video types together.
    channel_url = re.sub(r"/(videos|shorts|streams|featured)/?$", "", url.rstrip("/"))
    info = _extract_info(channel_url)
    channel_id = info.get("channel_id") or info.get("id")
    if not channel_id or not channel_id.startswith("UC"):
        raise ValueError("yt-dlp did not resolve a YouTube channel id")
    handle = info.get("uploader_id") or info.get("channel_handle")
    if not handle or handle.startswith("UC"):
        source_id = classify(url)["id"]
        handle = source_id if source_id.startswith("@") else channel_id
    sub = {"id": uuid.uuid4().hex, "channel_id": channel_id, "handle": handle,
           "title": info.get("channel") or info.get("uploader") or info.get("title") or handle,
           "url": url, "folder": folder, "backfill": backfill,
           "include_shorts": bool(include_shorts), "min_minutes": min_minutes,
           "keywords": _keywords(keywords), "paused": False, "last_checked": None,
           "seen": [], "created_at": _now()}
    uploads_url = "https://www.youtube.com/playlist?list=UU" + channel_id[2:]
    uploads = _extract_info(uploads_url)
    selected = []
    for entry in _entries(uploads):
        if not entry:
            continue
        ident = str(entry.get("id") or classify(_entry_url(entry))["id"])
        sub["seen"].append(ident)
        if (backfill == -1 or len(selected) < backfill) and _filters(entry, sub):
            selected.append(entry)
    with _lock():
        subs, jobs = _read("subscriptions"), _read("queue")
        subs.append(sub)
        for entry in selected:
            _new_job(_entry_url(entry), folder, handle, entry.get("title"), jobs)
        _write("queue", jobs)
        _write("subscriptions", subs)
    return sub


def list_subscriptions():
    with _lock():
        return _read("subscriptions")


def update_subscription(sub_id, **fields):
    allowed = {"folder", "backfill", "include_shorts", "min_minutes", "keywords", "paused"}
    if set(fields) - allowed:
        raise ValueError("unsupported subscription field")
    if "folder" in fields:
        _path(fields["folder"])
    if "keywords" in fields:
        fields["keywords"] = _keywords(fields["keywords"])
    if "backfill" in fields and (not isinstance(fields["backfill"], int) or fields["backfill"] < -1):
        raise ValueError("invalid backfill")
    if "min_minutes" in fields:
        fields["min_minutes"] = float(fields["min_minutes"])
        if not math.isfinite(fields["min_minutes"]) or fields["min_minutes"] < 0:
            raise ValueError("invalid min_minutes")
    for name in ("paused", "include_shorts"):
        if name in fields and not isinstance(fields[name], bool):
            raise ValueError(name + " must be boolean")
    with _lock():
        subs = _read("subscriptions")
        sub = _find(subs, sub_id)
        sub.update(fields)
        _write("subscriptions", subs)
        return sub


def _find(items, ident):
    for item in items:
        if item["id"] == ident:
            return item
    raise ValueError("unknown id: " + ident)


def _remove(name, ident):
    with _lock():
        items = _read(name)
        _find(items, ident)
        _write(name, [item for item in items if item["id"] != ident])


def remove_subscription(sub_id):
    _remove("subscriptions", sub_id)


def list_queue(status=None):
    with _lock():
        jobs = _read("queue")
    return sorted((job for job in jobs if status is None or job["status"] == status),
                  key=lambda job: job["created_at"], reverse=True)


def retry(job_id):
    with _lock():
        jobs = _read("queue")
        job = _find(jobs, job_id)
        if job["status"] == "processing":
            raise ValueError("job is processing")
        job.update(status="queued", error=None, updated_at=_now())
        _write("queue", jobs)
        return job


def remove_job(job_id):
    _remove("queue", job_id)


def _rss(channel_id):
    with urlopen("https://www.youtube.com/feeds/videos.xml?channel_id=" + channel_id,
                 timeout=30) as response:
        tree = ET.fromstring(response.read())
    ns = {"a": "http://www.w3.org/2005/Atom", "yt": "http://www.youtube.com/xml/schemas/2015"}
    entries = []
    for node in tree.findall("a:entry", ns):
        ident = node.findtext("yt:videoId", namespaces=ns)
        if ident:
            link = node.find("a:link", ns)
            entries.append({"id": ident, "title": node.findtext("a:title", namespaces=ns),
                            "url": link.get("href") if link is not None else
                            "https://www.youtube.com/watch?v=" + ident})
    return entries


def check_all():
    checked = new = 0
    for snapshot in list_subscriptions():
        if snapshot["paused"]:
            continue
        entries = _rss(snapshot["channel_id"])
        candidates = []
        for entry in entries:
            if entry["id"] in snapshot["seen"]:
                continue
            # RSS has no duration. Ask yt-dlp only for unseen entries requiring it.
            if not snapshot["include_shorts"] or snapshot["min_minutes"]:
                details = _extract_info(_entry_url(entry), flat=False)
                entry = {**entry, **{k: v for k, v in details.items() if v is not None}}
            candidates.append(entry)
        with _lock():
            subs, jobs = _read("subscriptions"), _read("queue")
            current = next((s for s in subs if s["id"] == snapshot["id"]), None)
            if current is None or current["paused"]:
                continue
            for entry in candidates:
                ident = str(entry.get("id") or classify(_entry_url(entry))["id"])
                if ident in current["seen"]:
                    continue
                if _filters(entry, current):
                    _, added = _new_job(_entry_url(entry), current["folder"], current["handle"],
                                        entry.get("title"), jobs)
                    new += int(added)
                current["seen"].append(ident)
            current["last_checked"] = _now()
            _write("queue", jobs)
            _write("subscriptions", subs)
            checked += 1
    return {"checked": checked, "new": new}


def _channel_name(channel):
    if isinstance(channel, dict):
        channel = channel.get("handle") or channel.get("channel_id") or channel.get("title")
    return _unique_component(str(channel).lstrip("@"))


def _slug(text):
    safe = re.sub(r"[^A-Za-z0-9._-]+", "-", text)
    return re.sub(r"\.{2,}", "-", safe).strip("._-")[:80] or "video"


def _unique_component(text):
    safe = _slug(text)
    # Keep ordinary IDs/handles readable; never alias values changed by sanitizing.
    if safe != text:
        safe += "-" + hashlib.sha256(text.encode()).hexdigest()[:10]
    return safe


def _notify(job):
    try:
        text = str(job.get("title") or job["video_id"])
        script = "display notification " + json.dumps(text) + " with title " + json.dumps("crv: " + job["status"])
        subprocess.run(["osascript", "-e", script], capture_output=True, timeout=10)
    except Exception:
        pass


def work_once():
    with _lock(worker=True):
        with _lock():
            jobs = _read("queue")
            queued = [job for job in jobs if job["status"] == "queued"]
            if not queued:
                return None
            job = min(queued, key=lambda item: item["created_at"])
            topic = job["folder"]
            if job["channel"]:
                topic += "/" + _channel_name(job["channel"])
            slug = _slug(str(job["title"] or "video")) + "-" + _unique_component(job["video_id"])
            if job["out_dir"]:
                # Renames/moves update out_dir; retry must preserve that location.
                relative = Path(job["out_dir"]).relative_to(_root()).as_posix()
                out = _path(relative)
                slug = out.name
            else:
                out = _path(topic + "/" + slug)
            job.update(status="processing", error=None, out_dir=str(out), updated_at=_now())
            _write("queue", jobs)
        script = Path(__file__).resolve().parents[2] / "contrib" / "crv-watch"
        command = str(script) if script.is_file() else "crv-watch"
        env = dict(os.environ, CRV_WATCH_OUT=str(out))
        try:
            result = subprocess.run([command, job["url"], str(out.parent), slug],
                                    env=env, capture_output=True, text=True)
            status = "done" if result.returncode == 0 else "failed"
            error = None if status == "done" else (result.stderr or result.stdout or
                                                    f"exit status {result.returncode}")[-4000:]
        except Exception as exc:
            status, error = "failed", str(exc)[-4000:]
        with _lock():
            jobs = _read("queue")
            current = next((item for item in jobs if item["id"] == job["id"]), None)
            if current is not None:
                current.update(status=status, error=error, updated_at=_now())
                job = current
                _write("queue", jobs)
            else:
                job.update(status=status, error=error, updated_at=_now())
        _notify(job)
        return job


def _video(path):
    return (path / "transcript.json").is_file() or (path / "frames.json").is_file()


def list_folders():
    def visit(path):
        children, videos = [], 0
        for child in sorted(path.iterdir()):
            if child.name.startswith((".", "_")) or child.is_symlink() or not child.is_dir():
                continue
            if _video(child):
                videos += 1
            else:
                node = visit(child)
                children.append(node)
                videos += node["videos"]
        return {"name": path.name, "path": path.relative_to(_root()).as_posix(),
                "videos": videos, "subfolders": children}
    root = _root()
    if not root.exists():
        return []
    return [visit(path) for path in sorted(root.iterdir()) if path.is_dir() and
            not path.is_symlink() and not path.name.startswith((".", "_")) and not _video(path)]


def create_folder(name):
    with _lock():
        _path(name).mkdir(parents=True, exist_ok=True)


def _rewrite_paths(old, new, moved_video=False):
    old_path, new_path = str(_path(old)), str(_path(new))
    subs, jobs = _read("subscriptions"), _read("queue")
    for item in subs + jobs:
        folder = item["folder"]
        if not moved_video and (folder == old or folder.startswith(old + "/")):
            item["folder"] = new + folder[len(old):]
        out = item.get("out_dir")
        if out and (out == old_path or out.startswith(old_path + os.sep)):
            item["out_dir"] = new_path + out[len(old_path):]
            if moved_video:
                item["folder"] = str(Path(new).parent)
                item["channel"] = None
    _write("subscriptions", subs)
    _write("queue", jobs)


def rename_folder(old, new):
    # Serialize with workers so a running pipeline cannot recreate the old path.
    with _lock(worker=True), _lock():
        source, target = _path(old), _path(new)
        if target == source or target.is_relative_to(source) or source.is_relative_to(target):
            raise ValueError("overlapping folder paths")
        if not source.is_dir() or target.exists():
            raise ValueError("source missing or destination exists")
        target.parent.mkdir(parents=True, exist_ok=True)
        source.rename(target)
        _rewrite_paths(old, new)


def move_video(video_path_relative_to_root, dest_folder):
    with _lock(worker=True), _lock():
        source, dest = _path(video_path_relative_to_root), _path(dest_folder)
        if not source.is_dir() or not _video(source):
            raise ValueError("not a video folder")
        target = _path(dest_folder + "/" + source.name)
        if target.exists() or target.is_relative_to(source):
            raise ValueError("destination exists or overlaps source")
        dest.mkdir(parents=True, exist_ok=True)
        source.rename(target)
        _rewrite_paths(video_path_relative_to_root, target.relative_to(_root()).as_posix(), True)


def delete_folder(name):
    with _lock(worker=True), _lock():
        _path(name).rmdir()


def main(argv=None):
    parser = argparse.ArgumentParser(prog="crv-follow")
    commands = parser.add_subparsers(dest="command", required=True)
    add = commands.add_parser("add")
    add.add_argument("url")
    add.add_argument("--folder", default="Inbox")
    add.add_argument("--backfill", type=int, default=10)
    add.add_argument("--shorts", action="store_true")
    add.add_argument("--min-minutes", type=float, default=0)
    add.add_argument("--keywords")
    for name in ("list", "check", "run", "folders"):
        commands.add_parser(name)
    for name in ("pause", "resume", "remove", "retry"):
        commands.add_parser(name).add_argument("id")
    commands.add_parser("queue").add_argument("--status")
    commands.add_parser("work").add_argument("--loop", action="store_true")
    args = parser.parse_args(argv)
    try:
        kind = classify(args.url)["kind"] if args.command == "add" else None
        if kind == "channel":
            result = add_subscription(args.url, args.folder, args.backfill, args.shorts,
                                      args.min_minutes, args.keywords)
        elif kind == "playlist":
            result = [enqueue(e["url"], args.folder, title=e["title"])
                      for e in playlist_entries(args.url)]
        elif kind == "video":
            result = enqueue(args.url, args.folder)
        elif args.command == "list":
            result = list_subscriptions()
        elif args.command in ("pause", "resume"):
            result = update_subscription(args.id, paused=args.command == "pause")
        elif args.command == "remove":
            result = remove_subscription(args.id)
        elif args.command == "retry":
            result = retry(args.id)
        elif args.command == "queue":
            result = list_queue(args.status)
        elif args.command == "check":
            result = check_all()
        elif args.command == "folders":
            result = list_folders()
        else:
            result = {"check": check_all()} if args.command == "run" else {}
            jobs = []
            while True:
                job = work_once()
                if job is None:
                    break
                jobs.append(job)
                if args.command == "work" and not args.loop:
                    break
            result["jobs"] = jobs
        print(json.dumps(result, ensure_ascii=False, indent=2))
    except (ValueError, OSError, subprocess.SubprocessError) as exc:
        parser.exit(1, f"crv-follow: {exc}\n")


if __name__ == "__main__":
    main()
