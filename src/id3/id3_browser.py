"""Per-file ID3 tag browser and editor UI."""
from __future__ import annotations
import os
import re
import sys
import tempfile
import pyperclip
import subprocess
import numpy as np
import cv2

from src.utils import prompt
from src.lyrics.lyrics import save_sylt_entries
from mutagen.id3 import ID3
import mutagen.id3
from mutagen.id3._frames import APIC, USLT

from src.utils import ui_utils
from src.utils.ui_utils import Colors as C, get_terminal_height, get_terminal_width
from src.art.album_art import render_with_viu
from src.music_library import refresh_library_entry

from src.id3.id3_tag_handler import (
    get_tag_info,
    get_tag_category,
    display_tag_id,
    summarize_tag_value,
    prompt_for_value,
    create_frame,
    rename_frame,
    create_apic_frame,
    TAG_REGISTRY,
    _EXT_TO_MIME,
    parse_composite_tag_id,
)

# Structured columns for the tag list. Column 1 holds the tag id AND the friendly
# name as two styled segments (TAG bright + friendly dim) in a single column.
_TAG_COLUMNS = [
    prompt.Column(style='primary'),                  # TAG (friendly)
    prompt.Column(style='dynamic-dim'),              # type / category
    prompt.Column(style='normal', flex=True),        # value (takes the rest)
]


import src.id3.name_corpus as _nc

_SORT_SOURCES: dict[str, str] = {
    'TSOT': 'TIT2',
    'TSOA': 'TALB',
    'TSOP': 'TPE1',
    'TSO2': 'TPE2',
    'TSOC': 'TCOM',
}
# Tags where full name-splitting applies (artists/composers); others just strip articles.
_NAME_SORT_TAGS = {'TSOP', 'TSO2', 'TSOC'}

# Articles in English + major European languages.
# Longer strings first so prefix matching is unambiguous.
_ARTICLE_MAP: dict[str, str] = {
    # English
    'the': 'The', 'an': 'An', 'a': 'A',
    # French
    'les': 'Les', 'le': 'Le', 'la': 'La', "l'": "L'",
    # Spanish / Portuguese
    'los': 'Los', 'las': 'Las', 'el': 'El', 'os': 'Os', 'as': 'As',
    # German
    'die': 'Die', 'der': 'Der', 'das': 'Das', 'den': 'Den', 'dem': 'Dem',
    # Italian
    'gli': 'Gli', 'lo': 'Lo', 'il': 'Il',
    # Dutch
    'het': 'Het', 'de': 'De',
    # Swedish / Norwegian / Danish
    'den': 'Den', 'det': 'Det',
}

# Spacing surname prefixes: the prefix + following word(s) form the compound surname.
# "van Beethoven" → surname is "van Beethoven".
_SPACING_PREFIXES: frozenset[str] = frozenset({
    'von', 'van', 'de', 'del', 'della', 'degli', 'dei', 'di', 'da', 'du',
    'le', 'la', 'les', 'bin', 'ibn', 'al', 'af', 'av', 'zu', 'ter', 'ten',
    'uit', 'den', 'het',
})

# Joining prefixes: merged (no space) with the following word to form one surname token.
# "O' Connor" → "O'Connor",  "Mc Gregor" → "McGregor".
_JOINING_PREFIXES: frozenset[str] = frozenset({"o'", "ó'", 'mc', 'mac', "m'"})

# Single-letter initial pattern: "J." or "J" alone.
_INITIAL_RE = re.compile(r'^[A-Za-zÀ-ÖØ-öø-ÿ]\.$')

# Delimiters that separate multiple artists in a single tag value.
# NOTE: "and his/her/their/the" is NOT a delimiter — it describes a backing
# ensemble belonging to the lead artist (e.g. "Paul Tremaine And His Aristocrats").
_LIST_SPLIT_RE = re.compile(
    r'\s*(?:'
    r'(?<!\w)&(?!\s*(?:his|her|their)\b)'   # & but not "& His/Her/Their" (backing band)
    r'|\|'
    r'|[/\\]'
    r'|\bfeaturing\b|\bfeat\.?\b|\bft\.?\b'
    r'|\band(?=\s+\w)(?!\s+(?:his|her|their)\b)'  # "and X" but not "and his/her/their"
    r'|\bwith(?=\s+\w)(?!\s+(?:his|her|their)\b)'
    r'|\bvs\.?\b'
    r'|\+'
    r')\s*',
    re.IGNORECASE,
)

# Integers (1–99) that benefit from zero-padding when used as ordinals.
_ORDINAL_RE = re.compile(r'(?<!\d)([1-9]\d?)(?!\d)')


def _pad_ordinals(s: str) -> str:
    """'Series 1' → 'Series 01'; leaves two-digit numbers unchanged."""
    def _pad(m: re.Match) -> str:
        n = int(m.group(1))
        return str(n).zfill(2) if n < 10 else m.group(1)
    return _ORDINAL_RE.sub(_pad, s)


def _merge_initials(words: list[str]) -> list[str]:
    """Merge space-separated single-letter initials: ['J.', 'S.', 'Bach'] → ['J.S.', 'Bach']."""
    result: list[str] = []
    i = 0
    while i < len(words):
        if _INITIAL_RE.match(words[i]):
            group = [words[i]]
            j = i + 1
            while j < len(words) and _INITIAL_RE.match(words[j]):
                group.append(words[j])
                j += 1
            result.append(''.join(group))
            i = j
        else:
            result.append(words[i])
            i += 1
    return result


def _merge_joining_prefixes(words: list[str]) -> list[str]:
    """Merge Celtic/Irish prefix tokens with the following word without a space.
    ['Sinéad', "O'", 'Connor'] → ['Sinéad', "O'Connor"]
    ['Ewan', 'Mc', 'Gregor'] → ['Ewan', 'McGregor']

    Does NOT merge all-uppercase 'MC' (hip-hop prefix) — that is an honorific,
    handled separately by the honorific-stripping step.
    """
    result: list[str] = []
    i = 0
    while i < len(words):
        w = words[i]
        wl = w.lower()
        # Only merge if it looks like a Celtic prefix (mixed/lower case), not
        # an all-caps abbreviation like "MC" or "MAC" used as a stage prefix.
        is_celtic = wl in _JOINING_PREFIXES and not w.isupper()
        if is_celtic and i + 1 < len(words):
            result.append(w + words[i + 1])
            i += 2
        else:
            result.append(w)
            i += 1
    return result


def _sort_single_name(name: str) -> list[str]:
    """
    Return sort-order candidates for a single name string, ordered by confidence.

    Pipeline:
    1. Merge space-separated initials ('J. S.' → 'J.S.')
    2. Merge Celtic joining prefixes ('O' Connor' → 'O'Connor')
    3. Leading article → move to end ('The Beatles' → 'Beatles, The')
    4. Strip leading honorific ('Dr. Dre' → honorific saved, name is 'Dre')
    5. Strip trailing ordinal suffix ('James Brown Jr.' → suffix saved)
    6. Check known compound surnames (corpus)
    7. Use given-name corpus to weight the most likely split point
    8. Check spacing surname prefixes (von/van/de/…)
    9. All remaining right-splits as fallback
    """
    words = _merge_joining_prefixes(_merge_initials(name.split()))
    if len(words) <= 1:
        # Check mononyms: known single-name artists are returned as-is.
        if name.strip().lower() in _nc.mononyms():
            return [name.strip()]
        return [name]

    # Multi-word mononym check (e.g. "Daft Punk", "Aphex Twin", "Flying Lotus")
    if name.strip().lower() in _nc.mononyms():
        return [name.strip()]

    # ── Article ────────────────────────────────────────────────────────────
    if words[0].lower() in _ARTICLE_MAP:
        art  = _ARTICLE_MAP[words[0].lower()]
        rest = ' '.join(words[1:])
        return [f"{rest}, {art}"]

    candidates: list[str] = []
    seen: set[str] = set()

    def _add(firstname: str, surname: str) -> None:
        nonlocal honorific, suffix
        fn = ' '.join(filter(None, [honorific, firstname]))
        sn = ' '.join(filter(None, [surname, suffix]))
        c  = f"{sn}, {fn}" if fn else sn
        if c and c not in seen:
            seen.add(c)
            candidates.append(c)

    # ── Honorific ──────────────────────────────────────────────────────────
    honorific = ''
    if words[0].rstrip('.').lower() in _nc.honorifics():
        honorific = words[0]
        words = words[1:]
        if len(words) == 1:
            # e.g. "Dr. Dre" → "Dre, Dr." / "MC Raptor" → "Raptor, MC"
            return [f"{words[0]}, {honorific}"]
        if not words:
            return [name]

    # ── Trailing suffix ────────────────────────────────────────────────────
    suffix = ''
    if words[-1].rstrip('.').lower() in _nc.ordinal_suffixes():
        suffix = words[-1]
        words = words[:-1]
        if len(words) <= 1:
            return [name]

    # ── Known compound surnames ────────────────────────────────────────────
    for compound in _nc.compound_surnames():
        n = len(compound)
        if len(words) > n and tuple(words[-n:]) == compound:
            _add(' '.join(words[:-n]), ' '.join(compound))

    # ── Corpus-weighted split ──────────────────────────────────────────────
    # Walk from right to left; the first word that is NOT a known given name
    # is treated as the start of the surname.
    corpus_split: int | None = None
    for i in range(len(words) - 1, 0, -1):
        if words[i].lower().rstrip('.') not in _nc.given_names():
            corpus_split = i
            break
    if corpus_split is not None:
        _add(' '.join(words[:corpus_split]), ' '.join(words[corpus_split:]))

    # ── Spacing surname prefixes ────────────────────────────────────────────
    for i, w in enumerate(words):
        if i == 0:
            continue
        if w.lower() in _SPACING_PREFIXES and i < len(words) - 1:
            _add(' '.join(words[:i]), ' '.join(words[i:]))

    # ── All right-splits (fallback, most-specific first) ──────────────────
    for n in range(1, len(words)):
        _add(' '.join(words[:len(words) - n]), ' '.join(words[len(words) - n:]))

    return candidates if candidates else [name]


def _sort_candidates(base_id: str, raw: str) -> list[str]:
    """
    Generate sort-order candidates for a raw tag value.
    Returns candidates ordered by confidence, excluding the raw value itself.
    """
    candidates: list[str] = []
    seen: set[str] = {raw}

    def _add(c: str) -> None:
        if c and c not in seen:
            seen.add(c)
            candidates.append(c)

    if base_id in _NAME_SORT_TAGS:
        parts = _LIST_SPLIT_RE.split(raw)
        # Strip stray punctuation left by delimiter tokenisation (e.g. trailing "." from "Feat.").
        parts = [re.sub(r'^[^\w\s]+|[^\w\s]+$', '', p).strip() for p in parts]
        parts = [p for p in parts if p]

        if len(parts) > 1:
            # Multi-entity list: sort each part, join with the configured delimiter.
            from src.config import load_config
            delim = load_config().get('sort_list_delimiter', '/')
            best_parts = [(_sort_single_name(p) or [p])[0] for p in parts]
            _add(delim.join(best_parts))
        else:
            for c in _sort_single_name(raw):
                _add(c)
    else:
        # Title / album: strip leading article only.
        words = raw.split()
        if words and words[0].lower() in _ARTICLE_MAP:
            art  = _ARTICLE_MAP[words[0].lower()]
            rest = ' '.join(words[1:])
            _add(f"{rest}, {art}")

    # Ordinal-padded variants of every candidate plus the raw value.
    for base in list(candidates) + [raw]:
        padded = _pad_ordinals(base)
        _add(padded)

    return candidates


def _prompt_sort_order(base_id: str, audio: ID3) -> str | None:
    """
    If base_id is a sort tag and its source tag has a value, show sort-order
    suggestions and return the chosen prefill (or None to skip / type custom).
    Returns None immediately for non-sort tags.
    """
    source_id = _SORT_SOURCES.get(base_id)
    if not source_id:
        return None
    frame = audio.get(source_id)
    if not frame or not getattr(frame, 'text', None):
        return None
    raw = str(frame.text[0]).strip()
    if not raw:
        return None

    cands = _sort_candidates(base_id, raw)
    if not cands:
        return raw  # nothing to suggest — just prefill with the raw value

    if len(cands) == 1:
        return cands[0]

    # Multiple suggestions: let the user pick, or type their own.
    _CUSTOM = "— type custom"
    picked = prompt.select(
        f'Sort order for “{raw}” — pick a suggestion:',
        choices=cands + [prompt.separator(), _CUSTOM],
    )
    if picked is None or picked == _CUSTOM:
        return None
    return picked


def _get_image_from_apic(apic_frame: APIC) -> tuple:
    try:
        img_data = getattr(apic_frame, 'data', b"")
        mime_type = getattr(apic_frame, 'mime', "image/jpeg")
        if not img_data:
            return None, mime_type, b""
        nparr = np.frombuffer(img_data, np.uint8)
        image = cv2.imdecode(nparr, cv2.IMREAD_COLOR)
        return image, mime_type, img_data
    except (ValueError, cv2.error):
        return None, "unknown", b""


def _convert_apic_to_viu(apic_frame: APIC, width: int = 80) -> str:
    img_bytes = getattr(apic_frame, 'data', None)
    if not img_bytes:
        return "Error: No image data."
    return render_with_viu(img_bytes, width=width, is_bytes=True)


def _open_apic_preview(apic_frame: APIC) -> bool:
    image, mime_type, img_bytes = _get_image_from_apic(apic_frame)

    if not img_bytes or (hasattr(img_bytes, 'size') and img_bytes.size == 0) or not len(img_bytes):
        return False

    try:
        ext = {
            'image/jpeg': '.jpg', 'image/jpg': '.jpg',
            'image/png': '.png', 'image/gif': '.gif',
        }.get(mime_type, '.jpg')

        with tempfile.NamedTemporaryFile(suffix=ext, delete=False) as tmp:
            data_to_write = img_bytes.tobytes() if hasattr(img_bytes, 'tobytes') else img_bytes
            tmp.write(data_to_write)
            tmp_path = tmp.name

        if sys.platform == 'darwin':
            subprocess.run(['open', tmp_path], check=True)
        elif sys.platform == 'win32':
            os.startfile(tmp_path)
        elif sys.platform.startswith('linux'):
            subprocess.run(['xdg-open', tmp_path], check=True)
        else:
            raise OSError(f"Unsupported OS: {sys.platform}")

        return True
    except (OSError, subprocess.CalledProcessError) as e:
        print(f"Error opening preview: {e}")
        return False


_LRC_TIMESTAMP_RE = re.compile(r'\[(\d+):(\d+)(?:[.:](\d{1,3}))?\]')
_LRC_META_RE = re.compile(r'^\s*\[(ti|ar|al|by|offset|re|ve)\s*:.+\]\s*$', re.I)

def _parse_lrc_file(lrc_path: str) -> list[tuple[str, int | None]]:
    with open(lrc_path, "r", encoding="utf-8") as f:
        raw = f.read()

    entries: list[tuple[str, int | None]] = []
    for raw_line in raw.splitlines():
        if _LRC_META_RE.match(raw_line):
            continue
        timestamps = list(_LRC_TIMESTAMP_RE.finditer(raw_line))
        text = _LRC_TIMESTAMP_RE.sub("", raw_line).strip()
        if not text and not timestamps:
            continue
        if timestamps:
            for match in timestamps:
                mins = int(match.group(1))
                secs = int(match.group(2))
                frac = match.group(3) or "0"
                ms = int(frac.ljust(3, "0")[:3])
                entries.append((text, mins * 60_000 + secs * 1_000 + ms))
        else:
            entries.append((text, None))
    return entries


def _import_from_lrc(file_path: str, audio: ID3, tag_id: str) -> None:
    default_lrc = os.path.splitext(file_path)[0] + ".lrc"
    lrc_path = prompt.text("LRC file path:", default=default_lrc)
    if not lrc_path or not os.path.exists(lrc_path):
        ui_utils.show_status("File not found." if lrc_path else "Cancelled.")
        return

    entries = _parse_lrc_file(lrc_path)
    if not entries:
        ui_utils.show_status("No usable lines in LRC file.")
        return

    timed = [(text, ts) for text, ts in entries if ts is not None]

    if timed:
        sylt_data = [(text, int(ts)) for text, ts in timed if text]
        if not sylt_data:
            if tag_id.startswith('SYLT'):
                ui_utils.show_status("LRC has no timestamps for SYLT import.")
                return
        else:
            save_sylt_entries(file_path, sylt_data)
            ui_utils.show_status(f"Imported {len(sylt_data)} lines to SYLT.")
            return

    if tag_id.startswith('SYLT'):
        ui_utils.show_status("LRC has no timestamps; cannot import to SYLT.")
        return

    uslt_text = "\n".join(text for text, _ in entries if text)
    if uslt_text.strip():
        audio.delall('USLT')
        audio.add(USLT(encoding=3, lang='eng', desc='', text=uslt_text))
        audio.save(v2_version=3)
        ui_utils.show_status("Imported to USLT.")


def _edit_apic_tag(audio_obj: ID3, tag_name: str, apic_frame: APIC) -> bool:
    view_mode = "viu"
    cols = get_terminal_width()

    def _apic_header() -> list[str]:
        art_width = min(round(get_terminal_height()*1.5),get_terminal_width())
        art = _convert_apic_to_viu(apic_frame, width=art_width)
        image, mime, img_data = _get_image_from_apic(apic_frame)
        h = w = 0
        if image is not None:
            h, w = image.shape[:2]
        kb = len(img_data) / 1024
        info = f"{w}×{h}px  {mime}  {kb:.0f} KB"

        lines = [
            f"  {C.BOLD}{tag_name}{C.RESET}",
            f"  {C.DIM}{info}{C.RESET}",
            f"{C.DIM}{'─' * cols}{C.RESET}"
        ]

        if view_mode == "viu":
            lines.extend(art.splitlines())
        elif view_mode == "info":
            if image is not None:
                h, w = image.shape[:2]
                channels = image.shape[2] if len(image.shape) == 3 else 1
                color_mode = {1: "Grayscale", 3: "RGB", 4: "RGBA"}.get(channels, f"{channels}ch")
                size_kb = len(getattr(apic_frame, 'data', b"")) / 1024
                lines += [
                    f"  Description : {getattr(apic_frame, 'desc', '') or '(none)'}",
                    f"  Dimensions  : {w} × {h} px",
                    f"  Color mode  : {color_mode}",
                    f"  File size   : {size_kb:.1f} KB",
                ]

        lines.append(f"{C.DIM}{'─' * cols}{C.RESET}")
        return lines

    while True:
        actions = []
        if view_mode != "viu":
            actions.append("View Art")
        if view_mode != "info":
            actions.append("View Info")
        actions.extend(["Open Preview", "Replace", "Edit Description"])

        action = prompt.select("Action:", choices=actions, header=_apic_header)

        if action == "View Art":
            view_mode = "viu"
        elif action == "View Info":
            view_mode = "info"
        elif action == "Open Preview":
            if _open_apic_preview(apic_frame):
                ui_utils.show_status("Opening...")
            else:
                ui_utils.show_status("Could not open preview.")
        elif action == "Replace":
            img_path = prompt.path("Path to new image:")
            if img_path and os.path.isfile(img_path):
                try:
                    with open(img_path, 'rb') as f:
                        new_data = f.read()
                    ext = os.path.splitext(img_path)[1].lower()
                    mime = _EXT_TO_MIME.get(ext, 'image/jpeg')
                    new_frame = create_apic_frame(
                        new_data, mime, 3,
                        getattr(apic_frame, 'desc', '')
                    )
                    if new_frame is not None:
                        audio_obj.delall(tag_name)
                        audio_obj.add(new_frame)
                        apic_frame = new_frame
                    ui_utils.show_status("Image replaced.")
                except (OSError, IOError) as e:
                    ui_utils.show_status(f"Error: {e}")
            elif img_path:
                ui_utils.show_status("File not found.")
        elif action == "Edit Description":
            new_desc = prompt.text(
                f"Description:",
                default=getattr(apic_frame, 'desc', '')
            )
            if new_desc is not None:
                apic_frame.desc = new_desc
                ui_utils.show_status("Updated.")
        elif not action:
            audio_obj.save(v2_version=3)
            return True


def inspect_tag_loop(
    file_path: str,
    library_metadata: dict | None = None,
    library: list | None = None
) -> None:
    show_xml = False

    # Duration is constant for the file — compute it ONCE here, not per render.
    # (The header is redrawn on every keypress/resize; reading the file each
    # time made resizing feel sluggish.)
    _cached_dur = 0.0
    try:
        _cached_dur = float((library_metadata or {}).get("duration") or 0.0)
    except (TypeError, ValueError):
        _cached_dur = 0.0
    if not _cached_dur:
        try:
            import mutagen
            _mf = mutagen.File(file_path)  # type: ignore[reportPrivateImportUsage]
            if _mf is not None and getattr(_mf, "info", None) is not None:
                _cached_dur = float(getattr(_mf.info, "length", 0.0) or 0.0)
        except Exception:
            _cached_dur = 0.0

    def _save(audio_obj):
        audio_obj.save(v2_version=3)
        if library is not None:
            try:
                fresh = refresh_library_entry(library, file_path)
                if library_metadata is not None:
                    library_metadata.update(fresh)
            except (OSError, KeyError) as e:
                ui_utils.show_status(f"Warning: cache update failed: {e}")

    def _main_header() -> list[str]:
        cols = ui_utils.get_terminal_width()
        ext = os.path.splitext(file_path)[1].upper().lstrip('.')

        try:
            size_str = f"  {os.path.getsize(file_path) / (1024*1024):.1f} MB"
        except OSError:
            size_str = ""

        dur_str = f"  {ui_utils.format_time(int(_cached_dur))}" if _cached_dur else ""

        # Prefer real tags over the (possibly cryptic) filename. Read live from
        # the loaded ID3 object, falling back to the cached library metadata,
        # and only to the filename when there is no title at all.
        meta = library_metadata or {}
        _PLACEHOLDERS = {"", "Unknown Artist", "Unknown Album", "Unknown Genre", "Unknown Year"}

        def _from_tags(frame: str, meta_key: str) -> str:
            try:
                fr = audio.get(frame)
                if fr is not None and getattr(fr, "text", None):
                    val = str(fr.text[0]).strip()
                    if val:
                        return val
            except Exception:
                pass
            val = str(meta.get(meta_key, "")).strip()
            return "" if val in _PLACEHOLDERS else val

        title = _from_tags("TIT2", "title")
        artist = _from_tags("TPE1", "artist") or _from_tags("TPE2", "album_artist")
        if not title:
            title = os.path.splitext(os.path.basename(file_path))[0]

        # Full-width rounded box: bold title (· dim artist) left, dim meta right.
        mh = ui_utils.MARGIN_H
        inner = max(12, cols - 2 * mh - 4)
        right = f"[{ext}]{dur_str}{size_str}"
        avail = max(4, inner - len(right) - 2)

        if len(title) > avail:
            title = title[:avail - 1] + "…"
        left_styled = f"{C.BOLD}{title}{C.RESET}"
        left_vis = len(title)
        rem = avail - left_vis
        if artist and rem > 5:
            suffix = f" · {artist}"
            if len(suffix) > rem:
                suffix = suffix[:rem - 1] + "…"
            left_styled += f"{C.DIM}{suffix}{C.RESET}"
            left_vis += len(suffix)

        gap = max(1, inner - left_vis - len(right))
        title_line = f"{left_styled}{' ' * gap}{C.DIM}{right}{C.RESET}"

        lines = [
            f"{' ' * mh}{C.DIM}╭{'─' * (inner + 2)}╮{C.RESET}",
            f"{' ' * mh}{C.DIM}│{C.RESET} {title_line} {C.DIM}│{C.RESET}",
            f"{' ' * mh}{C.DIM}╰{'─' * (inner + 2)}╯{C.RESET}",
            "",
        ]

        xml_data = library_metadata.get('xml_data') if library_metadata else None
        has_id3 = file_path.lower().endswith('.mp3')
        if xml_data and (show_xml or not has_id3):
            lines.extend(ui_utils.get_xml_metadata_lines(xml_data))

        return lines

    while True:
        try:
            audio = ID3(file_path)
        except mutagen.id3.ID3NoHeaderError:  # type: ignore[reportPrivateImportUsage]
            if file_path.lower().endswith('.mp3'):
                audio = ID3()  # untagged MP3 — start fresh so tags can be added
            else:
                ui_utils.show_status("Tag editing is only supported for MP3 files.", duration=4.0)
                break
        except OSError as e:
            ui_utils.show_status(f"Could not open file: {e}", duration=4.0)
            break
        tags = sorted(audio.keys())

        cols = ui_utils.get_terminal_width()

        def _tag_cells(tag_id: str) -> list:
            # Column 1 = TAG (bright) + friendly name (dim) as two segments.
            info = get_tag_info(tag_id)
            friendly = f" ({info.name[0]})" if info else ""
            category = get_tag_category(tag_id)
            val = summarize_tag_value(tag_id, audio[tag_id])
            return [[(display_tag_id(tag_id), 'primary'), (friendly, 'dynamic-dim')], category, val]

        # Read-only filesystem path row — "⌁ File path" white (bold when active),
        # "(filesystem)" dimmed, both in column 1 (#36).
        filepath_row = prompt.Choice(
            title="File path", value="__filepath__",
            cells=[[("⌁ File path", 'primary'), (" (filesystem)", 'dynamic-dim')], "", ""],
        )
        tag_choices = [filepath_row, prompt.separator()] + [
            prompt.Choice(title=t, value=t, cells=_tag_cells(t)) for t in tags
        ]
        xml_data = library_metadata.get('xml_data') if library_metadata else None
        has_id3 = file_path.lower().endswith('.mp3')

        extras = (["Toggle XML"] if xml_data and has_id3 else [])

        _shortcuts = {'a': 'Add Tag'} if has_id3 else None
        _extra_hints = {'a': 'add tag'} if has_id3 else None

        choice = prompt.select(
            "Select tag to manage:",
            choices=tag_choices + extras,
            header=_main_header,
            shortcuts=_shortcuts,
            extra_hints=_extra_hints,
            columns=_TAG_COLUMNS,
        )

        if choice == "Toggle XML":
            show_xml = not show_xml
            continue

        if choice == "__filepath__":
            _fp_header = [
                f"  {C.BOLD}File path{C.RESET}  {C.DIM}(filesystem location — not stored in ID3){C.RESET}",
                f"{C.DIM}{'─' * cols}{C.RESET}",
                f"  {file_path}",
                f"{C.DIM}{'─' * cols}{C.RESET}",
            ]
            fp_action = prompt.select(
                "Action:",
                choices=["Copy path to clipboard"],
                header=_fp_header,
            )
            if fp_action == "Copy path to clipboard":
                pyperclip.copy(file_path)
                ui_utils.show_status("File path copied.")
            continue

        if choice == "Add Tag":
            tag_id = prompt.text("Tag ID (e.g. TPE2, TXXX:Transcription:eng, COMM::fre):")
            if not tag_id:
                continue

            # Check if it's a known category base via our new parser
            base_id, _, _ = parse_composite_tag_id(tag_id)
            info = get_tag_info(base_id)

            if info:
                value = prompt_for_value(base_id, current_value=_prompt_sort_order(base_id, audio))
            else:
                value = prompt.text(f"Value for {tag_id}:")

            if value is not None:
                new_frame = create_frame(tag_id, value)
                if new_frame:
                    audio.add(new_frame)
                    _save(audio)
                    ui_utils.show_status(f"Added {tag_id}.")
                else:
                    ui_utils.show_status(f"Could not create frame for {tag_id}.")
            continue

        if not choice:
            break

        # Edit tag
        while True:
            audio = ID3(file_path)
            if choice not in audio:
                break

            raw_val = audio[choice]
            category = get_tag_category(choice)

            def _tag_header() -> list[str]:
                cols = ui_utils.get_terminal_width()
                info = get_tag_info(choice) if choice else None
                label = info.name[0] if info else choice or "Unknown"

                lines = [
                    f"  {C.BOLD}{display_tag_id(choice or '')}{C.RESET}  {C.DIM}({label}){C.RESET}",
                    f"{C.DIM}{'─' * cols}{C.RESET}",
                ]

                if category == 'image':
                    art_width = min(round(get_terminal_height()*1.5),get_terminal_width())
                    art = _convert_apic_to_viu(raw_val, width=art_width)
                    lines.extend(art.splitlines())
                    lines.append(f"{C.DIM}{'─' * cols}{C.RESET}")

                if category == 'people':
                    people = getattr(raw_val, 'people', [])
                    cw = max(12, (cols - 6) // 2)
                    lines.append(f"  {C.DIM}{'ROLE':<{cw}}  NAME{C.RESET}")
                    lines.append(f"  {'─' * cw}  {'─' * (cols - cw - 4)}")
                    for role, name in people[:8]:
                        r = ui_utils.truncate_text(role, cw)
                        n = ui_utils.truncate_text(name, cols - cw - 4)
                        lines.append(f"  {r:<{cw}}  {n}")
                    if len(people) > 8:
                        lines.append(f"  {C.DIM}… +{len(people) - 8} more{C.RESET}")
                    lines.append(f"{C.DIM}{'─' * cols}{C.RESET}")

                return lines

            # Action selection
            actions = ["Copy", "Paste", "Edit", "Rename", "Delete"]
            if category in ('lyrics',) and choice.startswith(('USLT', 'SYLT')):
                actions.insert(0, "Import LRC")
            if category == 'image':
                actions.remove("Edit")
                actions.insert(0, "Manage")
            if choice.startswith('SYLT'):
                actions.remove("Edit")

            action = prompt.select("Action:", choices=actions, header=_tag_header)

            if action == "Manage" and category == 'image':
                if _edit_apic_tag(audio, choice, raw_val):
                    _save(audio)
                break

            elif action == "Import LRC":
                _import_from_lrc(file_path, audio, choice)
                break

            elif action == "Copy":
                if category == 'people':
                    text = "\n".join(f"{r}: {n}" for r, n in getattr(raw_val, 'people', []))
                else:
                    text = summarize_tag_value(choice, raw_val)
                pyperclip.copy(text)
                ui_utils.show_status("Copied to clipboard.")

            elif action == "Paste":
                clipboard = pyperclip.paste()
                if clipboard and prompt.confirm(f"Replace {choice}?"):
                    audio.delall(choice)
                    new_frame = create_frame(choice, clipboard)
                    if new_frame:
                        audio.add(new_frame)
                        _save(audio)
                        ui_utils.show_status("Updated.")
                    else:
                        ui_utils.show_status("Could not create frame - wrong data type for this tag.")

            elif action == "Rename":
                new_id = prompt.text("New tag ID:")
                if new_id and new_id != choice:
                    old_frame = audio.pop(choice)
                    if rename_frame(audio, old_frame, new_id):
                        _save(audio)
                        ui_utils.show_status(f"Renamed to {new_id}.")
                    else:
                        audio.add(old_frame)
                        ui_utils.show_status("Rename failed.")
                    break

            elif action == "Edit":
                current_frame = audio.get(choice)
                new_value = prompt_for_value(choice, current_value=current_frame)
                if new_value is not None:
                    new_frame = create_frame(choice, new_value)
                    if new_frame:
                        audio.add(new_frame)
                        _save(audio)
                        ui_utils.show_status("Updated.")
                    else:
                        ui_utils.show_status("Could not create frame - check data format.")
                    break

            elif action == "Delete":
                if prompt.confirm(f"Delete {choice}?"):
                    try:
                        audio.pop(choice)
                        _save(audio)
                        ui_utils.show_status(f"Deleted {choice}.")
                    except KeyError:
                        ui_utils.show_status(f"Could not delete {choice}.")
                    break

            elif not action:
                break
