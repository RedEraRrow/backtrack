"""ID3 frame creation, value prompts, and bulk-operation helpers."""
from __future__ import annotations
from typing import Any, Optional
from mutagen.id3 import ID3
from mutagen.id3._frames import SYLT, USLT, TMCL, TIPL, TXXX, WXXX, COMM  # type: ignore[reportPrivateImportUsage]
from mutagen.id3._frames import APIC, EQU2, RVA2, POPM, PCNT, RBUF
from mutagen.id3._frames import Frame  # type: ignore[reportPrivateImportUsage]

from src.id3.tag_registry import (parse_composite_tag_id, get_tag_info, get_tag_category, get_preferred_tag_name)
from src.music_library import refresh_library_entry, format_value_list

import mutagen.id3
import os
import re
import time
from src.utils import prompt, ui_utils



_EXT_TO_MIME: dict[str, str] = {
    '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg',
    '.png': 'image/png', '.gif': 'image/gif',
    '.bmp': 'image/bmp', '.webp': 'image/webp',
}


def _prompt_for_image_file() -> bytes | None:
    """Prompt for an image path and return its raw bytes, or None if cancelled/unreadable."""
    img_path = prompt.path("Path to image:")
    if not img_path or not os.path.isfile(img_path):
        return None

    try:
        with open(img_path, 'rb') as f:
            return f.read()
    except Exception:
        return None


def _get_mime_type(data: bytes) -> str:
    """Sniff an image's MIME type from its magic bytes; defaults to JPEG."""
    if data.startswith(b'\xFF\xD8\xFF'):
        return 'image/jpeg'
    elif data.startswith(b'\x89PNG'):
        return 'image/png'
    elif data.startswith(b'GIF8'):
        return 'image/gif'
    elif data.startswith(b'BM'):
        return 'image/bmp'
    elif data.startswith(b'RIFF') and b'WEBP' in data[:12]:
        return 'image/webp'
    return 'image/jpeg'


# Sentinel returned by pick_nearby_cover when the user chooses "no art here".
CLEAR_COVER = object()

_COVER_PICK_COLUMNS = [
    prompt.Column(style='primary', flex=True),                          # image name
    prompt.Column(style='dynamic-dim', align='right', max_width=10, priority=1),  # size — drops first when narrow
    prompt.Column(style='dynamic-dim', align='right', pin=True),  # confidence / current (kept)
]


def _cover_size(path: str) -> str:
    """Human-readable file size ("N KB"/"N.N MB") for a cover image, or '' on error."""
    try:
        kb = os.path.getsize(path) / 1024
    except OSError:
        return ''
    return f"{kb/1024:.1f} MB" if kb >= 1024 else f"{kb:.0f} KB"


def _cover_label(image_path: str, track_dir: str) -> str:
    """Image name, prefixed with its subfolder when it isn't beside the track."""
    d = os.path.dirname(os.path.abspath(image_path))
    if os.path.abspath(track_dir) != d:
        return f"{os.path.basename(d)}/{os.path.basename(image_path)}"
    return os.path.basename(image_path)


def pick_nearby_cover(file_path: str, *, tokens: dict | None = None,
                      images: list | None = None, current: str | None = None,
                      allow_none: bool = False, header=None,
                      title: str | None = None):
    """Ranked picker of cover images sitting next to ``file_path``.

    Lists the candidate images best-guess first (an exact name match, then a
    track-number match, then a title match), with a size and confidence hint,
    plus a "Type a path…" escape. Returns the chosen image *path*, ``None`` if
    cancelled, or :data:`CLEAR_COVER` when ``allow_none`` and the user picks
    "no art for this file". Falls straight through to a manual path prompt when
    there are no nearby images.
    """
    from src.id3 import cover_matcher as cm
    from src.id3 import file_namer as fnm

    track_dir = os.path.dirname(os.path.abspath(file_path))
    if tokens is None:
        tokens = fnm.read_tokens(file_path) if fnm.is_supported(file_path) else {}
    if images is None:
        images = cm.find_images_for_track(file_path)

    def _manual() -> str | None:
        """Fall back to a free-typed image path when no candidates are shown/chosen."""
        p = prompt.path("Path to image:")
        if p and os.path.isfile(p):
            return p
        if p:
            ui_utils.show_status("File not found.")
        return None

    if not images:
        return _manual()

    ranked = cm.rank_candidates(file_path, tokens, images)
    choices: list = []
    cur_idx = 0
    for i, (score, img) in enumerate(ranked):
        hint = cm.confidence(score)
        if current and os.path.abspath(img) == os.path.abspath(current):
            hint = 'current'
            cur_idx = i
        choices.append(prompt.Choice(
            title=os.path.basename(img), value=img,
            cells=[_cover_label(img, track_dir), _cover_size(img), hint]))
    choices.append(prompt.separator())
    choices.append(prompt.Choice(title="Type a path…", value='__manual__',
                                 cells=["Type a path…", '', '']))
    if allow_none:
        choices.append(prompt.Choice(title="No art for this file", value='__none__',
                                     cells=["No art for this file", '', '']))

    msg = title or "Album art — top row is the best guess:"
    hdr = header(ui_utils.plural(len(ranked), "candidate")) if header else None
    sel = prompt.select(msg, choices=choices, columns=_COVER_PICK_COLUMNS,
                        header=hdr, index=cur_idx)
    if sel is None:
        return None
    if sel == '__manual__':
        return _manual()
    if sel == '__none__':
        return CLEAR_COVER
    return sel


# APIC picture types offered when embedding art: (ID3 type byte, label).
_PICTURE_TYPES = [(3, "Cover (front)"), (4, "Cover (back)"),
                  (8, "Artist"), (0, "Other")]

_IMAGE_META_COLUMNS = [
    prompt.Column(style='primary'),                                   # type + [n]
    prompt.Column(style='dynamic-dim', flex=True, priority=1),        # description
]


def _prompt_for_picture_type(*, initial: int = 3, header=None) -> int | None:
    """Pick just the APIC picture type. Returns the type byte, or None if cancelled."""
    choices, index = [], 0
    for i, (pt, label) in enumerate(_PICTURE_TYPES):
        if pt == initial:
            index = i
        choices.append(prompt.Choice(f"{label}  [{pt}]", pt))
    sel = prompt.select("Picture type:", choices=choices, index=index,
                        header=header() if header else None)
    return sel if isinstance(sel, int) else None


def _prompt_for_image_metadata(*, initial_type: int = 3, initial_desc: str = '',
                               header=None) -> tuple[int, str] | None:
    """Pick the picture type and its description on ONE screen.

    Prefilled with Cover (front) and no description — the overwhelmingly common
    case — so Enter accepts immediately instead of walking the user through a
    type screen and then a description screen for every single image.  `d` edits
    the description in place, without leaving the list.

    Returns (pic_type, description) or None if cancelled.
    """
    desc = initial_desc

    def _desc_cell() -> str:
        """The description column's text, or a placeholder when it's blank."""
        return desc if desc else "— no description —"

    choices, index = [], 0
    for i, (pt, label) in enumerate(_PICTURE_TYPES):
        if pt == initial_type:
            index = i
        choices.append(prompt.Choice(f"{label}  [{pt}]", pt,
                                     cells=[f"{label}  [{pt}]", _desc_cell()]))

    def _edit_desc(_row) -> None:
        """Prompt for a description and restamp it onto every row's cells."""
        nonlocal desc
        new_desc = prompt.text("Description (blank for none):", default=desc)
        if new_desc is None:
            return
        desc = new_desc.strip()
        for ch in choices:
            ch.cells[1] = _desc_cell()

    sel = prompt.select("Picture type:",
                        choices=choices, columns=_IMAGE_META_COLUMNS, index=index,
                        header=header() if header else None,
                        row_actions={'d': _edit_desc},
                        row_action_hints={'d': 'description'})
    if not isinstance(sel, int):
        return None
    return sel, desc


# "Various Artists" is *derived*, never stored: the app works it out from the
# tracks (see music_library.derive_album_credit) and shows it wherever a
# compilation has no single artist. Writing it into a tag turns an inference into
# data that then has to be maintained — and it hides the real per-track artists.
# The compilation flag (TCMP) is the thing worth storing.
_PLACEHOLDER_NAMES = frozenset({
    'various artists', 'various', 'va', 'v.a.', 'v/a', 'unknown', 'unknown artist',
    'unknown album', 'no artist', 'n/a', 'none',
})
# Fields this applies to: names of people or acts, and their sort tags.
_NAME_FIELD_IDS = ('TPE1', 'TPE2', 'TPE3', 'TPE4', 'TCOM', 'TSOP', 'TSO2', 'TSOC')


def is_placeholder_name(value: Any) -> bool:
    """True for a name the app derives rather than stores ("Various Artists")."""
    return str(value or '').strip().lower() in _PLACEHOLDER_NAMES


def strip_placeholder_names(tag_id: str, values: list[str]) -> list[str]:
    """Drop derived-only names from a name field's values (other fields pass through)."""
    base = str(tag_id or '').split(':')[0].upper()
    if base not in _NAME_FIELD_IDS:
        return values
    return [v for v in values if not is_placeholder_name(v)]


def _as_value_list(value: Any) -> list[str]:
    """Normalise a text-frame value into a list of non-empty strings.

    A plain string becomes a one-element list; a list (multi-value, #60) is
    stripped of blank entries. Order is preserved."""
    if isinstance(value, (list, tuple)):
        return [s for s in (str(v).strip() for v in value) if s]
    s = str(value).strip()
    return [s] if s else []


def _has_multivalue(audio: ID3) -> bool:
    """True if any text frame in `audio` holds more than one value.

    People frames (TMCL/TIPL) are skipped — their `.text` is a flat role/name
    pair list, not a multi-value text field."""
    for frame in audio.values():
        if getattr(frame, 'people', None) is not None:
            continue
        txt = getattr(frame, 'text', None)
        if isinstance(txt, list) and len(txt) > 1:
            return True
    return False


def save_id3(audio: ID3, path: str | None = None) -> None:
    """Save an ID3 tag, choosing the version by content.

    ID3v2.3 collapses multi-value text into one '/'-joined string (corrupting
    values that contain '/'), so files carrying any multi-value frame are saved
    as v2.4. Single-value files stay v2.3 for maximum player compatibility."""
    ver = 4 if _has_multivalue(audio) else 3
    if path is None:
        audio.save(v2_version=ver)
    else:
        audio.save(path, v2_version=ver)


def create_frame(tag_id: str, value: Any) -> Frame | None:
    """Create the correct mutagen frame for tag_id from value, or None on failure."""
    if value is None:
        return None

    info = get_tag_info(tag_id)
    if not info:
        return None

    parsed_base, parsed_desc, parsed_lang = parse_composite_tag_id(tag_id)

    try:
        # Audio-adjustment frames carry structured payloads from their own
        # editors (see _prompt_for_equalisation / _prompt_for_rva2).
        if parsed_base == 'EQU2':
            if isinstance(value, dict) and value.get('__eq__'):
                return EQU2(method=0, desc='', adjustments=list(value.get('adjustments', [])))
            return None

        if parsed_base == 'RVA2':
            if isinstance(value, dict) and value.get('__rva2__'):
                return RVA2(desc='', channel=1, gain=float(value['gain']), peak=0.0)
            return None

        # POPM (rating 0-255 + play count) and PCNT (play counter) carry structured
        # payloads from their editors; a bare int is also accepted for convenience.
        if parsed_base == 'POPM':
            if isinstance(value, dict) and value.get('__popm__'):
                return POPM(email=str(value.get('email') or DEFAULT_POPM_EMAIL),
                            rating=int(value.get('rating', 0)), count=int(value.get('count', 0)))
            return None
        if parsed_base == 'PCNT':
            if isinstance(value, dict) and value.get('__pcnt__'):
                return PCNT(count=int(value.get('count', 0)))
            if isinstance(value, int):
                return PCNT(count=max(0, value))
            return None
        if parsed_base == 'RBUF':
            if isinstance(value, dict) and value.get('__rbuf__'):
                return RBUF(size=int(value.get('size', 0)))
            if isinstance(value, int):
                return RBUF(size=max(0, value))
            return None

        # APIC is a BINARY frame but its UI category is 'image'. (The old
        # `frame_type == 'IMAGE'` check never matched, so APICs never saved.)
        if info.ui_category == 'image':
            if isinstance(value, dict) and value.get('__image__'):
                data = value.get('data') or b''
                if not isinstance(data, bytes) or not data:
                    return None
                return APIC(encoding=3, mime=_get_mime_type(data),
                            type=int(value.get('type', 3)), desc=str(value.get('desc', '')),
                            data=data)
            if isinstance(value, bytes) and len(value) > 0:
                return APIC(encoding=3, mime=_get_mime_type(value), type=3, desc='', data=value)
            return None

        if info.frame_type == 'TEXT':
            frame_class = info.mutagen_class

            # COMM/USLT carry a single (possibly multi-line) body from the
            # system-editor path — keep them scalar.
            if frame_class in [COMM, USLT]:
                text_val = str(value).strip()
                if not text_val:
                    return None
                clean_lang = str(parsed_lang).strip() if parsed_lang else 'eng'
                if len(clean_lang) != 3:
                    clean_lang = 'eng'
                return frame_class(encoding=3, lang=clean_lang, desc=parsed_desc, text=text_val)

            # Multi-value text frames (#60): value may be a list of strings.
            vals = strip_placeholder_names(tag_id, _as_value_list(value))
            if not vals:
                return None

            if frame_class in [TXXX, WXXX]:
                return frame_class(encoding=3, desc=parsed_desc, text=vals)

            return frame_class(encoding=3, text=vals)

        elif info.frame_type == 'URL':
            # W*** frames store a single URL; WXXX also carries a description.
            url_val = str(value).strip()
            if not url_val:
                return None
            if info.mutagen_class is WXXX:
                return WXXX(encoding=3, desc=parsed_desc, url=url_val)
            return info.mutagen_class(url=url_val)

        elif info.format_spec == 'ISO8601':
            date_val = str(value).strip()
            if not date_val or not any(c.isdigit() for c in date_val):
                return None
            return info.mutagen_class(encoding=3, text=[date_val])

        elif info.frame_type == 'FRACTIONAL':
            frac_val = str(value).strip()
            if not frac_val or not any(c.isdigit() for c in frac_val):
                return None
            return info.mutagen_class(encoding=3, text=[frac_val])

        elif info.frame_type == 'NUMERIC':
            num_val = str(value).strip()
            if not num_val or not any(c.isdigit() for c in num_val):
                return None
            return info.mutagen_class(encoding=3, text=[num_val])

        elif info.frame_type == 'LIST':
            # People frames (TMCL/TIPL): a list of (role, name) pairs.
            if info.ui_category == 'people':
                if isinstance(value, list) and value:
                    people_list = [(str(item[0]).strip(), str(item[1]).strip())
                                   for item in value
                                   if isinstance(item, (tuple, list)) and len(item) == 2]
                    if not people_list:
                        return None
                    return info.mutagen_class(encoding=3, people=people_list)
                return None
            # Text-list frames (genre TCON, language TLAN): a plain string or,
            # for multi-value (#60), a list of strings.
            vals = _as_value_list(value)
            if not vals:
                return None
            return info.mutagen_class(encoding=3, text=vals)

        elif info.frame_type == 'DATE':
            val = str(value).strip()
            if '-' in val:
                parts = val.split('-')
                if len(parts) == 3:
                    val = parts[2].zfill(2) + parts[1].zfill(2)
            if not val or not any(c.isdigit() for c in val):
                return None
            return info.mutagen_class(encoding=3, text=[val])

        elif info.frame_type == 'YEAR':
            val = str(value).strip()
            if '-' in val:
                val = val.split('-')[0]
            if not val or not any(c.isdigit() for c in val):
                return None
            return info.mutagen_class(encoding=3, text=[val])

        elif info.frame_type == 'TIME':
            val = str(value).strip()
            if ':' in val:
                parts = val.split(':')
                val = parts[0].zfill(2) + (parts[1].zfill(2) if len(parts) > 1 else "00")
            if not val or not any(c.isdigit() for c in val):
                return None
            return info.mutagen_class(encoding=3, text=[val])

        # SYLT and other unhandled kinds have no create path (edited elsewhere).
        return None

    except (ValueError, TypeError, AttributeError):
        return None


def create_apic_frame(data: bytes, mime: str = '', pic_type: int = 3, desc: str = '') -> APIC | None:
    """
    Create APIC (album art) frame with explicit metadata.
    Falls back to magic-byte detection if mime not provided.
    """
    if not isinstance(data, bytes) or len(data) == 0:
        return None

    if not mime:
        mime = _get_mime_type(data)

    if not isinstance(pic_type, int):
        pic_type = 3

    try:
        return APIC(encoding=3, mime=mime, type=pic_type, desc=desc, data=data)
    except Exception:
        return None


def rename_frame(audio_obj: ID3, old_frame, new_id: str) -> bool:
    """Rename a frame while preserving type and value.

    Callers pop `old_frame` out of `audio_obj` before calling, so the old id is
    read from the frame itself (`HashKey`/`FrameID`) rather than searched for in
    the object (which no longer holds it). The new frame is added on success; on
    failure the caller re-adds the original.
    """
    old_id = getattr(old_frame, 'HashKey', None) or getattr(old_frame, 'FrameID', None)
    if not old_id:
        return False

    old_info = get_tag_info(old_id)
    new_info = get_tag_info(new_id)
    if not old_info or not new_info or old_info.ui_category != new_info.ui_category:
        return False

    if old_info.ui_category == 'image':
        return False

    if hasattr(old_frame, 'people'):
        value = old_frame.people
    elif hasattr(old_frame, 'text'):
        # Preserve every value for multi-value frames, not just the first.
        value = list(old_frame.text) if len(old_frame.text) > 1 else (
            old_frame.text[0] if old_frame.text else None)
    elif hasattr(old_frame, 'url'):
        value = old_frame.url
    else:
        value = str(old_frame)

    if value is None:
        return False

    new_frame = create_frame(new_id, value)
    if not new_frame:
        return False

    try:
        audio_obj.pop(old_id, None)   # normally already popped by the caller
        audio_obj.add(new_frame)
        return True
    except (AttributeError, KeyError):
        return False


def _prompt_for_equalisation(current_value: Any) -> dict | None:
    """Interactive graphic equaliser for an EQU2 frame."""
    existing: list[tuple[float, float]] = []
    if current_value is not None and hasattr(current_value, 'adjustments'):
        existing = [(float(freq), float(gain)) for freq, gain in current_value.adjustments]

    adjustments = prompt.equaliser_edit("Equalisation — boost/cut per frequency band:", existing)
    if adjustments is None:
        return None
    return {'__eq__': True, 'adjustments': sorted(adjustments)}


def _prompt_for_rva2(current_value: Any) -> dict | None:
    """Interactive gain meter for a single-channel RVA2 frame."""
    cur = float(current_value.gain) if (current_value is not None and hasattr(current_value, 'gain')) else 0.0
    gain = prompt.rva2_edit("Volume adjustment (master channel):", gain=cur)
    if gain is None:
        return None
    return {'__rva2__': True, 'gain': gain}


# POPM rating: 0-5 stars ↔ 0-255 byte (Windows Media Player convention). Writing
# uses the canonical byte per star; reading maps any byte to the nearest star via
# WMP's boundaries so ratings written by other players display sensibly.
_POPM_STAR_BYTES = (0, 1, 64, 128, 196, 255)
DEFAULT_POPM_EMAIL = "Windows Media Player 9 Series"


def popm_stars_to_byte(stars: int) -> int:
    """Map a 0-5 star rating to its canonical WMP POPM byte value."""
    return _POPM_STAR_BYTES[max(0, min(5, int(stars)))]


def popm_byte_to_stars(rating: int) -> int:
    """Map any POPM byte rating to the nearest 0-5 stars, per WMP's boundaries."""
    r = int(rating)
    if r <= 0:
        return 0
    if r < 32:
        return 1
    if r < 96:
        return 2
    if r < 160:
        return 3
    if r < 224:
        return 4
    return 5


def _prompt_for_rating(current_value: Any) -> dict | None:
    """Star-rating editor (0-5) + play count + rater email for a POPM frame."""
    stars = count = 0
    email = DEFAULT_POPM_EMAIL
    if current_value is not None and hasattr(current_value, 'rating'):
        stars = popm_byte_to_stars(getattr(current_value, 'rating', 0) or 0)
        count = int(getattr(current_value, 'count', 0) or 0)
        email = str(getattr(current_value, 'email', '') or DEFAULT_POPM_EMAIL)
    res = prompt.rating_edit("Rating (POPM):", stars=stars, count=count, email=email)
    if res is None:
        return None
    return {'__popm__': True, 'rating': popm_stars_to_byte(res['stars']),
            'count': int(res['count']), 'email': str(res['email'] or DEFAULT_POPM_EMAIL)}


def _prompt_for_playcount(current_value: Any) -> dict | None:
    """Simple non-negative integer editor for a PCNT play-counter frame."""
    cur = int(getattr(current_value, 'count', 0) or 0) if current_value is not None else 0
    n = prompt.number_edit("Play count (PCNT):", value=cur, minimum=0)
    if not isinstance(n, int):          # None (cancel) or MODE_TOGGLE
        return None
    return {'__pcnt__': True, 'count': int(n)}


def _current_text(current_value: Any) -> str:
    """The current single value as a string, from a mutagen frame or a raw value."""
    if current_value is None:
        return ''
    if hasattr(current_value, 'text') and current_value.text:
        return str(current_value.text[0])
    return str(current_value)


# Musical keys for TKEY (ID3: ground keys A–G, ♯, minor 'm', off-key 'o').
# The 12 chromatic notes: (ID3 value with '#', pretty display with ♯, flat enharmonic).
# The tag stores plain ASCII ('C#'); the picker shows the ♯ glyph and full names.
_KEY_NOTES = (
    ('C', 'C', None), ('C#', 'C♯', 'D♭'), ('D', 'D', None), ('D#', 'D♯', 'E♭'),
    ('E', 'E', None), ('F', 'F', None), ('F#', 'F♯', 'G♭'), ('G', 'G', None),
    ('G#', 'G♯', 'A♭'), ('A', 'A', None), ('A#', 'A♯', 'B♭'), ('B', 'B', None),
)

_KEY_COLUMNS = [
    prompt.Column(style='primary', min_width=4),        # pretty key code
    prompt.Column(style='dynamic-dim', flex=True),      # full readable name
]

# Common TMED media types: (label shown, code stored). Codes follow the ID3v2.4
# media-type table; a custom entry is always available for anything unusual.
_MEDIA_TYPES = (
    ('CD', 'CD'), ('Digital / file', 'DIG'), ('Vinyl', 'VIN'), ('Cassette', 'MC'),
    ('DAT', 'DAT'), ('MiniDisc', 'MD'), ('DVD', 'DVD'), ('Laserdisc', 'LD'),
    ('Reel', 'REE'), ('Radio', 'RAD'), ('Television', 'TV'), ('Telephone', 'TEL'),
    ('Analogue (other)', 'ANA'),
)

_ISRC_RE = re.compile(r'^[A-Za-z]{2}[A-Za-z0-9]{3}\d{2}\d{5}$')


def _prompt_for_musical_key(current_value: Any) -> str | None:
    """Pick the initial musical key (TKEY) from a grouped, labelled picker (major /
    minor / other, with ♯ glyphs and full names), or type a custom value."""
    cur = _current_text(current_value)
    choices: list = [prompt.separator("Major")]
    for val, sym, flat in _KEY_NOTES:
        name = f"{sym} / {flat} major" if flat else f"{sym} major"
        choices.append(prompt.Choice(title=sym, value=val, cells=[sym, name]))
    choices.append(prompt.separator("Minor"))
    for val, sym, flat in _KEY_NOTES:
        name = f"{sym} / {flat} minor" if flat else f"{sym} minor"
        choices.append(prompt.Choice(title=f"{sym}m", value=f"{val}m", cells=[f"{sym}m", name]))
    choices.append(prompt.separator("Other"))
    choices.append(prompt.Choice(title="off-key", value="o", cells=["o", "off-key / atonal"]))
    choices.append(prompt.Choice(title="Type custom…", value="__custom__",
                                 cells=["Type custom…", "any ID3 key string"]))

    idx = next((i for i, c in enumerate(choices) if getattr(c, 'value', None) == cur), 0)
    sel = prompt.select("Initial key:", choices=choices, columns=_KEY_COLUMNS, index=idx)
    if sel is None:
        return None
    if sel == '__custom__':
        raw = prompt.text("Key (e.g. Dbm, F#, o):", default=cur)
        return raw or None
    return sel


def _prompt_for_media_type(current_value: Any) -> str | None:
    """Pick the media type (TMED) from common options, or type a custom code."""
    cur = _current_text(current_value)
    choices = [prompt.Choice(title=f"{label}  ({code})", value=code) for label, code in _MEDIA_TYPES]
    choices += [prompt.separator(), prompt.Choice(title='Type custom…', value='__custom__')]
    idx = next((i for i, (_, code) in enumerate(_MEDIA_TYPES) if code == cur), 0)
    sel = prompt.select("Media type:", choices=choices, index=idx)
    if sel is None:
        return None
    if sel == '__custom__':
        raw = prompt.text("Media-type code (e.g. CD, DIG, VIN/33):", default=cur)
        return raw or None
    return sel


def _prompt_for_isrc(current_value: Any) -> str | None:
    """ISRC (TSRC) with light validation: normalise to 12 chars and warn if the
    shape isn't CC-RRR-YY-NNNNN, but let the user save anyway."""
    cur = _current_text(current_value)
    raw = prompt.text("ISRC (CC-RRR-YY-NNNNN):", default=cur)
    if not raw:
        return None
    norm = re.sub(r'[\s-]', '', raw).upper()
    if not _ISRC_RE.match(norm):
        if not prompt.confirm(f"'{norm}' doesn't look like a valid ISRC — save anyway?"):
            return None
    return norm


def _prompt_for_compilation(current_value: Any) -> str | None:
    """Yes/No toggle for the TCMP compilation flag (stored as '1'/'0')."""
    cur = _current_text(current_value).strip()
    is_comp = cur not in ('', '0')
    sel = prompt.select("Part of a compilation (various-artists album)?",
                        choices=[prompt.Choice(title='Yes', value='1'),
                                 prompt.Choice(title='No', value='0')],
                        index=0 if is_comp else 1)
    return sel  # '1' / '0' / None


def _prompt_for_rbuf(current_value: Any) -> dict | None:
    """Numeric editor for the RBUF recommended-buffer-size frame (bytes)."""
    cur = int(getattr(current_value, 'size', 0) or 0) if current_value is not None else 0
    n = prompt.number_edit("Recommended buffer size (bytes):", value=cur, minimum=0)
    if not isinstance(n, int):
        return None
    return {'__rbuf__': True, 'size': int(n)}


def prompt_for_value(tag_id: str, current_value: Any = None, initial_people: list | None = None,
                     force_plain: bool | None = None, file_path: str | None = None) -> Any | None:
    """Prompt for a new value for tag_id, dispatching to the right editor for its category
    (structured binary frames, image picker, people list, plain/smart text, etc.)."""
    info = get_tag_info(tag_id)
    if not info:
        return None

    label = get_preferred_tag_name(tag_id)
    ui_cat = info.ui_category
    fmt = info.format_spec
    base_id, _, _ = parse_composite_tag_id(tag_id)

    # Structured binary-frame editors (no raw-text equivalent).
    if base_id == 'EQU2':
        return _prompt_for_equalisation(current_value)
    if base_id == 'RVA2':
        return _prompt_for_rva2(current_value)
    if base_id == 'POPM':
        return _prompt_for_rating(current_value)
    if base_id == 'PCNT':
        return _prompt_for_playcount(current_value)
    if base_id == 'RBUF':
        return _prompt_for_rbuf(current_value)
    if base_id == 'RVRB':
        ui_utils.show_status("Reverb (RVRB) isn't editable in Backtrack.")
        return None

    # Other structured BINARY frames (UFID/MCDI/GEOB/PRIV/ETCO/MLLT/SEEK/…) have
    # no meaningful free-text form and create_frame can't build them, so refuse
    # them here rather than accept a value the save path would silently drop.
    if info.frame_type == 'BINARY' and ui_cat not in ('image', 'audio adjustment', 'lyrics'):
        ui_utils.show_status(f"{base_id} is a structured binary frame and isn't editable in Backtrack.")
        return None

    # Enum / bool text frames get a dedicated picker instead of a free-text field.
    if base_id == 'TKEY':
        return _prompt_for_musical_key(current_value)
    if base_id == 'TMED':
        return _prompt_for_media_type(current_value)
    if base_id == 'TSRC':
        return _prompt_for_isrc(current_value)
    if base_id == 'TCMP':
        return _prompt_for_compilation(current_value)

    # Extract editor-ready defaults from whatever current_value is.
    # It may be a raw mutagen frame (single-file edit), a summary string
    # (bulk edit path), or None (new tag).
    # Multi-value candidates (#60): plain text frames the registry doesn't flag
    # single-only (e.g. genre, artist, composer, conductor, mood, language).
    multivalue = (ui_cat == 'text' and info.frame_type in ('TEXT', 'LIST')
                  and not info.single_only)

    default_vals: list[str] = []
    if current_value is None:
        default_val = ""
    elif hasattr(current_value, 'people'):
        # People frame (TIPL, TMCL)
        if initial_people is None:
            initial_people = list(current_value.people) if current_value.people else []
        default_val = ""
    elif hasattr(current_value, 'text'):
        txt = current_value.text
        # USLT and USER carry a single scalar string, not the value *list* every
        # other text frame uses.  Iterating one splits it into characters, so the
        # editor opened pre-filled with just the first letter of the lyrics — and
        # saving wrote that one character back over the whole body.
        if isinstance(txt, str):
            default_vals = [txt] if txt else []
        else:
            default_vals = [str(t) for t in txt] if txt else []
        default_val = default_vals[0] if default_vals else ""
    else:
        # Already a plain string (bulk edit summary — multi-values joined by '; ').
        default_val = str(current_value)
        default_vals = [s for s in (p.strip() for p in default_val.split('; ')) if s]

    # Power-user option (#62): edit values as raw text instead of the smart
    # widget — via the config default, or flipped per-edit with Ctrl-T. Binary/
    # asset frames (image, SYLT, EQU2, RVA2) have no raw-text form, so no toggle.
    if force_plain is not None:
        plain = force_plain
    else:
        try:
            from src.config import load_config
            plain = bool(load_config().get('plain_text_editing', False))
        except Exception:
            plain = False

    # Single-mode structural editors (no raw-text equivalent).
    if ui_cat == 'image':
        # With a known file, recommend the most likely cover sitting next to it
        # (ranked, best-guess first) and let the user pick or type a path. Without
        # one (some bulk paths), fall back to a plain path prompt.
        if file_path:
            img_path = pick_nearby_cover(file_path)
            if not isinstance(img_path, str):     # None (cancelled); CLEAR only with allow_none
                ui_utils.show_status("Cancelled")
                return None
            from src.id3 import cover_matcher as cm
            read = cm.read_image(img_path)
            if not read:
                ui_utils.show_status("Could not read that image.")
                return None
            img_data = read[0]
        else:
            img_data = _prompt_for_image_file()
            if not img_data:
                ui_utils.show_status("Cancelled")
                return None
        meta = _prompt_for_image_metadata()
        if not meta:
            ui_utils.show_status("Cancelled")
            return None
        pic_type, desc = meta
        return {'__image__': True, 'data': img_data, 'type': pic_type, 'desc': desc}
    if ui_cat == 'lyrics':
        ui_utils.show_status("Use lyric sync tool for SYLT.")
        return None
    if ui_cat == 'multiline text':
        return prompt.system_editor_edit(initial_text=default_val)

    def _edit_once(as_plain: bool):
        """Run a single edit pass: plain text/list field, or the format-specific widget."""
        if ui_cat == 'people':
            if as_plain:
                lines = "\n".join(f"{r}: {n}" for r, n in (initial_people or []))
                template = "# One 'role: name' per line\n" + (f"{lines}\n" if lines else "")
                txt = prompt.system_editor_edit(initial_text=template)
                if txt is None:
                    return None
                return prompt._parse_import_rows(txt, ("ROLE", "NAME"))
            return prompt.list_edit(f"{label}:", initial_people or [], ("ROLE", "NAME"))

        # Multi-value text frames (#60): a simple single-line field by default,
        # with Ctrl-T expanding to the structured list editor (#60 avenue A).
        # `as_plain` is the text field; the list is the "widget" side. mv_vals
        # carries the current values across a toggle (see the run loop below).
        if multivalue:
            if as_plain:
                return prompt.text(f"{label}:", default=(mv_vals[0] if mv_vals else ""))
            return prompt.list_edit(f"{label} — values:", list(mv_vals), ("VALUE",))

        if as_plain:
            hints = {'FRACTIONAL': ' (n/total)', 'ISO8601': ' (ISO 8601)',
                     'DDMM': ' (DD-MM or ISO)', 'YYYY': ' (year)', 'HHMM': ' (HH:MM)'}
            hint = hints.get(fmt or '', '')
            if fmt == 'INT_BIG' and ui_cat == 'duration':
                hint = ' (milliseconds)'
            return prompt.text(f"{label}{hint}:", default=default_val)

        if fmt == 'ISO8601':
            return prompt.datetime_edit(f"{label}:", initial=default_val)
        if fmt == 'DDMM':
            cal_init = default_val
            if len(default_val) == 4 and default_val.isdigit():
                year = time.localtime().tm_year
                cal_init = f"{year}-{default_val[2:]}-{default_val[:2]}"
            return prompt.calendar_select(f"{label}:", initial=cal_init)
        if fmt == 'YYYY':
            cal_init = f"{default_val}-01-01" if len(default_val) == 4 and default_val.isdigit() else default_val
            result = prompt.calendar_select(f"{label}:", initial=cal_init)
            if result is prompt.MODE_TOGGLE or result is None:
                return result
            return result[:4]
        if fmt == 'HHMM':
            init = f"{default_val[:2]}:{default_val[2:]}:00" if len(default_val) == 4 and default_val.isdigit() else default_val
            return prompt.time_edit(f"{label}:", initial=init or "00:00:00")
        if fmt == 'FRACTIONAL':
            b_id, _, _ = parse_composite_tag_id(tag_id)
            result = prompt.fraction_edit(f"Edit {label}:", tag=b_id, value=default_val)
            if result is prompt.MODE_TOGGLE or result is None:
                return result
            # A value can come back None when that half "varies" (bulk); the
            # single-file path never sets `varies`, but stay defensive.
            curr = (result.get('current') or '').strip()
            tot = (result.get('total') or '').strip()
            if curr and tot:
                return f"{curr}/{tot}"
            return curr or None
        if fmt == 'INT_BIG':
            try:
                cur_int = int(str(default_val).strip() or 0)
            except ValueError:
                cur_int = 0
            unit = "ms" if ui_cat == 'duration' else ""
            return prompt.number_edit(f"{label}:", value=cur_int, minimum=0, unit=unit)
        # Default: plain text (TEXT_UTF8, URL, LIST_STRING, etc.)
        return prompt.text(f"{label}:", default=default_val)

    # The raw↔widget toggle (#62) is only meaningful when the smart editor
    # actually differs from a plain text field. For plain-text/URL frames the
    # "smart widget" IS prompt.text(), so a toggle would be a no-op — don't
    # advertise it there. INT_BIG now gets a numeric spinner, so it toggles too.
    has_widget_toggle = (multivalue or ui_cat == 'people'
                         or fmt in ('ISO8601', 'DDMM', 'YYYY', 'HHMM', 'FRACTIONAL', 'INT_BIG'))

    # Multi-value frames (#60 avenue A): edit as a simple text field by default,
    # Ctrl-T expands to the list editor. Open straight into the list when the
    # frame already holds 2+ values (a single field can't show them). mv_vals is
    # the working set carried across toggles.
    mv_vals = list(default_vals)
    if multivalue and force_plain is None:
        plain = len(mv_vals) < 2

    # Run the editor, flipping raw↔widget whenever Ctrl-T returns MODE_TOGGLE.
    prompt._value_toggle_enabled = has_widget_toggle
    prompt._toggle_hint_label = 'values' if multivalue else 'widget'
    prompt._toggle_carry = None
    try:
        while True:
            res = _edit_once(plain)
            if res is prompt.MODE_TOGGLE:
                # Carry a half-typed text value into the list editor so it isn't
                # lost when expanding from the field.
                if multivalue and prompt._toggle_carry is not None:
                    carry = prompt._toggle_carry.strip()
                    mv_vals = [carry] if carry else []
                prompt._toggle_carry = None
                plain = not plain
                continue
            return res
    finally:
        prompt._value_toggle_enabled = False
        prompt._toggle_hint_label = 'widget'


def display_tag_id(tag_id: str) -> str:
    """Human-facing form of a frame key: drop a trailing ':' from an empty
    descriptor (e.g. mutagen's "APIC:" / "TXXX:" key → "APIC" / "TXXX")."""
    return tag_id[:-1] if tag_id.endswith(':') else tag_id


def summarize_tag_value(tag_id: str, raw_frame, display: bool = False) -> str:
    """Short display string for a frame's value, tailored to its category (people count, image size,
    star rating, band/gain count, or joined text)."""
    info = get_tag_info(tag_id)
    if not info:
        return str(raw_frame)

    # PEOPLE
    if info.ui_category == 'people':
        people = getattr(raw_frame, 'people', [])
        return f"{len(people)} people"

    # IMAGE
    if info.ui_category == 'image':
        img_data = getattr(raw_frame, 'data', b'')
        mime = getattr(raw_frame, 'mime', '').split("/")[-1].upper()
        b = len(img_data)
        return f"image [{mime}] ({b / 1024:.0f} KB)"

    # LYRICS (SYLT)
    if info.ui_category == 'lyrics':
        sylt_data = getattr(raw_frame, 'text', [])
        return f"{len(sylt_data)} lines"

    # RATING (POPM) — show stars + play count.
    if info.tag_id == 'POPM' or hasattr(raw_frame, 'rating'):
        stars = popm_byte_to_stars(getattr(raw_frame, 'rating', 0) or 0)
        cnt = int(getattr(raw_frame, 'count', 0) or 0)
        star_str = '★' * stars + '☆' * (5 - stars)
        return star_str + (f"  ({cnt} plays)" if cnt else "")

    # PLAY COUNTER (PCNT)
    if info.tag_id == 'PCNT':
        return f"{int(getattr(raw_frame, 'count', 0) or 0)} plays"

    # RECOMMENDED BUFFER SIZE (RBUF)
    if info.tag_id == 'RBUF':
        return f"{int(getattr(raw_frame, 'size', 0) or 0)} bytes"

    # COMPILATION FLAG (TCMP)
    if info.tag_id == 'TCMP':
        txt = getattr(raw_frame, 'text', None)
        val = str(txt[0]).strip() if txt else ''
        return 'Yes' if val not in ('', '0') else 'No'

    # AUDIO ADJUSTMENT (EQU2 / RVA2)
    if info.official_category == 'AUDIO_ADJUSTMENT' or hasattr(raw_frame, 'adjustments') or (hasattr(raw_frame, 'gain') and hasattr(raw_frame, 'channel')):
        if hasattr(raw_frame, 'adjustments'):
            bands = getattr(raw_frame, 'adjustments', [])
            n = len(bands)
            return f"{n} band{'s' if n != 1 else ''}"
        if hasattr(raw_frame, 'gain') and hasattr(raw_frame, 'channel'):
            return f"{getattr(raw_frame, 'gain', 0):+g} dB"
        return "—"

    # Generic text. Multi-values join with the storage separator by default,
    # because this summary also seeds editors and the clipboard where it has to
    # round-trip; `display=True` renders them as a list for the screen instead.
    if hasattr(raw_frame, 'text'):
        vals = [str(t).replace("\n", "\\") for t in raw_frame.text]
        text = format_value_list(vals) if display else "; ".join(vals)
        return text[:100]

    return str(raw_frame)[:100]


def collect_tag_data(paths: list[str]) -> tuple[dict, dict, dict]:
    """Scan files and tally tag presence counts, unique value summaries, and people-tag names."""
    tag_counts = {}
    tag_values = {}
    people_tags = {}

    for path in paths:
        try:
            audio = ID3(path)
            for tag_id in audio.keys():
                tag_counts[tag_id] = tag_counts.get(tag_id, 0) + 1

                frame = audio[tag_id]
                info = get_tag_info(tag_id)

                if info and info.ui_category == 'people':
                    people = getattr(frame, 'people', [])
                    if people:
                        if tag_id not in people_tags:
                            people_tags[tag_id] = set()
                        people_tags[tag_id].update(people)
                else:
                    if tag_id not in tag_values:
                        tag_values[tag_id] = []

                    val = summarize_tag_value(tag_id, frame)
                    if val not in tag_values[tag_id]:
                        tag_values[tag_id].append(val)
        except (mutagen.id3.ID3NoHeaderError, OSError, IOError):  # type: ignore[reportPrivateImportUsage]
            # skip unreadable files
            pass

    people_tags_list = {k: list(v) for k, v in people_tags.items()}

    return tag_counts, tag_values, people_tags_list


def apply_bulk_edit(
    audio: ID3,
    tag_id: str,
    operation: str,
    new_value: Any = None,
    new_tag_id: str | None = None
) -> bool:
    """Apply one set/rename/delete operation to a tag on an already-open ID3 object."""
    try:
        if operation == 'set':
            if new_value is None:
                return False
            audio.delall(tag_id)
            new_frame = create_frame(tag_id, new_value)
            if not new_frame:
                return False
            audio.add(new_frame)
            return True

        elif operation == 'rename':
            if not new_tag_id or new_tag_id == tag_id:
                return False

            old_info = get_tag_info(tag_id)
            new_info = get_tag_info(new_tag_id)
            if not old_info or not new_info or old_info.ui_category != new_info.ui_category:
                return False

            if tag_id not in audio:
                return False

            old_frame = audio.pop(tag_id)
            if not rename_frame(audio, old_frame, new_tag_id):
                audio.add(old_frame)
                return False
            return True

        elif operation == 'delete':
            audio.delall(tag_id)
            return True

        return False

    except (KeyError, AttributeError, ValueError):
        return False


def apply_bulk_operation_to_files(
    file_paths: list[str],
    operation: str,
    tag_ids: list[str],
    target_value: Any = None,
    library: list | None = None
) -> tuple[int, int]:
    """Apply one set/rename/delete operation across multiple files, saving and refreshing
    the library cache for each one changed. Returns (success_count, fail_count)."""
    success_count = 0
    fail_count = 0

    for path in file_paths:
        try:
            audio = ID3(path)
            changed = False

            for tag_id in tag_ids:
                if operation == 'set':
                    if apply_bulk_edit(audio, tag_id, 'set', target_value):
                        changed = True
                        success_count += 1
                    else:
                        fail_count += 1
                elif operation == 'rename':
                    if apply_bulk_edit(audio, tag_id, 'rename', new_tag_id=target_value):
                        changed = True
                        success_count += 1
                    else:
                        fail_count += 1
                elif operation == 'delete':
                    if apply_bulk_edit(audio, tag_id, 'delete'):
                        changed = True
                        success_count += 1
                    else:
                        fail_count += 1

            if changed:
                save_id3(audio)
                if library is not None:
                    try:
                        refresh_library_entry(library, path)
                    except Exception:
                        pass
        except (mutagen.id3.ID3NoHeaderError, OSError, IOError):  # type: ignore[reportPrivateImportUsage]
            fail_count += len(tag_ids)

    return success_count, fail_count
