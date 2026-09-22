#!/usr/bin/env python3
"""Sanity-check a transcript produced by align_script.py, before trusting 28 of them.

Checks the things a forced alignment can get wrong without saying so: words it
never placed, times that run backwards, a segment that claims more of the clock
than anyone could speak in, and whether the editor's own matcher agrees the
transcript and the script are the same words.
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from src.lyrics.lyrics import _find_markdown_for_audio          # noqa: E402
from src.lyrics.lyrics_editor import _verify_matchup, _split_candidates  # noqa: E402


def check(mp3: str) -> int:
    mp3 = Path(mp3).resolve()
    jp = mp3.with_suffix(".json")
    if not jp.exists():
        print(f"  no {jp.name}"); return 1
    segs = json.loads(jp.read_text())["segments"]
    md = _find_markdown_for_audio(str(mp3))

    words = [w for s in segs for w in s["words"]]
    timed = [w for w in words if w.get("start") is not None]
    print(f"  segments {len(segs)}   words {len(words)}   timed {len(timed)}"
          f"   untimed {len(words) - len(timed)}")

    back = sum(1 for w in timed if w["end"] < w["start"])
    order = sum(1 for a, b in zip(timed, timed[1:]) if b["start"] < a["start"] - 0.01)
    print(f"  words with end before start: {back}")
    print(f"  words out of time order:     {order}")

    gaps = sorted(((b["start"] - a["end"], a["word"], b["word"])
                   for a, b in zip(timed, timed[1:])), reverse=True)[:5]
    print("  largest gaps between consecutive words (silence, music, effects):")
    for g, a, b in gaps:
        print(f"    {g:6.1f}s   after {a!r} before {b!r}")

    slow = sorted(((w["end"] - w["start"], w["word"]) for w in timed), reverse=True)[:5]
    print("  longest single words (a stretched one means a bad window seam):")
    for d, w in slow:
        print(f"    {d:6.2f}s   {w!r}")

    rate = len(timed) / max(1e-9, timed[-1]["end"] - timed[0]["start"]) * 60
    print(f"  speaking rate over the aligned span: {rate:.0f} wpm")

    if md:
        rep = _verify_matchup(segs, md)["summary"]
        cands, unplaced, suggested = _split_candidates(segs, md)
        print(f"  verify: {rep['match_pct']}% matched  "
              f"({rep['missing']} missing, {rep['extra']} extra, {rep['changed']} changed, "
              f"{rep.get('elided', 0)} elided)")
        print(f"  still to split: {len(cands)}   unplaced directions: {len(unplaced)}"
              f"   mid-line suggestions: {len(suggested)}")
    return 0


def summary(mp3: str) -> int:
    """One line per episode: enough to see which of 28 needs a closer look.

    `worst` is the longest run of speech holding no measurement at either end of
    it, so it is the bound on how far out a single line in that episode can be.
    Nothing accumulates past it, so this is a ceiling on the error, not a estimate
    of it — an episode with a small worst figure needs no further attention.
    """
    mp3 = Path(mp3).resolve()
    jp = mp3.with_suffix(".json")
    if not jp.exists():
        print(f"{mp3.stem[:28]:30} no transcript"); return 1
    segs = json.loads(jp.read_text())["segments"]
    words = [w for s in segs for w in s["words"]]
    timed = [w for w in words if w.get("start") is not None]
    if not timed:
        print(f"{mp3.stem[:28]:30} nothing timed"); return 1
    order = sum(1 for a, b in zip(timed, timed[1:]) if b["start"] < a["start"] - 0.01)
    worst = max((b["start"] - a["end"] for a, b in zip(timed, timed[1:])), default=0.0)
    longest = max(w["end"] - w["start"] for w in timed)
    md = _find_markdown_for_audio(str(mp3))
    pct = _verify_matchup(segs, md)["summary"]["match_pct"] if md else 0.0
    bad = len(words) - len(timed) or order
    print(f"{mp3.stem[:28]:30} {len(segs):4} segs  {len(words):5} words  "
          f"{len(words) - len(timed):3} untimed  {order:3} out of order  "
          f"worst gap {worst:5.1f}s  longest word {longest:4.1f}s  verify {pct:5.1f}%"
          f"{'   <-- look' if bad or worst > 20 or pct < 99 else ''}")
    return 1 if bad else 0


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if a != "--summary"]
    rc = 0
    if "--summary" in sys.argv:
        for a in args:
            rc |= summary(a)
    else:
        for a in args:
            print(Path(a).name)
            rc |= check(a)
    sys.exit(rc)
