"""crv-library: one local library for every video crv has watched.

Menu = topic > subfolder > video (the folder layout under crv-out IS the
organisation). Each video: player, speaker-labelled transcript (click a line
to seek), AI notes (notes.md), action steps (the notes.md section whose
heading says action/next steps/to-do), and your own notes (my-notes.md,
saved to disk next to the video). Search runs across every transcript and
every note at once, from the page or from the command line:

    python library.py                     # serve on http://127.0.0.1:8770
    python library.py search "guarantee"  # same search, printed (for an AI)

Stdlib only, binds to 127.0.0.1, nothing leaves the machine.
"""
from __future__ import annotations

import http.server
import json
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
ACTION_RE = re.compile(r"action|next step|to-?do|homework", re.I)


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


def title_of(d: Path, notes: str) -> str:
    m = re.search(r"^#\s+(.+)$", notes, re.M)
    return m.group(1).strip() if m else pretty(d.name)


def library() -> list[dict]:
    items = []
    for tj in sorted(ROOT.rglob("transcript.json")):
        d = tj.parent
        rel = d.relative_to(ROOT).as_posix()
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
        })
    return items


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
        for s in segments(d):
            t = s.get("text", "")
            if all(w in t.lower() for w in words):
                hits.append({"id": v["id"], "title": v["title"], "kind": "transcript",
                             "t": s.get("start", 0), "speaker": s.get("speaker", ""), "text": t.strip()})
        for kind, fname in (("ai notes", "notes.md"), ("my notes", "my-notes.md")):
            for line in read(d / fname).splitlines():
                if all(w in line.lower() for w in words):
                    m = re.search(r"@(\d+):(\d\d)(?::(\d\d))?", line)
                    t = None
                    if m:
                        a, b, c = m.groups()
                        t = int(a) * 3600 + int(b) * 60 + int(c) if c else int(a) * 60 + int(b)
                    hits.append({"id": v["id"], "title": v["title"], "kind": kind,
                                 "t": t, "speaker": "", "text": line.strip()})
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

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        q = {k: v[0] for k, v in urllib.parse.parse_qs(u.query).items()}
        try:
            if u.path == "/":
                return self._send(PAGE.encode(), "text/html; charset=utf-8")
            if u.path == "/api/library":
                return self._json({"root": str(ROOT), "videos": library()})
            if u.path == "/api/search":
                return self._json(search(q.get("q", ""), q.get("topic", "")))
            if u.path == "/api/video":
                d = video_dir(q["id"])
                notes = read(d / "notes.md")
                return self._json({"segments": segments(d), "notes": notes,
                                   "actions": action_steps(notes), "my_notes": read(d / "my-notes.md")})
            if u.path == "/media":
                return self._media(video_dir(q["id"]))
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

    def do_POST(self):
        u = urllib.parse.urlparse(self.path)
        q = {k: v[0] for k, v in urllib.parse.parse_qs(u.query).items()}
        if u.path != "/api/my-notes":
            return self._json({"error": "not found"}, 404)
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


PAGE = r"""<!doctype html>
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
    .replace(/@?\b(\d{1,2}):(\d\d)(?::(\d\d))?\b/g,(m,a,b,c)=>`<a class="t" data-t="${c?(+a*3600+ +b*60+ +c):(+a*60+ +b)}">${m}</a>`);
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


def main() -> None:
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
