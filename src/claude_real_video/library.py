"""crv-library: one local library for every video crv has watched.

Menu = topic > subfolder > video (the folder layout under crv-out IS the
organisation). Each video: player, speaker-labelled transcript (click a line
to seek), AI notes (notes.md), action steps (the notes.md section whose
heading says action/next steps/to-do), and your own notes (my-notes.md,
saved to disk next to the video). Search runs across every transcript and
every note at once, from the page or from the command line:

    python library.py                     # serve on http://127.0.0.1:8770
    python library.py --root DIR --port N --no-open   # another library folder / port
                                          # (same as CRV_LIBRARY_ROOT / CRV_LIBRARY_PORT)
    python library.py search "guarantee"  # same search, printed (for an AI)

Stdlib only, binds to 127.0.0.1, nothing leaves the machine.
"""
from __future__ import annotations

import http.server
import json
import math
import os
import re
import sys
import urllib.parse
import webbrowser
from pathlib import Path

ROOT = Path(os.environ.get("CRV_LIBRARY_ROOT",
            Path(__file__).resolve().parents[2] / "crv-out")).resolve()
PORT = int(os.environ.get("CRV_LIBRARY_PORT", "8770"))
VIDEO_NAMES = ("source.mp4", "source.webm", "source.mkv", "video.mp4")
UI_FILE = Path(__file__).resolve().parent / "library_ui" / "index.html"
ACTION_RE = re.compile(r"action|next step|to-?do|homework", re.I)
JSON_MAX = 64 * 1024  # follow API JSON bodies; my-notes stays on its own limit
_LOOPBACK = frozenset({"127.0.0.1", "localhost", "::1"})
_JOB_STATUS = frozenset({"queued", "processing", "done", "failed"})
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,200}$")
_SUB_FIELDS = ("folder", "backfill", "include_shorts", "min_minutes", "keywords", "paused", "title")


def pretty(slug: str) -> str:
    return re.sub(r"^\d+-", "", slug).replace("-", " ").replace("_", " ").strip().title()


def video_dir(vid: str) -> Path:
    """Resolve a video id to its folder; refuse anything outside ROOT."""
    p = (ROOT / vid).resolve()
    if p != ROOT and ROOT not in p.parents:
        raise ValueError("outside library")
    if not (p / "transcript.json").is_file():
        raise ValueError("not a video folder")
    return p


def _hidden_rel(rel: str) -> bool:
    """Folders whose name starts with '_' are system folders (_crv and friends)."""
    return any(part.startswith("_") for part in rel.split("/") if part not in ("", "."))


def _safe_rel(rel: str) -> str:
    """Relative path that stays inside ROOT. No absolutes, '..', or system folders."""
    if not isinstance(rel, str):
        raise ValueError("bad path")
    rel = rel.strip()
    if not rel or len(rel) > 500 or "\x00" in rel or "\\" in rel or rel.startswith("/"):
        raise ValueError("bad path")
    if re.match(r"^[A-Za-z]:", rel):
        raise ValueError("bad path")
    parts = rel.split("/")
    for part in parts:
        if part in ("", ".", "..") or part.startswith((".", "_")) or len(part) > 200:
            raise ValueError("bad path")
        if any(c in part for c in "\n\r\t"):
            raise ValueError("bad path")
    root = ROOT.resolve()
    dest = (root / rel).resolve()
    if root not in dest.parents:  # never the root itself, never a symlink that leaves it
        raise ValueError("outside library")
    try:
        dest.relative_to(root)
    except ValueError:
        raise ValueError("outside library") from None
    return rel


def _safe_id(raw: str) -> str:
    if not isinstance(raw, str):
        raise ValueError("bad id")
    raw = urllib.parse.unquote(raw)
    if not _ID_RE.fullmatch(raw) or ".." in raw:
        raise ValueError("bad id")
    return raw


def _as_int(value, name: str, lo: int, hi: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < lo or value > hi:
        raise ValueError(f"bad {name}")
    return value


def _clean_url(url) -> str:
    if not isinstance(url, str):
        raise ValueError("bad url")
    url = url.strip()
    if not url or len(url) > 2048 or any(c in url for c in "\x00\r\n\t "):
        raise ValueError("bad url")
    return url


def _follow():
    from . import follow  # lazy: the follow module is built separately and may be absent
    return follow


def _sub_updates(data: dict) -> dict:
    fields = {}
    for key in _SUB_FIELDS:
        if key not in data:
            continue
        val = data[key]
        if key == "folder":
            fields[key] = _safe_rel(val)
        elif key == "backfill":
            fields[key] = _as_int(val, key, -1, 100_000)
        elif key == "min_minutes":
            fields[key] = _as_int(val, key, 0, 100_000)
        elif key in ("include_shorts", "paused"):
            if not isinstance(val, bool):
                raise ValueError(f"bad {key}")
            fields[key] = val
        elif key == "keywords":
            if val is None:
                fields[key] = None
            elif (isinstance(val, list) and len(val) <= 50
                  and all(isinstance(x, str) and 0 < len(x) <= 200 and "\x00" not in x for x in val)):
                fields[key] = val
            else:
                raise ValueError("bad keywords")
        elif key == "title":
            if not isinstance(val, str) or len(val) > 500 or "\x00" in val:
                raise ValueError("bad title")
            fields[key] = val
    if not fields:
        raise ValueError("no fields")
    return fields


def _playlist_entries(follow, info: dict, url: str) -> list[dict]:
    """classify() does not list playlist videos. Use entries on that result when
    the backend includes them, otherwise follow.playlist_entries(url)."""
    if "entries" in info and info["entries"] is not None:
        raw = info["entries"]
    else:
        fn = getattr(follow, "playlist_entries", None)
        if not callable(fn):
            raise ValueError("playlist entries unavailable")
        raw = fn(url)
    if not isinstance(raw, list) or not raw:
        raise ValueError("playlist has no entries")
    if len(raw) > 5000:
        raise ValueError("playlist too large")
    out = []
    for entry in raw:
        if isinstance(entry, str):
            out.append({"url": _clean_url(entry), "title": None})
        elif isinstance(entry, dict) and isinstance(entry.get("url"), str):
            title = entry.get("title") if isinstance(entry.get("title"), str) else None
            out.append({"url": _clean_url(entry["url"]), "title": title})
        else:
            raise ValueError("bad playlist entry")
    return out


def _perform_add(data: dict) -> dict:
    url = _clean_url(data.get("url"))
    folder = _safe_rel(data.get("folder", "Inbox"))
    follow = _follow()
    info = follow.classify(url)
    if not isinstance(info, dict) or info.get("kind") not in ("video", "channel", "playlist"):
        raise ValueError("bad classify result")
    kind = info["kind"]
    if kind == "channel":
        kwargs: dict = {"folder": folder}
        if "backfill" in data:
            kwargs["backfill"] = _as_int(data["backfill"], "backfill", -1, 100_000)
        if "include_shorts" in data:
            if not isinstance(data["include_shorts"], bool):
                raise ValueError("bad include_shorts")
            kwargs["include_shorts"] = data["include_shorts"]
        if "min_minutes" in data:
            kwargs["min_minutes"] = _as_int(data["min_minutes"], "min_minutes", 0, 100_000)
        if "keywords" in data:
            kwargs.update(_sub_updates({"keywords": data["keywords"]}))
        return {"kind": "channel", "sub": follow.add_subscription(url, **kwargs)}
    if kind == "playlist":
        jobs = [follow.enqueue(entry["url"], folder=folder, title=entry.get("title"))
                for entry in _playlist_entries(follow, info, url)]
        return {"kind": "playlist", "jobs": jobs}
    title = info.get("title") if isinstance(info.get("title"), str) else None
    return {"kind": "video", "jobs": [follow.enqueue(url, folder=folder, title=title)]}


def read(p: Path) -> str:
    try:
        return p.read_text(encoding="utf-8")
    except OSError:
        return ""


def segments(d: Path) -> list[dict]:
    try:
        return json.loads(read(d / "transcript.json")).get("segments", [])
    except ValueError:
        return []


def action_steps(notes: str) -> str:
    """The notes.md section(s) whose heading names action items / next steps."""
    out, keep, level = [], False, 0
    for line in notes.splitlines():
        m = re.match(r"^(#+)\s+(.*)", line)
        if m:
            if keep and len(m.group(1)) <= level:
                keep = False
            if not keep and ACTION_RE.search(m.group(2)):
                keep, level = True, len(m.group(1))
        if keep:
            out.append(line)
    return "\n".join(out).strip()


def screens(d: Path) -> list:
    """screens.json: [{t, frame, title, note, links}] — what was shown on screen, for the Screens tab."""
    try:
        data = json.loads(read(d / "screens.json") or "[]")
    except ValueError:
        return []
    out = []  # agent-written file: keep only well-typed entries, coerce the rest
    for e in data if isinstance(data, list) else []:
        if not isinstance(e, dict) or not re.fullmatch(r"frame_\d+\.jpg", str(e.get("frame", ""))):
            continue
        t = e.get("t")
        try:  # 1e999 -> inf (breaks browser JSON), 400-digit ints -> OverflowError
            t = float(t) if isinstance(t, (int, float)) and not isinstance(t, bool) else 0.0
        except OverflowError:
            t = 0.0
        links = e.get("links") if isinstance(e.get("links"), list) else []
        out.append({"t": t if math.isfinite(t) and t >= 0 else 0.0,
                    "frame": e["frame"], "title": str(e.get("title") or ""), "note": str(e.get("note") or ""),
                    "links": [u for u in links if isinstance(u, str) and re.match(r"https?://", u)]})
    return out


def title_of(d: Path, notes: str) -> str:
    m = re.search(r"^#\s+(.+)$", notes, re.M)
    return m.group(1).strip() if m else pretty(d.name)


def library() -> list[dict]:
    items = []
    for tj in sorted(ROOT.rglob("transcript.json")):
        d = tj.parent
        rel = d.relative_to(ROOT).as_posix()
        if _hidden_rel(rel):
            continue
        parts = rel.split("/")
        notes = read(d / "notes.md")
        segs = segments(d)
        items.append({
            "id": rel,
            "topic": parts[0] if len(parts) > 1 else "(unsorted)",
            "sub": "/".join(parts[1:-1]),
            "title": title_of(d, notes),
            "duration": segs[-1]["end"] if segs else 0,
            "speakers": sorted({s["speaker"] for s in segs if s.get("speaker")}),
            "has_video": any((d / n).is_file() for n in VIDEO_NAMES),
            "has_notes": bool(notes.strip()),
            "has_actions": bool(action_steps(notes)),
            "has_my_notes": bool(read(d / "my-notes.md").strip()),
            "has_screens": bool(screens(d)),
        })
    return items


STOP = set("that this with from have they them their there what when where which about would could should into your just like will been were more than then some also very only over make made does doing because really thing things going want need".split())
WORD_RE = re.compile(r"[a-z0-9']{4,}")


def _words(text: str) -> set[str]:
    return {w for w in WORD_RE.findall(text.lower()) if w not in STOP}


def best_moment(line: str, segs: list[dict], window: int = 3) -> float | None:
    """Start time of the transcript stretch a note line summarises: the window of
    `window` consecutive segments sharing the most distinct words with the line.
    None for headings, or when under max(3, 30% of the line's words) overlap."""
    # ponytail: plain word overlap, swap for embeddings if notes get too abstract to match
    if line.lstrip().startswith("#"):
        return None
    want = _words(re.sub(r"\[@?[\d:]+\]", "", line))
    if len(want) < 3:
        return None
    seg_words = [_words(s.get("text", "")) for s in segs]
    best, best_t = max(3, int(len(want) * 0.3)) - 1, None
    for i in range(len(segs)):
        got = len(want & set().union(*seg_words[i:i + window]))
        if got > best:  # start at the window's first segment that actually shares a word
            j = next(k for k in range(i, i + window) if want & seg_words[k])
            best, best_t = got, segs[j].get("start", 0)
    return best_t


def chapters(d: Path) -> list[dict]:
    try:
        return json.loads(read(d / "chapters.json")).get("chapters", [])
    except ValueError:
        return []


def fetch_chapters(d: Path, url: str) -> int:
    """Save the video's YouTube chapters (yt-dlp metadata, no download) to chapters.json."""
    import subprocess
    meta = json.loads(subprocess.run(["yt-dlp", "--skip-download", "-J", url], capture_output=True,
                                     text=True, encoding="utf-8", check=True).stdout)
    chs = [{"t": int(c["start_time"]), "title": c["title"]} for c in meta.get("chapters") or []]
    (d / "chapters.json").write_text(json.dumps({"url": url, "chapters": chs}, indent=1), encoding="utf-8")
    return len(chs)


def search(q: str, topic: str = "", limit: int = 400) -> list[dict]:
    """Every word must appear in the line (any order, case-insensitive)."""
    words = [w for w in q.lower().split() if w]
    if not words:
        return []
    hits = []
    for v in library():
        if topic and v["topic"] != topic:
            continue
        d = ROOT / v["id"]
        segs = segments(d)
        for s in segs:
            t = s.get("text", "")
            if all(w in t.lower() for w in words):
                hits.append({"id": v["id"], "title": v["title"], "kind": "transcript",
                             "t": s.get("start", 0), "speaker": s.get("speaker", ""), "text": t.strip()})
        for kind, fname in (("ai notes", "notes.md"), ("my notes", "my-notes.md")):
            for line in read(d / fname).splitlines():
                if all(w in line.lower() for w in words):
                    m = re.search(r"@(\d+):(\d\d)(?::(\d\d))?", line)
                    t, approx = None, False
                    if m:
                        a, b, c = m.groups()
                        t = int(a) * 3600 + int(b) * 60 + int(c) if c else int(a) * 60 + int(b)
                    elif kind == "ai notes":  # no stamp: point at the transcript part it summarises
                        t = best_moment(line, segs)
                        approx = t is not None
                    hits.append({"id": v["id"], "title": v["title"], "kind": kind,
                                 "t": t, "approx": approx, "speaker": "", "text": line.strip()})
        for sc in screens(d):
            text = " — ".join(x for x in (sc["title"], sc["note"]) if x)
            links = " ".join(sc["links"])
            if all(w in (text + " " + links).lower() for w in words):
                if not all(w in text.lower() for w in words):  # matched via a link: show it
                    text = f"{text} · {links}"
                hits.append({"id": v["id"], "title": v["title"], "kind": "screens",
                             "t": sc.get("t"), "approx": False, "speaker": "", "text": text})
        if len(hits) >= limit:
            break
    return hits[:limit]


class H(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, body: bytes, ctype: str, code: int = 200):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")  # page + notes always fresh
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code=200):
        self._send(json.dumps(obj).encode(), "application/json", code)

    def _own_netlocs(self) -> set[str]:
        host, port = self.server.server_address[:2]
        port = int(port)
        names = {host}
        if host in _LOOPBACK:  # the page is opened as 127.0.0.1 or localhost
            names = set(_LOOPBACK)
        out = set()
        for name in names:
            shown = f"[{name}]" if ":" in name and not str(name).startswith("[") else name
            out.add(f"{shown}:{port}".lower())
        return out

    def _same_origin(self) -> bool:
        """Host, and Origin when the browser sent one, must be this server."""
        hosts = self.headers.get_all("Host") or []
        if len(hosts) != 1 or hosts[0].strip().lower() not in self._own_netlocs():
            return False
        origins = self.headers.get_all("Origin")
        if not origins:
            return True
        if len(origins) != 1:
            return False
        parsed = urllib.parse.urlparse(origins[0].strip())
        if (parsed.scheme or "").lower() != "http" or parsed.username or parsed.password or parsed.path not in ("", "/"):
            return False
        return parsed.netloc.lower() in self._own_netlocs()

    def _guard(self) -> bool:
        if self._same_origin():
            return True
        self.close_connection = True
        self._json({"error": "forbidden"}, 403)
        return False

    def _read_json(self, required: bool = True):
        try:
            n = int(self.headers.get("Content-Length", "0"))
        except (TypeError, ValueError):
            raise ValueError("bad length") from None
        if n < 0:
            raise ValueError("bad length")
        if n > JSON_MAX:
            self.close_connection = True
            self._json({"error": "too large"}, 413)
            return None
        raw = self.rfile.read(n) if n else b""
        if not raw.strip():
            if required:
                raise ValueError("empty body")
            return {}
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            raise ValueError("invalid json") from None
        if not isinstance(data, dict):
            raise ValueError("json object required")
        return data

    def _guarded_follow(self, fn):
        try:
            fn()
        except ImportError:
            self._json({"error": "follow unavailable"}, 503)
        except FileNotFoundError as e:
            self._json({"error": str(e) or "not found"}, 404)
        except (ValueError, KeyError) as e:
            self._json({"error": str(e) or "bad request"}, 400)
        except OSError as e:
            self._json({"error": str(e) or "error"}, 400)

    def _get_queue(self, q: dict):
        status = q.get("status") or None
        if status is not None and status not in _JOB_STATUS:
            raise ValueError("bad status")
        jobs = _follow().list_queue(status)
        if not isinstance(jobs, list):
            raise ValueError("bad queue")
        self._json({"jobs": jobs})

    def _get_subs(self):
        subs = _follow().list_subscriptions()
        if not isinstance(subs, list):
            raise ValueError("bad subscriptions")
        self._json({"subscriptions": subs})

    def _get_folders(self):
        folders = _follow().list_folders()
        if not isinstance(folders, list):
            raise ValueError("bad folders")
        self._json({"folders": folders})

    def _post_add(self):
        body = self._read_json(True)
        if body is None:
            return
        self._json(_perform_add(body))

    def _post_check(self):
        body = self._read_json(False)
        if body is None:
            return
        result = _follow().check_all()
        if not isinstance(result, dict):
            raise ValueError("bad check result")
        self._json(result)

    def _post_folder(self):
        body = self._read_json(True)
        if body is None:
            return
        name = _safe_rel(body.get("name"))
        _follow().create_folder(name)
        self._json({"ok": True, "name": name})

    def _patch_folder(self):
        body = self._read_json(True)
        if body is None:
            return
        old, new = _safe_rel(body.get("old")), _safe_rel(body.get("new"))
        _follow().rename_folder(old, new)
        self._json({"ok": True, "old": old, "new": new})

    def _post_move(self):
        body = self._read_json(True)
        if body is None:
            return
        video, folder = _safe_rel(body.get("video")), _safe_rel(body.get("folder"))
        moved = _follow().move_video(video, folder)
        out = {"ok": True, "video": video, "folder": folder}
        if isinstance(moved, str):
            try:
                out["id"] = _safe_rel(moved)
            except ValueError:
                pass
        self._json(out)

    def _post_retry(self, raw_id: str):
        body = self._read_json(False)
        if body is None:
            return
        job = _follow().retry(_safe_id(raw_id))
        self._json(job if isinstance(job, dict) else {"ok": True})

    def _patch_sub(self, raw_id: str):
        body = self._read_json(True)
        if body is None:
            return
        sub = _follow().update_subscription(_safe_id(raw_id), **_sub_updates(body))
        self._json(sub if isinstance(sub, dict) else {"ok": True})

    def _delete_job(self, raw_id: str):
        if self._read_json(False) is None:
            return
        _follow().remove_job(_safe_id(raw_id))
        self._json({"ok": True})

    def _delete_sub(self, raw_id: str):
        if self._read_json(False) is None:
            return
        _follow().remove_subscription(_safe_id(raw_id))
        self._json({"ok": True})

    def _delete_folder(self, raw_name: str):
        if self._read_json(False) is None:
            return
        name = _safe_rel(urllib.parse.unquote(raw_name))
        _follow().delete_folder(name)
        self._json({"ok": True})

    def do_GET(self):
        if not self._guard():
            return
        u = urllib.parse.urlparse(self.path)
        q = {k: v[0] for k, v in urllib.parse.parse_qs(u.query).items()}
        try:
            if u.path in ("/", "/index.html"):
                return self._send(read(UI_FILE).encode() or CLASSIC_PAGE.encode(), "text/html; charset=utf-8")
            if u.path == "/classic":  # the previous single-theme UI, kept until Bala retires it
                return self._send(CLASSIC_PAGE.encode(), "text/html; charset=utf-8")
            if u.path == "/api/library":
                return self._json({"root": str(ROOT), "videos": library()})
            if u.path == "/api/queue":
                return self._guarded_follow(lambda: self._get_queue(q))
            if u.path == "/api/subscriptions":
                return self._guarded_follow(self._get_subs)
            if u.path == "/api/folders":
                return self._guarded_follow(self._get_folders)
            if u.path == "/api/search":
                return self._json(search(q.get("q", ""), q.get("topic", "")))
            if u.path == "/api/video":
                d = video_dir(q["id"])
                notes = read(d / "notes.md")
                return self._json({"segments": segments(d), "notes": notes, "chapters": chapters(d),
                                   "actions": action_steps(notes), "my_notes": read(d / "my-notes.md"),
                                   "screens": screens(d)})
            if u.path == "/media":
                return self._media(video_dir(q["id"]))
            if u.path == "/still":  # a Screens-tab screenshot: hires/ first, crv's 640px frames/ fallback
                d, name = video_dir(q["id"]), q["f"]
                if not re.fullmatch(r"frame_\d+\.jpg", name):
                    raise ValueError("bad frame name")
                f = next((p for p in (d / "hires" / name, d / "frames" / name) if p.is_file()), None)
                if not f:
                    return self._json({"error": "no such frame"}, 404)
                return self._send(f.read_bytes(), "image/jpeg")
        except (ValueError, KeyError) as e:
            return self._json({"error": str(e)}, 400)
        self._json({"error": "not found"}, 404)

    def _media(self, d: Path):
        f = next((d / n for n in VIDEO_NAMES if (d / n).is_file()), None)
        if not f:
            return self._json({"error": "no video file"}, 404)
        size = f.stat().st_size
        start, end = 0, size - 1
        m = re.match(r"bytes=(\d*)-(\d*)", self.headers.get("Range", ""))
        if m:
            if m.group(1):
                start = int(m.group(1))
                end = int(m.group(2)) if m.group(2) else end
            elif m.group(2):  # suffix range: last N bytes
                start = max(0, size - int(m.group(2)))
            end = min(end, size - 1)
            if start > end:
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{size}")
                return self.end_headers()
        self.send_response(206 if m else 200)
        self.send_header("Content-Type", "video/mp4" if f.suffix == ".mp4" else "video/" + f.suffix[1:])
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(end - start + 1))
        if m:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()
        with f.open("rb") as fh:
            fh.seek(start)
            left = end - start + 1
            try:
                while left > 0:
                    chunk = fh.read(min(1 << 20, left))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    left -= len(chunk)
            except (ConnectionError, OSError):
                pass  # browser cancelled the range (normal while seeking)

    def _post_notes(self, u):
        q = {k: v[0] for k, v in urllib.parse.parse_qs(u.query).items()}
        try:
            d = video_dir(q["id"])
        except (ValueError, KeyError) as e:
            return self._json({"error": str(e)}, 400)
        n = int(self.headers.get("Content-Length", "0"))
        if n > 5_000_000:
            return self._json({"error": "too large"}, 413)
        text = self.rfile.read(n).decode("utf-8")
        tmp = d / "my-notes.md.tmp"
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, d / "my-notes.md")  # atomic: a crash never leaves half a file
        self._json({"saved": True, "bytes": len(text.encode())})

    def do_POST(self):
        if not self._guard():
            return
        u = urllib.parse.urlparse(self.path)
        if u.path == "/api/my-notes":
            return self._post_notes(u)
        if u.path == "/api/add":
            return self._guarded_follow(self._post_add)
        if u.path == "/api/check":
            return self._guarded_follow(self._post_check)
        if u.path == "/api/folders":
            return self._guarded_follow(self._post_folder)
        if u.path == "/api/move":
            return self._guarded_follow(self._post_move)
        m = re.fullmatch(r"/api/queue/([^/]+)/retry", u.path)
        if m:
            return self._guarded_follow(lambda: self._post_retry(m.group(1)))
        self._json({"error": "not found"}, 404)

    def do_PATCH(self):
        if not self._guard():
            return
        u = urllib.parse.urlparse(self.path)
        if u.path == "/api/folders":
            return self._guarded_follow(self._patch_folder)
        m = re.fullmatch(r"/api/subscriptions/([^/]+)", u.path)
        if m:
            return self._guarded_follow(lambda: self._patch_sub(m.group(1)))
        self._json({"error": "not found"}, 404)

    def do_DELETE(self):
        if not self._guard():
            return
        path = urllib.parse.urlparse(self.path).path
        m = re.fullmatch(r"/api/queue/([^/]+)", path)
        if m:
            return self._guarded_follow(lambda: self._delete_job(m.group(1)))
        m = re.fullmatch(r"/api/subscriptions/([^/]+)", path)
        if m:
            return self._guarded_follow(lambda: self._delete_sub(m.group(1)))
        if path.startswith("/api/folders/"):
            return self._guarded_follow(lambda: self._delete_folder(path[len("/api/folders/"):]))
        self._json({"error": "not found"}, 404)


CLASSIC_PAGE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Video Library</title>
<style>
:root{--bg:#f6f5f2;--panel:#fff;--ink:#1d1d1b;--mute:#6b6a64;--line:#e3e1db;--acc:#2f6fdf;--hi:#fff3c4;--sel:#eaf1fd}
@media (prefers-color-scheme:dark){:root{--bg:#141414;--panel:#1d1d1d;--ink:#e9e7e2;--mute:#9a978f;--line:#2e2e2c;--acc:#7aa7ff;--hi:#4a3f12;--sel:#1f2a3d}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:14px/1.5 system-ui,Segoe UI,sans-serif;height:100vh;display:flex}
button,input,select,textarea{font:inherit;color:inherit}
#side{width:320px;min-width:260px;border-right:1px solid var(--line);background:var(--panel);display:flex;flex-direction:column}
#side header{padding:12px;border-bottom:1px solid var(--line);display:flex;flex-direction:column;gap:8px}
#side h1{font-size:16px;margin:0}
input[type=search],select,textarea{width:100%;padding:7px 9px;border:1px solid var(--line);border-radius:6px;background:var(--bg)}
.chips{display:flex;flex-wrap:wrap;gap:6px}.chips label{font-size:12px;color:var(--mute);display:flex;gap:4px;align-items:center}
#tree{overflow:auto;flex:1;padding:8px 8px 16px}
details>summary{cursor:pointer;list-style:none;display:flex;align-items:center;gap:8px;user-select:none}
details>summary::-webkit-details-marker{display:none}
details>summary::before{content:"";width:6px;height:6px;border-right:2px solid var(--mute);border-bottom:2px solid var(--mute);transform:rotate(-45deg);transition:transform .15s;flex:none}
details[open]>summary::before{transform:rotate(45deg)}
.topic{margin-top:6px}
.topic>summary{padding:10px 8px 6px;font-size:11px;font-weight:700;letter-spacing:.08em;text-transform:uppercase;color:var(--mute)}
.subf>summary{padding:6px 8px;margin:2px 0;border-radius:6px;font-weight:600;font-size:13px}
.subf>summary:hover{background:var(--sel)}
.subf .list{padding:2px 0 6px 14px;display:flex;flex-direction:column;gap:3px}
.count{margin-left:auto;font-size:11px;font-weight:600;color:var(--mute);background:var(--bg);border:1px solid var(--line);border-radius:999px;padding:0 7px;letter-spacing:0;text-transform:none}
.vid{display:flex;gap:10px;align-items:flex-start;padding:7px 8px;border-radius:8px;cursor:pointer;border:1px solid transparent}
.vid:hover{background:var(--sel)}.vid.on{background:var(--sel);border-color:var(--acc)}
.badge{flex:none;min-width:30px;height:24px;padding:0 5px;border-radius:6px;background:var(--bg);border:1px solid var(--line);font-size:11px;font-weight:700;display:flex;align-items:center;justify-content:center;font-variant-numeric:tabular-nums;color:var(--mute)}
.vid.on .badge{background:var(--acc);border-color:var(--acc);color:#fff}
.vt{min-width:0;flex:1}
.vt b{display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden;font-weight:500;font-size:13px;line-height:1.35}
.pills{display:flex;flex-wrap:wrap;gap:4px;margin-top:4px}
.pill{font-size:10.5px;line-height:16px;padding:0 6px;border-radius:999px;background:var(--bg);border:1px solid var(--line);color:var(--mute);white-space:nowrap}
.pill.ok{color:#2e8b57;border-color:#2e8b5755}.pill.me{color:var(--acc);border-color:var(--acc)}
#main{flex:1;display:flex;flex-direction:column;min-width:0}
#top{padding:10px 16px;border-bottom:1px solid var(--line);background:var(--panel)}
#top h2{margin:0;font-size:16px}#crumb{color:var(--mute);font-size:12px}
#body{flex:1;display:flex;min-height:0}
#left{flex:1.1;display:flex;flex-direction:column;min-width:0;padding:12px}
video{width:100%;max-height:52vh;background:#000;border-radius:6px}
#right{flex:1;display:flex;flex-direction:column;border-left:1px solid var(--line);min-width:0;background:var(--panel)}
.tabs{display:flex;border-bottom:1px solid var(--line)}.tabs button{flex:1;padding:9px;border:0;background:none;cursor:pointer;color:var(--mute);border-bottom:2px solid transparent}
.tabs button.on{color:var(--ink);border-bottom-color:var(--acc);font-weight:600}
.pane{flex:1;overflow:auto;padding:12px 14px;display:none}.pane.on{display:block}
.seg{padding:4px 6px;border-radius:4px;cursor:pointer;display:flex;gap:8px}.seg:hover{background:var(--sel)}
.seg.now{background:var(--hi)}.seg .ts{color:var(--acc);font-variant-numeric:tabular-nums;min-width:52px}
.seg .sp{font-weight:600;min-width:60px}.seg.hidden{display:none}
mark{background:var(--hi);color:inherit}
.tools{display:flex;gap:8px;align-items:center;margin-bottom:8px;flex-wrap:wrap}.tools input[type=search]{flex:1;min-width:140px}
.md h1{font-size:18px}.md h2{font-size:16px;margin-top:18px}.md h3{font-size:14px}.md li{margin:2px 0}
a.t{color:var(--acc);cursor:pointer;text-decoration:none;font-variant-numeric:tabular-nums}
#results .r{padding:6px 8px;border-bottom:1px solid var(--line);cursor:pointer}#results .r:hover{background:var(--sel)}
#results .r small{color:var(--mute)}
#mine textarea{height:40vh;resize:vertical;font-family:ui-monospace,Consolas,monospace;font-size:13px}
#saved{color:var(--mute);font-size:12px}.empty{color:var(--mute);padding:20px}
.btn{padding:5px 10px;border:1px solid var(--line);border-radius:6px;background:var(--bg);cursor:pointer}
@media (max-width:900px){body{flex-direction:column;height:auto}#side{width:100%;max-height:45vh}#body{flex-direction:column}#right{border-left:0}}
</style></head><body>
<aside id="side"><header>
<h1>Video Library</h1>
<input type="search" id="q" placeholder="Search every transcript + note (Enter)">
<select id="topic"><option value="">All topics</option></select>
<input type="search" id="tf" placeholder="Filter menu by title">
<div class="chips">
<label><input type="checkbox" id="fSp"> speakers</label>
<label><input type="checkbox" id="fAct"> action steps</label>
<label><input type="checkbox" id="fMine"> my notes</label>
</div></header>
<nav id="tree"></nav></aside>
<main id="main">
<div id="top"><div id="crumb"></div><h2 id="ttl">Pick a video from the menu, or search.</h2></div>
<div id="body">
<section id="left"><video id="v" controls preload="metadata"></video>
<div id="results" style="overflow:auto;flex:1;margin-top:10px"></div></section>
<section id="right">
<div class="tabs"><button data-p="tr" class="on">Transcript</button><button data-p="ai">AI notes</button><button data-p="act">Action steps</button><button data-p="mine">My notes</button></div>
<div class="pane on" id="tr"><div class="tools"><input type="search" id="tq" placeholder="Find in this transcript"><select id="spk"><option value="">All speakers</option></select><label><input type="checkbox" id="follow" checked> follow</label></div><div id="segs"></div></div>
<div class="pane md" id="ai"></div>
<div class="pane md" id="act"></div>
<div class="pane" id="mine"><div class="tools"><button class="btn" id="stamp">+ timestamp</button><span id="saved"></span></div>
<textarea id="mn" placeholder="Your notes. Saved automatically to my-notes.md next to the video. [@12:34] links jump the video."></textarea>
<div class="md" id="mnp"></div></div>
</section></div></main>
<script>
let LIB=[],cur=null,SEG=[],saveT=null;
const $=s=>document.querySelector(s),esc=s=>String(s).replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const fmt=t=>{t=Math.floor(t||0);const h=Math.floor(t/3600),m=Math.floor(t%3600/60),s=t%60;return (h?h+':'+String(m).padStart(2,'0'):m)+':'+String(s).padStart(2,'0')};
const pretty=s=>s.replace(/^\d+-/,'').replace(/[-_]/g,' ').replace(/\b\w/g,c=>c.toUpperCase());
function hl(text,q){let h=esc(text);for(const w of (q||'').toLowerCase().split(/\s+/).filter(Boolean)){h=h.replace(new RegExp('('+w.replace(/[.*+?^${}()|[\]\\]/g,'\\$&')+')','ig'),'<mark>$1</mark>')}return h}
function md(src){ // small markdown: headings, bullets, bold, code, clickable @mm:ss
  const inl=s=>esc(s).replace(/\*\*(.+?)\*\*/g,'<b>$1</b>').replace(/`([^`]+)`/g,'<code>$1</code>')
    .replace(/@?\b(\d{1,3}):(\d\d)(?::(\d\d))?\b/g,(m,a,b,c)=>`<a class="t" data-t="${c?(+a*3600+ +b*60+ +c):(+a*60+ +b)}">${m}</a>`);
  let out='',list=0;
  for(const l of src.split('\n')){const h=l.match(/^(#{1,4})\s+(.*)/),li=l.match(/^(\s*)[-*]\s+(.*)/);
    if(li){const d=Math.floor(li[1].length/2)+1;while(list<d){out+='<ul>';list++}while(list>d){out+='</ul>';list--}out+='<li>'+inl(li[2]);continue}
    while(list){out+='</ul>';list--}
    if(h)out+=`<h${h[1].length}>${inl(h[2])}</h${h[1].length}>`;else if(l.trim())out+='<p>'+inl(l)+'</p>'}
  while(list){out+='</ul>';list--}return out}
function seek(t){const v=$('#v');if(t==null)return;v.currentTime=t;v.play().catch(()=>{})}
document.addEventListener('click',e=>{const a=e.target.closest('a.t');if(a)seek(+a.dataset.t)});

async function load(){
  LIB=(await (await fetch('/api/library')).json()).videos;
  const tops=[...new Set(LIB.map(v=>v.topic))];
  $('#topic').innerHTML='<option value="">All topics</option>'+tops.map(t=>`<option value="${esc(t)}">${esc(pretty(t))}</option>`).join('');
  try{const st=JSON.parse(localStorage.getItem('crvlib')||'{}');for(const k of ['topic','tf'])if(st[k])$('#'+k).value=st[k]}catch(e){}
  tree();route()}
function tree(){
  const tp=$('#topic').value,tf=$('#tf').value.toLowerCase();
  try{localStorage.setItem('crvlib',JSON.stringify({topic:tp,tf}))}catch(e){}
  const vs=LIB.filter(v=>(!tp||v.topic==tp)&&(!tf||(v.title+' '+v.id).toLowerCase().includes(tf))
    &&(!$('#fSp').checked||v.speakers.length)&&(!$('#fAct').checked||v.has_actions)&&(!$('#fMine').checked||v.has_my_notes));
  const g={};for(const v of vs)((g[v.topic]??={})[v.sub||'']??=[]).push(v);
  const item=v=>{const leaf=v.id.split('/').pop(),m=leaf.match(/^(?:call-)?(?:m(\d+)-)?(\d+)/i);
    const badge=m?(m[1]?m[1]+'.':'')+m[2]:'•';
    const title=v.title.replace(/^[\w-]{1,12}:\s*/,'').replace(/\s*\([^)]*\)\s*$/,'');
    const sp=v.speakers.length?(v.speakers.length<=3&&!v.speakers.some(s=>/^SPEAKER_/.test(s))?v.speakers.join(' & '):v.speakers.length+' speakers'):'';
    return `<a class="vid${cur&&cur.id==v.id?' on':''}" data-id="${esc(v.id)}" title="${esc(v.title)}"><span class="badge">${esc(badge)}</span><span class="vt"><b>${esc(title)}</b><span class="pills"><span class="pill">${fmt(v.duration)}</span>${sp?`<span class="pill">${esc(sp)}</span>`:''}${v.has_actions?'<span class="pill ok">✓ actions</span>':''}${v.has_my_notes?'<span class="pill me">✎ my notes</span>':''}</span></span></a>`};
  let h='';
  for(const [t,subs] of Object.entries(g)){const n=Object.values(subs).flat().length;
    h+=`<details class="topic" open><summary>${esc(pretty(t))}<span class="count">${n}</span></summary>`;
    for(const [s,list] of Object.entries(subs)){
      const items=`<div class="list">${list.map(item).join('')}</div>`;
      h+=s?`<details class="subf" open><summary>${esc(s.split('/').map(pretty).join(' / '))}<span class="count">${list.length}</span></summary>${items}</details>`:items}
    h+='</details>'}
  $('#tree').innerHTML=h||'<div class="empty">No videos match.</div>'}
['#topic','#tf','#fSp','#fAct','#fMine'].forEach(s=>$(s).addEventListener('input',tree));
$('#tree').addEventListener('click',e=>{const a=e.target.closest('.vid');if(a)location.hash='v='+encodeURIComponent(a.dataset.id)});

async function openVid(id,t){
  if(!cur||cur.id!==id){
    const v=LIB.find(x=>x.id==id);if(!v)return;
    const d=await (await fetch('/api/video?id='+encodeURIComponent(id))).json();
    cur={...v,...d};SEG=d.segments;
    $('#ttl').textContent=v.title;$('#crumb').textContent=[pretty(v.topic),...(v.sub?v.sub.split('/').map(pretty):[])].join(' › ');
    $('#v').src=v.has_video?'/media?id='+encodeURIComponent(id):'';
    $('#spk').innerHTML='<option value="">All speakers</option>'+v.speakers.map(s=>`<option>${esc(s)}</option>`).join('');
    $('#segs').innerHTML=SEG.map((s,i)=>`<div class="seg" data-i="${i}"><span class="ts">${fmt(s.start)}</span>${s.speaker?`<span class="sp">${esc(s.speaker)}</span>`:''}<span class="tx">${esc(s.text)}</span></div>`).join('')||'<div class="empty">No transcript.</div>';
    $('#ai').innerHTML=d.notes?md(d.notes):'<div class="empty">No AI notes yet for this video.</div>';
    $('#act').innerHTML=d.actions?md(d.actions):'<div class="empty">No action steps yet. Ask Claude to add an "## Action steps" section to notes.md.</div>';
    $('#mn').value=d.my_notes;$('#mnp').innerHTML=md(d.my_notes);$('#saved').textContent='';
    if(!v.has_video)$('#results').innerHTML='<div class="empty">Video file not on disk; transcript and notes still work.</div>';
    tree();filterSegs()}
  if(t!=null){const v=$('#v');if(v.readyState)seek(t);else v.addEventListener('loadedmetadata',()=>seek(t),{once:true})}}
function route(){const p=new URLSearchParams(location.hash.slice(1));if(p.get('v'))openVid(p.get('v'),p.get('t')?+p.get('t'):null)}
addEventListener('hashchange',route);

$('#segs').addEventListener('click',e=>{const s=e.target.closest('.seg');if(s)seek(SEG[+s.dataset.i].start)});
function filterSegs(){const q=$('#tq').value.toLowerCase(),sp=$('#spk').value,words=q.split(/\s+/).filter(Boolean);
  document.querySelectorAll('#segs .seg').forEach(el=>{const s=SEG[+el.dataset.i];
    const ok=(!sp||s.speaker==sp)&&words.every(w=>s.text.toLowerCase().includes(w));el.classList.toggle('hidden',!ok);
    el.querySelector('.tx').innerHTML=hl(s.text,q)})}
$('#tq').addEventListener('input',filterSegs);$('#spk').addEventListener('input',filterSegs);
let lastNow=-1;
$('#v').addEventListener('timeupdate',()=>{const t=$('#v').currentTime;let i=-1;
  for(let k=0;k<SEG.length;k++){if(SEG[k].start<=t)i=k;else break}
  if(i===lastNow)return;document.querySelector('.seg.now')?.classList.remove('now');lastNow=i;
  const el=document.querySelector(`.seg[data-i="${i}"]`);if(el){el.classList.add('now');if($('#follow').checked&&$('#tr').classList.contains('on'))el.scrollIntoView({block:'center',behavior:'smooth'})}});

document.querySelectorAll('.tabs button').forEach(b=>b.onclick=()=>{document.querySelectorAll('.tabs button,.pane').forEach(x=>x.classList.remove('on'));b.classList.add('on');$('#'+b.dataset.p).classList.add('on')});

$('#mn').addEventListener('input',()=>{$('#mnp').innerHTML=md($('#mn').value);$('#saved').textContent='unsaved…';clearTimeout(saveT);saveT=setTimeout(save,700)});
async function save(){if(!cur)return;const r=await fetch('/api/my-notes?id='+encodeURIComponent(cur.id),{method:'POST',body:$('#mn').value});
  $('#saved').textContent=r.ok?'saved to my-notes.md':'SAVE FAILED';const v=LIB.find(x=>x.id==cur.id);if(v)v.has_my_notes=!!$('#mn').value.trim()}
$('#stamp').onclick=()=>{const ta=$('#mn'),ins='[@'+fmt($('#v').currentTime)+'] ',p=ta.selectionStart;
  ta.value=ta.value.slice(0,p)+ins+ta.value.slice(ta.selectionEnd);ta.focus();ta.selectionStart=ta.selectionEnd=p+ins.length;ta.dispatchEvent(new Event('input'))};

$('#q').addEventListener('keydown',async e=>{if(e.key!=='Enter')return;const q=$('#q').value.trim();if(!q){$('#results').innerHTML='';return}
  const hits=await (await fetch('/api/search?q='+encodeURIComponent(q)+'&topic='+encodeURIComponent($('#topic').value))).json();
  $('#results').innerHTML=`<div class="empty" style="padding:4px 8px">${hits.length} result(s) for “${esc(q)}”${hits.length>=400?' (first 400)':''}</div>`+
    hits.map(h=>`<div class="r" data-id="${esc(h.id)}" data-t="${h.t??''}"><small>${esc(h.title)} · ${h.kind}${h.t!=null?' · '+fmt(h.t):''}${h.speaker?' · '+esc(h.speaker):''}</small><div>${hl(h.text,q)}</div></div>`).join('')});
$('#results').addEventListener('click',e=>{const r=e.target.closest('.r');if(!r)return;
  location.hash='v='+encodeURIComponent(r.dataset.id)+(r.dataset.t?'&t='+r.dataset.t:'');if(cur&&cur.id==r.dataset.id&&r.dataset.t)seek(+r.dataset.t)});
load();
</script></body></html>"""


def _arg(flag: str) -> str | None:
    if flag in sys.argv:
        i = sys.argv.index(flag)
        if i + 1 < len(sys.argv):
            return sys.argv[i + 1]
    return None


def main() -> None:
    global ROOT, PORT
    if _arg("--root"):
        ROOT = Path(_arg("--root")).resolve()
    if _arg("--port"):
        PORT = int(_arg("--port"))
    if len(sys.argv) > 3 and sys.argv[1] == "chapters":  # chapters <video-id> <youtube-url>
        n = fetch_chapters(video_dir(sys.argv[2]), sys.argv[3])
        print(f"{n} chapter(s) saved to {sys.argv[2]}/chapters.json")
        return
    if len(sys.argv) > 2 and sys.argv[1] == "search":
        for h in search(" ".join(sys.argv[2:])):
            t = "" if h["t"] is None else f"@{int(h['t'] // 60)}:{int(h['t'] % 60):02d} "
            sp = f"[{h['speaker']}] " if h["speaker"] else ""
            print(f"{h['id']} | {h['kind']} | {t}{sp}{h['text'][:220]}")
        return
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", PORT), H)
    url = f"http://127.0.0.1:{PORT}/"
    print(f"Video Library: {url}  (root {ROOT})  Ctrl+C to stop")
    if "--no-open" not in sys.argv:
        webbrowser.open(url)
    srv.serve_forever()


if __name__ == "__main__":
    main()
