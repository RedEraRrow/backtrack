"""Lyrics to and from files: .lrc, .srt, .vtt, Whisper's .json, .ass and
plain text, read into a track's tags and written out of them. One place for
the CLI, the tag editor, the bulk editor and the player, so they can't
disagree about what a file means."""
from __future__ import annotations

import json
import os
import re

from backbone import timefmt

# The kinds of lyrics file written, and what each is good for.
FORMATS = {
    'lrc': "LRC, timed lines most players read",
    'srt': "SRT subtitles",
    'vtt': "WebVTT subtitles",
    'json': "Whisper JSON",
    'txt': "Plain text, the words only",
}
# And read: those, and subtitle files from Aegisub and the like.
READS = {**FORMATS, 'ass': "ASS subtitles", 'ssa': "SSA subtitles"}
# A file beside a track, best first: synced kinds, then the words alone.
EXTENSIONS = ('.lrc', '.vtt', '.srt', '.json', '.ass', '.ssa', '.txt')

# Where the player's lyrics come from (the `lyrics_source` setting).
SOURCES = {
    'auto': "Best available",
    'files': "Files beside the track, then tags",
    'embedded': "Tags only",
    'synced': "Synced lyrics only",
}

_CUE_TIME = r'(?:(\d+):)?(\d+):(\d+)[,.](\d{1,3})'        # [h:]mm:ss,mmm (SRT) or .mmm (WebVTT)
_CUE_RE = re.compile(rf'{_CUE_TIME}\s*-->\s*{_CUE_TIME}')
_MARKUP_RE = re.compile(r'<[^>]*>')                           # WebVTT voice, class and karaoke tags
_ASS_OVERRIDE_RE = re.compile(r'\{[^}]*\}')                  # ASS {\b1} styling


def _ms(h, m, s, frac) -> int:
    return ((int(h or 0) * 60 + int(m)) * 60 + int(s)) * 1000 + int(frac.ljust(3, '0')[:3])


def parse_cue_text(raw: str) -> list[tuple[str, int | None]]:
    """SRT or WebVTT cues as (text, start ms): each cue's lines joined as one,
    their markup dropped (a block with no timing, WEBVTT or a NOTE, is skipped)."""
    entries = []
    for block in re.split(r'\n\s*\n', raw.replace('\r\n', '\n').replace('\r', '\n').strip()):
        lines = [ln.strip() for ln in block.split('\n') if ln.strip()]
        cue = next((i for i, ln in enumerate(lines) if _CUE_RE.search(ln)), None)
        if cue is None:
            continue
        text = " ".join(_MARKUP_RE.sub("", ln) for ln in lines[cue + 1:]).strip()
        if text:
            entries.append((text, _ms(*_CUE_RE.search(lines[cue]).groups()[:4])))
    return entries


def parse_ass_text(raw: str) -> list[tuple[str, int | None]]:
    """ASS/SSA Dialogue events as (text, start ms), styling dropped and its
    line breaks (\\N) as spaces."""
    entries = []
    for line in raw.splitlines():
        if not line.startswith('Dialogue:'):
            continue
        fields = line.split(':', 1)[1].split(',', 9)
        if len(fields) < 10:
            continue
        m = re.match(r'\s*(\d+):(\d+):(\d+)[.:](\d{1,3})', fields[1])
        text = _ASS_OVERRIDE_RE.sub("", fields[9]).replace('\\N', ' ').replace('\\n', ' ').strip()
        if m and text:
            entries.append((text, _ms(*m.groups())))
    return sorted(entries, key=lambda e: e[1])


def _seconds_ms(value) -> int | None:
    """A JSON timestamp as ms: seconds as a number, a [start, end] pair, or an
    SRT-style clock string; None when it's none of those."""
    if isinstance(value, (list, tuple)) and value:
        value = value[0]
    if isinstance(value, (int, float)):
        return int(round(value * 1000))
    if isinstance(value, str):
        m = re.search(_CUE_TIME, value)
        return _ms(*m.groups()) if m else None
    return None


def parse_json_text(raw: str) -> list[tuple[str, int | None]]:
    """Whisper-style JSON as (text, start ms): `segments` of {start, text}
    (WhisperX and backtrack's own transcripts too, their stage directions and
    dead air skipped), else `word_segments` joined into lines at sentence ends,
    else a bare list of {text, timestamp} (MacWhisper's export)."""
    try:
        data = json.loads(raw)
    except ValueError:
        return []
    if isinstance(data, dict) and isinstance(data.get('segments'), list):
        return [(str(seg.get('text', '')).strip(), _seconds_ms(seg.get('start')))
                for seg in data['segments']
                if isinstance(seg, dict) and seg.get('kind') not in ('stage_dir', 'dead_air')
                and str(seg.get('text', '')).strip()]
    if isinstance(data, dict) and isinstance(data.get('word_segments'), list):
        lines, words, start = [], [], None
        for w in data['word_segments']:
            word = str(w.get('word', w.get('text', ''))).strip()
            if not word:
                continue
            start = _seconds_ms(w.get('start')) if not words else start
            words.append(word)
            if word[-1] in '.!?' or len(words) >= 12:
                lines.append((" ".join(words), start))
                words = []
        return lines + ([(" ".join(words), start)] if words else [])
    if isinstance(data, list):
        return [(str(item.get('text', '')).strip(), _seconds_ms(item.get('start', item.get('timestamp'))))
                for item in data if isinstance(item, dict) and str(item.get('text', '')).strip()]
    return []


def read_file(path: str) -> list[tuple[str, int | None]]:
    """A lyrics file's lines as (text, ms), ms None where untimed: by its
    extension, except that a .txt holding [mm:ss] stamps is read as the LRC it is."""
    from backtrack.lyrics.formats import parse_lrc_text
    with open(path, encoding='utf-8-sig', errors='replace') as handle:
        raw = handle.read()
    ext = os.path.splitext(path)[1].lower()
    if ext in ('.srt', '.vtt'):
        return parse_cue_text(raw)
    if ext in ('.ass', '.ssa'):
        return parse_ass_text(raw)
    if ext == '.json':
        return parse_json_text(raw)
    entries = parse_lrc_text(raw)
    if ext == '.lrc' or any(ms is not None for _t, ms in entries):
        return entries
    return [(line.strip(), None) for line in raw.splitlines() if line.strip()]


def sidecar(audio_path: str) -> str | None:
    """A lyrics file beside the track with its name, best first (EXTENSIONS):
    .lrc, the subtitle kinds and Whisper's .json, then .txt."""
    stem = os.path.splitext(audio_path)[0]
    return next((stem + ext for ext in EXTENSIONS if os.path.isfile(stem + ext)), None)


def import_file(audio_path: str, file_path: str, into: str | None = None,
                desc: str = '', lang: str = 'eng') -> tuple[str, int]:
    """A lyrics file written into the track's tags, replacing that one frame
    (`desc`, `lang`). `into` 'SYLT' takes the timed lines (none: nothing is
    written), 'USLT' the words alone; None decides by the file, timed lines
    into SYLT, else USLT. (tag, lines); ('', 0) when nothing was written."""
    from mutagen.id3 import USLT  # type: ignore[reportPrivateImportUsage]
    from backtrack.id3.tag_handler import load_id3, save_id3
    from backtrack.lyrics.formats import save_sylt_entries

    entries = read_file(file_path)
    timed = sorted(((text, int(ms)) for text, ms in entries if ms is not None and text), key=lambda e: e[1])
    if into == 'SYLT' or (into is None and timed):
        if not timed:
            return '', 0
        save_sylt_entries(audio_path, timed, desc=desc, lang=lang)
        return 'SYLT', len(timed)
    words = [text for text, _ms in entries if text]
    if not words:
        return '', 0
    audio = load_id3(audio_path)
    frame = USLT(encoding=3, lang=lang, desc=desc, text="\n".join(words))
    audio.delall(frame.HashKey)             # just this frame: other lyrics frames stay
    audio.add(frame)
    save_id3(audio, audio_path)
    return 'USLT', len(words)


def track_lyrics(audio_path: str) -> tuple[list[tuple[str, int | None]], str]:
    """A track's own lyrics as (text, ms) rows and where they're from: timed
    SYLT first, else USLT (any format's tags, through the player's view of them)."""
    from backtrack.lyrics import formats as ly
    from backtrack.playback.session import player_tags
    audio = player_tags(audio_path)
    timed = ly._parse_sylt(audio)
    if timed:
        return [(text, ms) for text, ms in timed], 'SYLT'
    return [(text, None) for text, _ms in ly._parse_uslt(audio)], 'USLT'


def render(rows: list, fmt: str, audio_path: str = "") -> str:
    """`rows` as the text of a `fmt` file. An .lrc carries the standard header
    (artist, album, title, length), which players match a file to its song by;
    a subtitle cue or JSON segment runs to the next one's start."""
    stamp = lambda ms: f"[{timefmt.clock(int(ms) / 1000)}]" if ms is not None else ""   # noqa: E731
    if fmt == 'lrc':
        head = []
        if audio_path:
            from backtrack.music_library import library_entry
            e = library_entry(audio_path)
            dur = int(e.get('duration') or 0)
            head = [f"[{tag}:{e[key]}]" for tag, key in (('ar', 'artist'), ('al', 'album'), ('ti', 'title'))
                    if e.get(key) and not str(e[key]).startswith('Unknown ')]
            head += [f"[length:{dur // 60:02d}:{dur % 60:02d}]"] if dur else []
        return "\n".join(head + [f"{stamp(ms)}{text}" for text, ms in rows])
    spans = [(text, int(ms or 0), int(rows[i + 1][1]) if i + 1 < len(rows) and rows[i + 1][1] else int(ms or 0) + 3000)
             for i, (text, ms) in enumerate(rows)]          # a cue runs to the next one's start
    if fmt == 'srt':
        return "\n".join(f"{i}\n{timefmt.srt(a / 1000)} --> {timefmt.srt(b / 1000)}\n{text}\n"
                         for i, (text, a, b) in enumerate(spans, 1))
    if fmt == 'vtt':
        vtt = lambda ms: timefmt.srt(ms / 1000).replace(',', '.')   # noqa: E731
        return "WEBVTT\n\n" + "\n".join(f"{vtt(a)} --> {vtt(b)}\n{text}\n" for text, a, b in spans)
    if fmt == 'json':
        timed = any(ms is not None for _t, ms in rows)
        segments = [{'start': a / 1000, 'end': b / 1000, 'text': text} if timed else {'text': text}
                    for text, a, b in spans]
        return json.dumps({'segments': segments}, ensure_ascii=False, indent=2)
    return "\n".join(text for text, _ms in rows)


def dialogue_rows(segs: list, md_path: str, kinds: dict | None = None) -> list[tuple[str, int]]:
    """A dialogue track (timed transcript segments and its script) as plain
    timed lines, the way the player shows it: each line its speaker's, with the
    manner it's said in where there is one ("MARTIN (angrily): …"), named again
    only when either changes; each stage direction a line of its own in
    brackets; silence left out. Markdown is dropped, for players that would
    show it as typed."""
    from backtrack.lyrics.formats import _chunks_from_segments
    from backtrack.lyrics.text import strip_markdown
    chunks, times = _chunks_from_segments(segs, md_path, 0.0, kinds)
    rows, said = [], None
    for c, (start, _end) in zip(chunks, times):
        if c['is_air']:
            continue
        if c['is_stage']:
            rows.append((f"({strip_markdown(c['stage_dir']).strip().strip('()')})", int(start * 1000)))
            continue
        who = (c['speaker'], c['stage_dir'])
        head = ""
        if c['speaker'] and (c.get('line'), who) != said:
            head = c['speaker'] + (f" ({strip_markdown(c['stage_dir'])})" if c['stage_dir'] else "") + ": "
        said = (c.get('line'), who)
        rows.append((head + strip_markdown(c['text']).strip(), int(start * 1000)))
    return rows


def export_file(audio_path: str, fmt: str, directory: str | None = None,
                rows: list | None = None) -> tuple[str, int]:
    """Write the track's lyrics (`rows`, else its tags') as `<name>.<fmt>` in
    `directory` (else beside the track). (the file written, lines); ('', 0)
    when there are none."""
    if rows is None:
        rows, _source = track_lyrics(audio_path)
    if not rows:
        return '', 0
    stem = os.path.splitext(os.path.basename(audio_path))[0]
    target = os.path.join(os.path.expanduser(directory or os.path.dirname(audio_path)), f"{stem}.{fmt}")
    with open(target, 'w', encoding='utf-8') as handle:
        handle.write(render(rows, fmt, audio_path) + "\n")
    return target, len(rows)
