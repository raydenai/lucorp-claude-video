#!/usr/bin/env python3
"""crv screens: full-res stills + OCR for every crv keyframe -> screens.md.

usage: screens.py <crv-out-dir> [--jobs 8]
Writes <dir>/hires/*.jpg (1080p stills at each keyframe time), <dir>/ocr/*.txt,
and <dir>/screens.md: every domain/URL seen on screen with its first-seen
[mm:ss] (clickable in the crv library dashboard). Re-runs skip existing files.
"""
import collections
import concurrent.futures as cf
import difflib
import json
import re
import subprocess
import sys
from pathlib import Path

TLD = r"(?:com|org|net|io|ai|dev|de|co|app|gov|edu|info|xyz|me|tv|us|uk|gg|so|sh)"
DOMAIN = re.compile(rf"\b((?:[a-z0-9-]+\.)+{TLD}(?:\.[a-z]{{2}})?)\b", re.I)
URL = re.compile(r"\b((?:github\.com|arxiv\.org/abs|doi\.org|huggingface\.co)/[\w./-]+)", re.I)


def ts(sec: float) -> str:
    return f"[{int(sec // 60):02d}:{int(sec % 60):02d}]"


def main() -> None:
    out = Path(sys.argv[1]).expanduser()
    jobs = int(sys.argv[sys.argv.index("--jobs") + 1]) if "--jobs" in sys.argv else 8
    src = next((out / n for n in ("source.mp4", "source.webm", "source.mkv") if (out / n).exists()), None)
    if not src:
        sys.exit(f"no source video in {out} (crv deletes it unless the run kept it)")
    d = json.loads((out / "frames.json").read_text())
    frames = d if isinstance(d, list) else d["frames"]
    (out / "hires").mkdir(exist_ok=True)
    (out / "ocr").mkdir(exist_ok=True)

    def work(f):
        img, txt = out / "hires" / f["file"], out / "ocr" / (f["file"][:-4] + ".txt")
        for back in (0, 1.0):  # a keyframe in the last few ms has nothing to decode: step back once
            if img.exists():
                break
            subprocess.run(["ffmpeg", "-v", "error", "-y", "-ss", str(max(0, f["timestamp_sec"] - back)), "-i", str(src),
                            "-frames:v", "1", "-q:v", "3", str(img)], capture_output=True)
        if not img.exists():
            return f["timestamp_sec"], ""
        if not txt.exists():
            subprocess.run(["tesseract", str(img), str(txt)[:-4], "--psm", "3"],
                           check=True, capture_output=True)
        return f["timestamp_sec"], txt.read_text(errors="ignore")

    with cf.ThreadPoolExecutor(jobs) as ex:
        texts = list(ex.map(work, frames))

    seen = collections.defaultdict(list)  # spelling -> [timestamps]
    for t, text in texts:
        for m in set(URL.findall(text)):
            seen[m.rstrip(".,").lower()].append(t)
        for m in set(DOMAIN.findall(text)):
            m = m.lower().removeprefix("www.")
            if len(m.split(".")[0]) >= 3:
                seen[m].append(t)
    # ponytail: OCR typos merged by string similarity to the most-seen spelling; real
    # near-identical domains can merge too — check screenshots for anything that matters
    canon = {}
    for s in sorted(seen, key=lambda k: -len(seen[k])):
        hit = difflib.get_close_matches(s, list(canon), n=1, cutoff=0.86)
        canon.setdefault(hit[0] if hit else s, []).extend(seen[s])
    rows = sorted(((min(v), k, len(v)) for k, v in canon.items()), key=lambda r: r[0])
    md = ["# On screen (OCR of full-res keyframes)", "",
          f"{len(rows)} domains/URLs; x = frames it appeared in. OCR can misread — verify in hires/.", ""]
    md += [f"- {ts(t)} `{k}` x{n}" for t, k, n in rows]
    (out / "screens.md").write_text("\n".join(md) + "\n")
    print(f"screens: {len(frames)} stills, {len(rows)} domains -> {out / 'screens.md'}")


if __name__ == "__main__":
    main()
