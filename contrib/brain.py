#!/usr/bin/env python3
"""crv brain: push a crv-out video into SEZARS-BRAIN (Obsidian vault + Supabase brain).

usage: brain.py <crv-out-dir> [--no-capture]
- Vault: writes 005-knowledge/video-library/<topic>/<slug>.md (type: video) with summary,
  on-screen list, action items and full transcript; refreshes MOC-Video-Library.md; logs to log.md.
  Only ever overwrites notes it generated itself (generated_by: crv-brain).
- Brain: capture_thought (compact: title, summary, screens, links, vault path) via the
  sezars-brain MCP from ~/.claude.json. Skipped when notes.md is missing or nothing changed
  since the last capture (hash kept in <dir>/.brain-captured).
"""
import hashlib
import json
import re
import sys
import urllib.request
from datetime import date
from pathlib import Path

HOME = Path.home()
CRV_ROOT = HOME / "crv-out"
VAULT = HOME / "projects/obsidian-vaults/SEZARS-BRAIN"
LIB = VAULT / "005-knowledge" / "video-library"
MARK = "generated_by: crv-brain"


def read(p: Path) -> str:
    return p.read_text(errors="ignore") if p.is_file() else ""


def mmss(t: float) -> str:
    t = int(t)
    return f"{t // 3600}:{t % 3600 // 60:02d}:{t % 60:02d}" if t >= 3600 else f"{t // 60}:{t % 60:02d}"


def section(md: str, name: str) -> str:
    m = re.search(rf"^##\s+{name}[^\n]*\n(.*?)(?=^##\s|\Z)", md, re.M | re.S | re.I)
    return m.group(1).strip() if m else ""


def one_line(v, limit: int) -> str:
    return re.sub(r"\s+", " ", str(v or "")).strip()[:limit]


def screens(d: Path) -> list:
    """screens.json is agent-written: keep well-typed entries, one-line text, finite times, clean URLs."""
    try:
        data = json.loads(read(d / "screens.json") or "[]")
    except ValueError:
        return []
    out = []
    for s in data if isinstance(data, list) else []:
        if not isinstance(s, dict):
            continue
        t = s.get("t")
        t = t if isinstance(t, (int, float)) and not isinstance(t, bool) and 0 <= t < 1e7 else 0
        links = s.get("links") if isinstance(s.get("links"), list) else []
        out.append({"t": t, "title": one_line(s.get("title"), 120), "note": one_line(s.get("note"), 600),
                    "links": [u for u in links if isinstance(u, str) and re.fullmatch(r"https?://[^\s<>\"'\]\)]+", u)]})
    return out


def capture(content: str) -> str:
    cfg = json.loads(read(HOME / ".claude.json"))["mcpServers"]["sezars-brain"]
    body = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "capture_thought", "arguments": {
        "content": content, "source": "crv-video-library", "source_type": "video-note"}}}
    req = urllib.request.Request(cfg["url"], data=json.dumps(body).encode(), headers={
        **cfg.get("headers", {}), "Content-Type": "application/json", "Accept": "application/json, text/event-stream"})
    with urllib.request.urlopen(req, timeout=60) as r:
        raw = r.read().decode()
    data = [ln[5:].strip() for ln in raw.splitlines() if ln.startswith("data:")]  # SSE or plain JSON
    msg = json.loads(data[-1] if data else raw)
    if "error" in msg or msg.get("result", {}).get("isError"):
        raise RuntimeError(json.dumps(msg)[:300])
    return " ".join(c.get("text", "") for c in msg["result"].get("content", []))[:200]


def generated(text: str) -> bool:
    """True only if the note's own frontmatter says crv-brain wrote it."""
    m = re.match(r"---\n(.*?)\n---\n", text, re.S)
    return bool(m and re.search(rf"^{MARK}$", m.group(1), re.M))


def note_path_for(rel: Path) -> Path:
    """crv-out/<topic>/<a>/<b> -> video-library/<topic>/<a>/<b>.md (1:1, no slug collisions)."""
    p = LIB.joinpath(*rel.parts) if len(rel.parts) > 1 else LIB / "inbox" / rel.parts[0]
    return p.parent / (p.name + ".md")


def main() -> None:
    d = Path(sys.argv[1]).expanduser().resolve()
    rel = d.relative_to(CRV_ROOT.resolve())
    topic = rel.parts[0] if len(rel.parts) > 1 else "inbox"
    slug = rel.parts[-1]
    notes, manifest = read(d / "notes.md"), read(d / "MANIFEST.txt")
    m = re.search(r"^#\s+(.+)$", notes, re.M)
    title = m.group(1).strip() if m else slug
    m = re.search(r"^source:[ \t]*(.+?)[ \t]*$", manifest, re.M)  # local paths may contain spaces
    src = m.group(1) if m else ""
    dur = re.search(r"^duration:\s*(\d+)", manifest, re.M)
    scr = screens(d)
    dash = "http://127.0.0.1:8770/#v=" + urllib.request.quote(str(rel), safe="")

    on_screen = "\n".join(
        f"- **{mmss(s.get('t') or 0)} {s.get('title', '')}** — {s.get('note', '')}"
        + "".join(f"\n  - {u}" for u in s["links"])
        for s in scr)
    summary, actions = section(notes, "Summary"), section(notes, "Action steps")
    note_path = note_path_for(rel)
    existed = note_path.exists()
    if existed and not generated(read(note_path)):
        sys.exit(f"refusing to overwrite hand-made note {note_path}")
    note_path.parent.mkdir(parents=True, exist_ok=True)
    m = re.search(r"^date_ingested:\s*(\S+)", read(note_path), re.M)
    ingested = m.group(1) if m else date.today()
    note = f"""---
type: video
{MARK}
title: {json.dumps(title, ensure_ascii=False)}
topic: {json.dumps(topic)}
source_url: {json.dumps(src)}
duration_min: {round(int(dur.group(1)) / 60) if dur else ""}
date_ingested: {ingested}
crv_folder: {json.dumps(str(d))}
dashboard: {json.dumps(dash)}
tags: [video-library, {json.dumps(topic)}]
---

# {title}

> [!info] Source
> {src or "local file"} · [Video Library dashboard]({dash}) · crv folder `{d}`

## Summary
{summary or "_(no AI notes yet)_"}

## On screen
{on_screen or "_(no screens.json yet)_"}

## Action items
{actions or "-"}

## Transcript
{read(d / "transcript.txt").strip()}
"""
    changed = read(note_path) != note
    if changed:
        note_path.write_text(note)
    # MOC: one line per video note, grouped by topic (regenerated, idempotent)
    rows = []
    for p in sorted(x for x in LIB.rglob("*.md") if x.parent != LIB):
        t = re.search(r"^title:\s*(\".*\")\s*$", read(p), re.M)
        name = json.loads(t.group(1)) if t else p.stem
        rows.append(f"- [[{p.relative_to(VAULT).with_suffix('')}|{name}]] · {p.relative_to(LIB).parts[0]}")
    (LIB / "MOC-Video-Library.md").write_text(
        "---\ntype: moc\ntags: [video-library]\n---\n\n# Video Library\n\nVideos watched with crv "
        "(`crv-watch`). Each note: summary, what was shown on screen, actions, transcript.\n\n" + "\n".join(rows) + "\n")
    if changed:
        with (VAULT / "log.md").open("a") as f:
            f.write(f"\n## [{date.today()}] {'update' if existed else 'ingest'} | video: {title}\n"
                    f"`{note_path.relative_to(VAULT)}` from crv `{d}` ({len(scr)} screens).\n")
    print(f"vault: {note_path} ({'written' if changed else 'unchanged'})")

    if "--no-capture" in sys.argv or not notes.strip():
        print("brain: skipped (" + ("--no-capture" if "--no-capture" in sys.argv else "no notes.md yet") + ")")
        return
    title = one_line(title, 200)
    content = (f"VIDEO NOTE: {title}\nSource: {src or 'local file'}\nVault: {note_path.relative_to(VAULT)}\n"
               f"Dashboard: {dash}\n\nSUMMARY\n{summary}\n\nON SCREEN\n{on_screen}\n\nACTIONS\n{actions}")
    h = hashlib.sha256(content.encode()).hexdigest()  # hash the FULL text; only the upload is capped
    if read(d / ".brain-captured").strip() == h:
        print("brain: unchanged since last capture, skipped")
        return
    print("brain:", capture(content[:12000]))
    (d / ".brain-captured").write_text(h)


if __name__ == "__main__":
    main()
