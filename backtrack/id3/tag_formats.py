"""What each audio format's tags are and where every library field lives in them.

Three kinds of tag: ID3 (MP3, and WAV/AIFF, which carry an ID3 chunk), MP4
atoms (m4a/mp4/m4p/m4b) and Vorbis comments (FLAC, Ogg Vorbis, Opus). Every
reader and writer goes through here, so the three can't drift apart: one table
says where a field lives in each, and the accessors read and write it.
"""
from __future__ import annotations
import base64
import os
import re

import mutagen
from mutagen.flac import Picture
from mutagen.id3 import ID3, ID3NoHeaderError
import mutagen.id3
from mutagen.mp4 import MP4, MP4FreeForm, MP4Tags
from mutagen._vorbis import VComment

from backbone.log import log

ID3_EXTENSIONS = ('.mp3', '.wav', '.aiff', '.aif')
MP4_EXTENSIONS = ('.m4a', '.mp4', '.m4p', '.m4b')
VORBIS_EXTENSIONS = ('.flac', '.ogg', '.oga', '.opus')
# Raw .aac (ADTS) plays, but has nowhere to keep tags.
AUDIO_EXTENSIONS = (*ID3_EXTENSIONS, *MP4_EXTENSIONS, *VORBIS_EXTENSIONS, '.aac')


def kind(path: str) -> str:
    """The tag kind a file keeps, by its extension: 'id3', 'mp4', 'vorbis' or 'unsupported'."""
    ext = os.path.splitext(str(path))[1].lower()
    if ext in ID3_EXTENSIONS:
        return 'id3'
    if ext in MP4_EXTENSIONS:
        return 'mp4'
    if ext in VORBIS_EXTENSIONS:
        return 'vorbis'
    return 'unsupported'


def is_mp3(path: str) -> bool:
    """MPEG audio: what the frame-exact trim and the stale-length cleanup need,
    as opposed to anything that merely carries ID3."""
    return str(path).lower().endswith('.mp3')


def tags_kind(tags) -> str:
    """The kind of a tag object already open (by what it is, not a file name)."""
    if isinstance(tags, ID3):
        return 'id3'
    if isinstance(tags, MP4Tags):
        return 'mp4'
    if isinstance(tags, VComment):
        return 'vorbis'
    return 'unsupported'


# --- opening and saving --------------------------------------------------------

def _has_multivalue(audio: ID3) -> bool:
    """True if any text frame in `audio` holds more than one value.

    People frames (TMCL/TIPL) are skipped: their `.text` is a flat role/name
    pair list, not a multi-value text field."""
    for frame in audio.values():
        if getattr(frame, 'people', None) is not None:
            continue
        txt = getattr(frame, 'text', None)
        if isinstance(txt, list) and len(txt) > 1:
            return True
    return False


def load_id3(path: str) -> ID3:
    """A file's ID3 tag, or a new empty one when it has none yet, ready to add
    frames to and hand to save_id3(audio, path). A WAV or AIFF keeps its ID3 in
    a chunk, so its tag comes from (and saves back through) the container."""
    if is_mp3(path):
        try:
            return ID3(path)
        except ID3NoHeaderError:
            return ID3()
    f = mutagen.File(path)
    if f is None:
        raise ValueError(f"Not a readable audio file: {os.path.basename(path)}")
    if f.tags is None:
        f.add_tags()
    f.tags.filename = path
    return f.tags


def save_id3(audio: ID3, path: str | None = None) -> None:
    """Save an ID3 tag, choosing the version by content.

    ID3v2.3 collapses multi-value text into one '/'-joined string (corrupting
    values that contain '/'), so files carrying any multi-value frame are saved
    as v2.4. Single-value files stay v2.3 for maximum player compatibility.

    Refuses any file that doesn't keep ID3: an ID3 header written onto an MP4 (a
    fresh ID3() falls back to prepending one) corrupts the container. A WAV or
    AIFF takes it only through its own chunk (a tag from load_id3): a bare ID3
    prepended to one would corrupt it the same way."""
    target = path or getattr(audio, 'filename', None)
    if target:
        name = os.path.basename(str(target))
        if kind(target) != 'id3':
            raise ValueError(f"ID3 tags can only be written to MP3, WAV or AIFF files, not {name}")
        if not is_mp3(target) and type(audio) is ID3:
            log.warning("refused a bare ID3 tag for %s: it must come from load_id3", target)
            raise ValueError(f"{name} keeps its tags in a chunk: open them with load_id3")
    ver = 4 if _has_multivalue(audio) else 3
    if path is None:
        audio.save(v2_version=ver)
    else:
        audio.save(path, v2_version=ver)


def open_tags(path: str):
    """A file's tags of whatever kind, and a function that saves them: one way
    in for every reader and writer. Missing tags are created empty. Raises
    ValueError for a file with nowhere to keep tags (or that isn't what its name says)."""
    k = kind(path)
    if k == 'id3':
        tags = load_id3(path)
        return tags, lambda: save_id3(tags, path)
    if k in ('mp4', 'vorbis'):
        # mutagen.File looks at the content: an .ogg may hold Vorbis, Opus or FLAC.
        f = MP4(path) if k == 'mp4' else mutagen.File(path)
        if f is None:
            raise ValueError(f"Not a readable audio file: {os.path.basename(path)}")
        if f.tags is None:
            f.add_tags()
        if tags_kind(f.tags) != k:
            raise ValueError(f"{os.path.basename(path)} doesn't hold the tags its name says")
        return f.tags, f.save
    raise ValueError(f"No tags can be kept in {os.path.basename(path)}")


# --- where each field lives ------------------------------------------------------

# Library field → (ID3 frame, MP4 atom, Vorbis keys). The first Vorbis key is the
# one written; the rest are read as aliases. '' (or no keys): no home in that kind.
TAG_HOMES = {
    'title':         ('TIT2', '\xa9nam', ('TITLE',)),
    'artist':        ('TPE1', '\xa9ART', ('ARTIST',)),
    'album_artist':  ('TPE2', 'aART', ('ALBUMARTIST', 'ALBUM ARTIST')),
    'album':         ('TALB', '\xa9alb', ('ALBUM',)),
    'genre':         ('TCON', '\xa9gen', ('GENRE',)),
    'composer':      ('TCOM', '\xa9wrt', ('COMPOSER',)),
    'lyricist':      ('TEXT', '----:com.apple.iTunes:LYRICIST', ('LYRICIST',)),
    'year':          ('TDRC', '\xa9day', ('DATE', 'YEAR')),
    'work':          ('TIT3', '\xa9wrk', ('WORK',)),
    'grouping':      ('TIT1', '\xa9grp', ('GROUPING',)),
    'bpm':           ('TBPM', 'tmpo', ('BPM',)),
    'disc_subtitle': ('TSST', '', ('DISCSUBTITLE',)),
    'movement_name': ('MVNM', '\xa9mvn', ('MOVEMENTNAME',)),
    'Title Sort Order':        ('TSOT', 'sonm', ('TITLESORT',)),
    'Performer Sort Order':    ('TSOP', 'soar', ('ARTISTSORT',)),
    'Album Artist Sort Order': ('TSO2', 'soaa', ('ALBUMARTISTSORT',)),
    'Album Sort Order':        ('TSOA', 'soal', ('ALBUMSORT',)),
    'Composer Sort Order':     ('TSOC', 'soco', ('COMPOSERSORT',)),
    'comment':       ('COMM', '\xa9cmt', ('COMMENT', 'DESCRIPTION')),
    'lyrics':        ('USLT', '\xa9lyr', ('LYRICS', 'UNSYNCEDLYRICS')),
    'replaygain_track_gain': ('TXXX:REPLAYGAIN_TRACK_GAIN', '----:com.apple.iTunes:replaygain_track_gain',
                              ('REPLAYGAIN_TRACK_GAIN',)),
    'replaygain_track_peak': ('TXXX:REPLAYGAIN_TRACK_PEAK', '----:com.apple.iTunes:replaygain_track_peak',
                              ('REPLAYGAIN_TRACK_PEAK',)),
    'subtitle':      ('TIT3', '', ('SUBTITLE',)),
    'conductor':     ('TPE3', '', ('CONDUCTOR',)),
    'remixer':       ('TPE4', '', ('REMIXER',)),
    'publisher':     ('TPUB', '', ('PUBLISHER', 'LABEL')),
    'copyright':     ('TCOP', 'cprt', ('COPYRIGHT',)),
    'isrc':          ('TSRC', '', ('ISRC',)),
    'key':           ('TKEY', '', ('INITIALKEY',)),
    'language':      ('TLAN', '', ('LANGUAGE',)),
    'mood':          ('TMOO', '', ('MOOD',)),
    'encoder':       ('TSSE', '\xa9too', ('ENCODER',)),
    'original_artist': ('TOPE', '', ('ORIGINALARTIST',)),
    'original_album':  ('TOAL', '', ('ORIGINALALBUM',)),
}

# Numbered fields → (ID3 frame, MP4 pair atom or (number atom, total atom),
# Vorbis number keys, Vorbis total keys, the library's total field).
PAIR_HOMES = {
    'track': ('TRCK', 'trkn', ('TRACKNUMBER',), ('TRACKTOTAL', 'TOTALTRACKS'), 'total_tracks'),
    'disc':  ('TPOS', 'disk', ('DISCNUMBER',), ('DISCTOTAL', 'TOTALDISCS'), 'total_discs'),
    'movement_number': ('MVIN', ('\xa9mvi', '\xa9mvc'), ('MOVEMENT', 'MOVEMENTNUMBER'), ('MOVEMENTTOTAL',),
                        'total_movements'),
}

_KINDS = ('id3', 'mp4', 'vorbis')


def home(field: str, k: str):
    """Where `field` lives in kind `k`: an ID3 frame id, an MP4 atom, or the
    Vorbis keys; empty when it has no home there."""
    entry = TAG_HOMES.get(field) or PAIR_HOMES.get(field)
    return entry[_KINDS.index(k)] if entry and k in _KINDS else ''


def has_home(k: str, field: str) -> bool:
    return bool(home(field, k))


def field_of_frame(frame_id: str) -> str | None:
    """The field an ID3 frame holds (e.g. 'TSOP' → 'Performer Sort Order')."""
    return next((f for f, h in TAG_HOMES.items() if h[0] == frame_id), None)


def _text_of(value) -> str:
    if isinstance(value, bytes):
        return value.decode('utf-8', 'replace').strip()
    return str(value).strip()


def texts(tags, field: str) -> list[str]:
    """Every value of `field` in `tags`, as trimmed strings (empty when it isn't there)."""
    k = tags_kind(tags)
    where = home(field, k)
    if not where:
        return []
    if k == 'id3' and where == 'USLT':                  # one string per frame, not a list
        vals = [fr.text for fr in tags.getall(where)]
    elif k == 'id3':
        vals = [v for fr in tags.getall(where) for v in (getattr(fr, 'text', None) or [])]
    elif k == 'mp4':
        vals = list(tags.get(where) or [])
    else:
        key = next((key for key in where if key in tags), None)
        vals = list(tags[key]) if key else []
    return [t for t in (_text_of(v) for v in vals) if t]


def _split_pair(raw: str) -> tuple[str, str]:
    parts = str(raw).replace('⁄', '/').split('/')
    return parts[0].strip(), (parts[1].strip() if len(parts) > 1 else '')


def pair(tags, field: str) -> tuple[str, str] | None:
    """A numbered field and its total ('3', '12'; '' when a part is missing),
    or None when the field isn't there at all."""
    k = tags_kind(tags)
    if k not in _KINDS:
        return None
    id3f, atom, num_keys, total_keys, _total = PAIR_HOMES[field]
    if k == 'id3':
        fr = tags.get(id3f)
        if fr is None:
            return None
        return _split_pair(fr.text[0]) if fr.text else ('', '')
    if k == 'mp4':
        if isinstance(atom, tuple):
            num, total = tags.get(atom[0]), tags.get(atom[1])
            return (str(num[0]), str(total[0]) if total else '') if num else None
        val = tags.get(atom)
        if not val:
            return None
        p = val[0]
        return (str(p[0]) if p[0] else ''), (str(p[1]) if len(p) > 1 and p[1] else '')
    key = next((key for key in num_keys if key in tags), None)
    if key is None:
        return None
    num, total = _split_pair(tags[key][0]) if tags[key] else ('', '')
    if not total:
        tkey = next((t for t in total_keys if t in tags), None)
        total = str(tags[tkey][0]).strip() if tkey and tags[tkey] else ''
    return num, total


def delete(tags, field: str) -> bool:
    """Remove `field` (and its total, for a numbered one). Returns whether anything went."""
    k = tags_kind(tags)
    where = home(field, k)
    if not where:
        return False
    if k == 'id3':
        had = bool(tags.getall(where))
        tags.delall(where)
        return had
    if k == 'mp4':
        atoms = where if isinstance(where, tuple) else (where,)
        return bool([tags.pop(a) for a in atoms if a in tags])
    keys = list(where)
    if field in PAIR_HOMES:
        keys += list(PAIR_HOMES[field][3])
    present = [key for key in keys if key in tags]
    for key in present:
        del tags[key]
    return bool(present)


def set_text(tags, field: str, values: list[str]) -> None:
    """Replace `field` with `values` (several for a multi-value field); none removes it."""
    values = [v for v in (str(v).strip() for v in values) if v]
    delete(tags, field)
    if not values:
        return
    k = tags_kind(tags)
    where = home(field, k)
    if not where:
        raise ValueError(f"{field} has no place in {k} tags")
    if k == 'id3':
        if where.startswith('TXXX:'):
            tags.add(mutagen.id3.TXXX(encoding=3, desc=where[5:], text=values))
        elif where in ('COMM', 'USLT'):
            frame = getattr(mutagen.id3, where)
            tags.add(frame(encoding=3, lang='eng', desc='', text="\n".join(values)
                           if where == 'USLT' else values))
        else:
            tags.add(getattr(mutagen.id3, where)(encoding=3, text=values))
    elif k == 'mp4':
        if where.startswith('----:'):
            tags[where] = [MP4FreeForm(v.encode('utf-8')) for v in values]
        elif where == 'tmpo':
            tags[where] = [int(float(values[0]))]
        else:
            tags[where] = values
    else:
        tags[where[0]] = values


def set_pair(tags, field: str, num: str, total: str = '') -> None:
    """Write a numbered field and its total ('' leaves the total out)."""
    num, total = str(num or '').strip(), str(total or '').strip()
    delete(tags, field)
    if not num and not total:
        return
    k = tags_kind(tags)
    id3f, atom, num_keys, total_keys, _total = PAIR_HOMES[field]
    if k == 'id3':
        tags.add(getattr(mutagen.id3, id3f)(encoding=3, text=[f"{num}/{total}" if total else num]))
    elif k == 'mp4':
        if isinstance(atom, tuple):
            if num:
                tags[atom[0]] = [int(num)]
            if total:
                tags[atom[1]] = [int(total)]
        else:
            tags[atom] = [(int(num or 0), int(total or 0))]
    elif k == 'vorbis':
        if num:
            tags[num_keys[0]] = [num]
        if total:
            tags[total_keys[0]] = [total]


def compilation_flag(tags) -> bool:
    """Whether the tags mark the album a compilation (TCMP, cpil or COMPILATION)."""
    k = tags_kind(tags)
    if k == 'id3':
        tcmp = tags.get('TCMP')
        return bool(tcmp is not None and tcmp.text) and str(tcmp.text[0]).strip() not in ('', '0')
    if k == 'mp4':
        return bool(tags.get('cpil'))
    if k == 'vorbis':
        val = tags.get('COMPILATION')
        return bool(val) and str(val[0]).strip() not in ('', '0')
    return False


def set_compilation(tags, on: bool) -> None:
    k = tags_kind(tags)
    if k == 'id3':
        tags.delall('TCMP')
        if on:
            tags.add(mutagen.id3.TCMP(encoding=3, text=['1']))
    elif k == 'mp4':
        tags.pop('cpil', None)
        if on:
            tags['cpil'] = True
    elif k == 'vorbis':
        if 'COMPILATION' in tags:
            del tags['COMPILATION']
        if on:
            tags['COMPILATION'] = ['1']


# Vorbis PERFORMER values often carry the role: "Name (role)".
_PERFORMER_ROLE = re.compile(r'^(.*?)\s*\(([^()]*)\)\s*$')


def credits(tags) -> list[list[str]]:
    """The performers and other people credited, as [name, role] pairs:
    ID3 TMCL/TIPL, Vorbis PERFORMER ("Name (role)"), CONDUCTOR, ARRANGER."""
    k = tags_kind(tags)
    out = []
    if k == 'id3':
        for frame_id in ('TMCL', 'TIPL'):
            frame = tags.get(frame_id)
            for role, name in getattr(frame, 'people', None) or []:
                if name.strip():
                    out.append([name.strip(), role.strip()])
    elif k == 'vorbis':
        for value in tags.get('PERFORMER') or []:
            m = _PERFORMER_ROLE.match(value)
            name, role = (m.group(1), m.group(2)) if m else (value, '')
            if name.strip():
                out.append([name.strip(), role.strip()])
        for key, role in (('CONDUCTOR', 'conductor'), ('ARRANGER', 'arranger')):
            out += [[v.strip(), role] for v in tags.get(key) or [] if v.strip()]
    return out


# --- every tag as key and values (the per-file editor for MP4 and Vorbis) --------

# Pictures have their own row in the editor, not a raw key.
_PICTURE_KEYS = ('covr', 'metadata_block_picture', 'coverart', 'coverartmime')
# MP4 atoms holding numbers or a yes/no rather than text.
_MP4_INTS = ('tmpo', 'stik', 'rtng', 'tves', 'tvsn', 'plID', 'cnID', 'geID', 'atID', 'sfID', 'cmID', 'akID')
_MP4_BOOLS = ('cpil', 'pgap', 'pcst', 'shwm', 'hdvd')


def kv_items(tags) -> list[tuple[str, list[str]]]:
    """Every tag but the pictures, as (key, values) in key order. Vorbis keys
    are shown upper-case; MP4 number pairs as 'n/total', flags as '1'/'0'."""
    out = []
    for key in sorted(tags.keys(), key=str.lower):
        if key.lower() in _PICTURE_KEYS:
            continue
        vals = tags[key]
        if tags_kind(tags) == 'vorbis':
            out.append((key.upper(), [str(v) for v in vals]))
        elif isinstance(vals, bool):
            out.append((key, ['1' if vals else '0']))
        else:
            out.append((key, ['/'.join(str(n) for n in v if n) if isinstance(v, tuple) else _text_of(v)
                              for v in vals]))
    return out


def kv_set(tags, key: str, values: list[str]) -> None:
    """Set `key` to `values`, turned into what an MP4 atom holds (a number pair,
    a number, a flag, bytes). Raises ValueError for a value its atom can't hold."""
    values = [v for v in (str(v).strip() for v in values) if v]
    if tags_kind(tags) == 'vorbis':
        tags[key.upper()] = values
        return
    try:
        if key in ('trkn', 'disk'):
            tags[key] = [tuple(int(p or 0) for p in (_split_pair(v)[0], _split_pair(v)[1] or 0)) for v in values]
        elif key in _MP4_BOOLS:
            tags[key] = values[0] not in ('0', 'no', 'false', '') if values else False
        elif key in _MP4_INTS or key in ('\xa9mvi', '\xa9mvc'):
            tags[key] = [int(v) for v in values]
        elif key.startswith('----:'):
            tags[key] = [MP4FreeForm(v.encode('utf-8')) for v in values]
        else:
            tags[key] = values
    except (ValueError, TypeError) as e:
        raise ValueError(f"{key} needs {'a number like 3/12' if key in ('trkn', 'disk') else 'a number'}") from e


def kv_delete(tags, key: str) -> None:
    if key in tags:
        del tags[key]


def label_for(k: str, key: str) -> str:
    """A friendly name for a raw key, from the field table ('ALBUMARTIST' →
    'Album artist'); '' for a key the table doesn't know."""
    if key.upper() in ('CPIL', 'COMPILATION'):
        return 'Compilation'
    for f in (*TAG_HOMES, *PAIR_HOMES):
        where = home(f, k)
        names = [w.upper() for w in where] if k == 'vorbis' else (where if isinstance(where, tuple) else (where,))
        if key.upper() in names if k == 'vorbis' else key in names:
            if f in ('bpm', 'isrc'):
                return f.upper()
            return f[0].upper() + f[1:].replace('_', ' ') if f[0].islower() else f
    return ''


def parse_gain(text) -> float | None:
    """A ReplayGain value ("-6.20 dB") in dB, or None when it isn't one."""
    m = re.match(r'^\s*([+-]?\d+(?:\.\d+)?)\s*(?:dB)?\s*$', str(text or ''), re.I)
    return float(m.group(1)) if m else None


# --- cover art ------------------------------------------------------------------

def _pick(pictures: list, preferred_desc: str | None, preferred_type: int | None):
    """The cover among (data, type, desc) pictures: by description, then by
    picture type, else the first."""
    if not pictures:
        return None
    if preferred_desc:
        want = preferred_desc.strip().lower()
        for p in pictures:
            if (p[2] or '').strip().lower() == want:
                return p
    if preferred_type is not None:
        for p in pictures:
            if p[1] == preferred_type:
                return p
    return pictures[0]


def _vorbis_pictures(f) -> list:
    """A FLAC's pictures, or an Ogg file's METADATA_BLOCK_PICTURE comments, as mutagen Pictures."""
    if hasattr(f, 'pictures'):
        return list(f.pictures)
    pics = []
    for value in (f.tags or {}).get('METADATA_BLOCK_PICTURE') or []:
        try:
            pics.append(Picture(base64.b64decode(value)))
        except (ValueError, mutagen.MutagenError) as e:
            log.info("unreadable picture in %s: %s", getattr(f, 'filename', ''), e)
    return pics


def embedded_cover(path: str, preferred_desc: str | None = None,
                   preferred_type: int | None = None) -> bytes | None:
    """The picture embedded in an audio file (an ID3 APIC, an MP4 covr, a FLAC
    or Ogg picture): the one described `preferred_desc`, else of `preferred_type`,
    else the first. None when it has none."""
    k = kind(path)
    try:
        if k == 'id3':
            tags = load_id3(path)
            pics = [(f.data, f.type, f.desc) for f in tags.getall('APIC')]
        elif k == 'mp4':
            pics = [(bytes(c), 3, '') for c in (MP4(path).tags or {}).get('covr') or []]
        elif k == 'vorbis':
            f = mutagen.File(path)
            pics = [(p.data, p.type, p.desc) for p in (_vorbis_pictures(f) if f else [])]
        else:
            return None
    except (OSError, ValueError, mutagen.MutagenError):
        return None
    chosen = _pick(pics, preferred_desc, preferred_type)
    return bytes(chosen[0]) if chosen and chosen[0] else None
