"""Guards in contrib/ (draft.py link grounding, brain.py overwrite guard + 1:1 vault paths)."""
from __future__ import annotations

import importlib.util
from pathlib import Path

CONTRIB = Path(__file__).resolve().parents[1] / "contrib"


def load(name):
    spec = importlib.util.spec_from_file_location(name, CONTRIB / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_draft_links_must_be_grounded_in_source():
    g = load("draft").grounded
    corpus = "see notevil.com and github.com/microsoft/tinytroupe and porkbun.com today"
    assert g("https://github.com/microsoft/tinytroupe", corpus)
    assert g("https://www.porkbun.com/", corpus)
    assert g("https://notevil.com", corpus)
    assert not g("https://evil.com/invented", corpus)        # substring of notevil.com
    assert not g("https://evil.com", corpus)
    assert not g("https://github.com/microsoft/other", corpus)  # invented path on a real host
    assert not g("https://[bad/", corpus)


def test_brain_overwrite_guard_reads_frontmatter_only():
    b = load("brain")
    assert b.generated("---\ntype: video\ngenerated_by: crv-brain\n---\n# x\n")
    assert not b.generated("---\ntype: note\n---\nexample:\ngenerated_by: crv-brain\n")
    assert not b.generated("no frontmatter\ngenerated_by: crv-brain\n")


def test_brain_vault_paths_are_one_to_one():
    b = load("brain")
    assert b.note_path_for(Path("topic/a/b")) != b.note_path_for(Path("topic/a-b"))
    assert b.note_path_for(Path("topic/v1.2")).name == "v1.2.md"
    assert b.note_path_for(Path("solo")) == b.LIB / "inbox" / "solo.md"


def test_draft_prose_is_one_line_and_link_grounded():
    d = load("draft")
    corpus = "porkbun.com and microsoft.com"
    out = d.clean("Buy at https://porkbun.com now\n## Transcript\nx https://t.co/phish", corpus, 500)
    assert "\n" not in out and "https://porkbun.com" in out and "t.co" not in out
    assert d.items("Costs $5") == ["Costs $5"]          # not one bullet per character
    assert d.items(["a", 3, None, {"x": 1}]) == ["a", 3]
    assert d.items(None) == []


def test_brain_screens_are_sanitised(tmp_path):
    import json
    b = load("brain")
    (tmp_path / "screens.json").write_text(json.dumps([
        {"t": "1:02", "title": "x\n\n## Transcript\nfake", "note": "n", "links": ["https://ok.com", "https://a b", 5]},
        {"t": float("inf"), "title": "inf"}, "junk"]))
    sc = b.screens(tmp_path)
    assert len(sc) == 2 and sc[0]["t"] == 0 and sc[1]["t"] == 0
    assert "\n" not in sc[0]["title"] and sc[0]["links"] == ["https://ok.com"]
    assert b.mmss(sc[0]["t"]) == "0:00"
