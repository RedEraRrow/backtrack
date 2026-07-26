"""Format-agnostic tag writer for the bulk "Derive from filename" operation.

Writes a small, fixed set of fields — title, track(+total), disc(+total), album,
artist — to either MP3 (ID3v2) or the MP4 family (m4a/mp4/m4p/aac), so bulk
derivation works across formats. Two robustness guarantees the rest of the app
did not previously have:

  * **Blank MP3s** (no ID3 header) get a fresh ID3 created rather than raising.
  * **Non-MP3s** are written via their native MP4 atoms; anything genuinely
    unsupported returns ``unsupported=True`` instead of throwing.

Field semantics: ``track`` and ``disc`` are treated as single units that carry
their totals. "Fill blanks only" means a field is written only when its tag is
currently absent/empty, unless ``overwrite`` is set.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field

from mutagen.id3 import ID3, ID3NoHeaderError, TIT2, TPE1, TPE2, TALB, TRCK, TPOS, TSST, TDRC, TCMP, TSOT, TSOP, TSO2, TSOA  # type: ignore[reportPrivateImportUsage]  # noqa: E501
from mutagen.mp4 import MP4, MP4Cover  # type: ignore[reportPrivateImportUsage]

# The fields this writer understands (track/disc carry their totals). The
# compilation flag is not a user field — it rides along when a compilation is
# detected and the album artist is written.
FIELDS = ('title', 'artist', 'album_artist', 'album', 'track', 'disc',
          'disc_subtitle', 'year')

# Sort-order tags ride with their base field when the 'sort' pseudo-field is
# applied. The sort *string* is supplied by the caller as values['<base>_sort']
# (computed by the smart sort engine); the writer just stores it.
# base field → (ID3 frame class, MP4 sort atom).
_SORT_MAP = {
    'title':        (TSOT, 'sonm'),
    'artist':       (TSOP, 'soar'),
    'album_artist': (TSO2, 'soaa'),
    'album':        (TSOA, 'soal'),
}

_MP3_EXTS = ('.mp3',)
_MP4_EXTS = ('.m4a', '.mp4', '.m4p', '.aac')


@dataclass
class WriteResult:
    written: list[str] = field(default_factory=list)          # fields actually written
    skipped_existing: list[str] = field(default_factory=list)  # chosen but already had a value
    error: str | None = None
    unsupported: bool = False
    skipped_format: bool = False   # e.g. an MP4 cover that isn't JPEG/PNG

    @property
    def changed(self) -> bool:
        return bool(self.written)


def format_kind(path: str) -> str:
    """Return 'mp3', 'mp4', or 'unsupported' for a path."""
    ext = os.path.splitext(path)[1].lower()
    if ext in _MP3_EXTS:
        return 'mp3'
    if ext in _MP4_EXTS:
        return 'mp4'
    return 'unsupported'


def is_writable(path: str) -> bool:
    return format_kind(path) != 'unsupported'


def writable_fields(path: str) -> set:
    """Fields that can actually be written for this file's format.

    MP4 has no standard disc-subtitle atom, so ``disc_subtitle`` is dropped for
    the MP4 family — the plan/preview must not claim a write it can't perform.
    """
    kind = format_kind(path)
    if kind == 'unsupported':
        return set()
    fields = set(FIELDS)
    if kind == 'mp4':
        fields.discard('disc_subtitle')
    return fields


def _fmt_pair(num, total) -> str:
    """ID3 numeric-pair text: 'n/total' when a total is present, else 'n'."""
    return f"{int(num)}/{int(total)}" if total else f"{int(num)}"


# ---------------------------------------------------------------------------
# Reading current field presence (to honour fill-blanks-only)
# ---------------------------------------------------------------------------

def _id3_present(audio: ID3) -> dict[str, bool]:
    def _txt(fid: str) -> bool:
        fr = audio.get(fid)
        return bool(fr and fr.text and str(fr.text[0]).strip())

    def _num(fid: str) -> bool:
        fr = audio.get(fid)
        if not (fr and fr.text):
            return False
        head = str(fr.text[0]).split('/')[0].strip()
        return bool(head) and head != '0'

    return {
        'title': _txt('TIT2'), 'artist': _txt('TPE1'), 'album_artist': _txt('TPE2'),
        'album': _txt('TALB'), 'track': _num('TRCK'), 'disc': _num('TPOS'),
        'disc_subtitle': _txt('TSST'), 'year': _txt('TDRC'),
        'compilation': bool(audio.get('TCMP') and str(audio['TCMP'].text[0]) not in ('', '0')),
    }


def _mp4_present(audio: MP4) -> dict[str, bool]:
    tags = audio.tags or {}

    def _txt(atom: str) -> bool:
        v = tags.get(atom)
        return bool(v and str(v[0]).strip())

    def _pair(atom: str) -> bool:
        v = tags.get(atom)
        try:
            return bool(v and v[0][0])
        except (IndexError, TypeError):
            return False

    return {
        'title': _txt('\xa9nam'), 'artist': _txt('\xa9ART'), 'album_artist': _txt('aART'),
        'album': _txt('\xa9alb'), 'track': _pair('trkn'), 'disc': _pair('disk'),
        'disc_subtitle': False,           # no standard MP4 atom — never written
        'year': _txt('\xa9day'),
        'compilation': bool(tags.get('cpil')),
    }


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def present_fields(path: str) -> dict[str, bool]:
    """Which of :data:`FIELDS` already have a non-empty value on disk.

    Used to build the preview (write vs skip) without modifying the file.
    Returns all-False if the file can't be read or isn't writable.
    """
    kind = format_kind(path)
    try:
        if kind == 'mp3':
            try:
                return _id3_present(ID3(path))
            except ID3NoHeaderError:
                return {f: False for f in FIELDS}
        if kind == 'mp4':
            return _mp4_present(MP4(path))
    except Exception:
        pass
    return {f: False for f in FIELDS}


def write_fields(path: str, values: dict, apply_fields, overwrite: bool = False) -> WriteResult:
    """Write the chosen fields to ``path``.

    ``values`` is a parser dict (title/track/total_tracks/disc/total_discs/album/
    artist). ``apply_fields`` is the subset of :data:`FIELDS` the user opted into.
    Only fields with a derived value are written; existing values are preserved
    unless ``overwrite`` is True.
    """
    kind = format_kind(path)
    if kind == 'unsupported':
        return WriteResult(unsupported=True)

    apply_fields = set(apply_fields)
    res = WriteResult()

    try:
        if kind == 'mp3':
            try:
                audio = ID3(path)
            except ID3NoHeaderError:
                audio = ID3()               # fresh header for a blank MP3
            present = _id3_present(audio)
        else:
            audio = MP4(path)
            if audio.tags is None:
                audio.add_tags()
            present = _mp4_present(audio)

        for f in FIELDS:
            if f not in apply_fields:
                continue
            if f == 'disc_subtitle' and kind == 'mp4':
                continue                    # no standard MP4 disc-subtitle atom
            val = values.get(f)
            if val is None or (isinstance(val, str) and not val.strip()):
                continue                    # nothing derived for this field
            if present.get(f) and not overwrite:
                res.skipped_existing.append(f)
                continue
            _set_field(audio, kind, f, values)
            res.written.append(f)

        # Compilation flag rides along with the album artist when detected.
        if values.get('compilation') and 'album_artist' in apply_fields:
            if overwrite or not present.get('compilation'):
                _set_compilation(audio, kind)
                res.written.append('compilation')

        # Sort-order tags ride with a base field *actually written* this run, so
        # the sort string always matches the value we wrote (not a skipped one).
        if 'sort' in apply_fields:
            written_now = set(res.written)
            for base, (frame_cls, atom) in _SORT_MAP.items():
                if base not in written_now:
                    continue
                sval = values.get(f'{base}_sort')
                if not sval:
                    continue                    # no sort needed (e.g. "Radiohead")
                _set_sort(audio, kind, frame_cls, atom, str(sval))
                res.written.append(f'{base}_sort')

        if res.written:
            if kind == 'mp3':
                from src.id3.id3_tag_handler import save_id3
                save_id3(audio, path)   # type: ignore[arg-type]  # ID3 in this branch; v2.4 iff multi-value present
            else:
                audio.save()
    except Exception as e:                  # never let one bad file abort a bulk run
        return WriteResult(error=str(e))

    return res


def _set_compilation(audio, kind: str) -> None:
    if kind == 'mp3':
        audio.setall('TCMP', [TCMP(encoding=3, text=['1'])])
    else:
        audio.tags['cpil'] = True


def _set_sort(audio, kind: str, frame_cls, atom: str, val: str) -> None:
    if kind == 'mp3':
        audio.setall(frame_cls.__name__, [frame_cls(encoding=3, text=[val])])
    else:
        audio.tags[atom] = [val]


def _set_field(audio, kind: str, f: str, values: dict) -> None:
    if kind == 'mp3':
        if f == 'title':
            audio.setall('TIT2', [TIT2(encoding=3, text=[str(values['title'])])])
        elif f == 'artist':
            audio.setall('TPE1', [TPE1(encoding=3, text=[str(values['artist'])])])
        elif f == 'album_artist':
            audio.setall('TPE2', [TPE2(encoding=3, text=[str(values['album_artist'])])])
        elif f == 'album':
            audio.setall('TALB', [TALB(encoding=3, text=[str(values['album'])])])
        elif f == 'track':
            audio.setall('TRCK', [TRCK(encoding=3, text=[_fmt_pair(values['track'], values.get('total_tracks'))])])
        elif f == 'disc':
            audio.setall('TPOS', [TPOS(encoding=3, text=[_fmt_pair(values['disc'], values.get('total_discs'))])])
        elif f == 'disc_subtitle':
            audio.setall('TSST', [TSST(encoding=3, text=[str(values['disc_subtitle'])])])
        elif f == 'year':
            audio.setall('TDRC', [TDRC(encoding=3, text=[str(values['year'])])])
    else:  # mp4
        tags = audio.tags
        if f == 'title':
            tags['\xa9nam'] = [str(values['title'])]
        elif f == 'artist':
            tags['\xa9ART'] = [str(values['artist'])]
        elif f == 'album_artist':
            tags['aART'] = [str(values['album_artist'])]
        elif f == 'album':
            tags['\xa9alb'] = [str(values['album'])]
        elif f == 'track':
            tags['trkn'] = [(int(values['track']), int(values.get('total_tracks') or 0))]
        elif f == 'disc':
            tags['disk'] = [(int(values['disc']), int(values.get('total_discs') or 0))]
        elif f == 'year':
            tags['\xa9day'] = [str(values['year'])]


# ---------------------------------------------------------------------------
# Album art (APIC / covr) — used by the per-file album-art bulk op
# ---------------------------------------------------------------------------

def has_cover(path: str) -> bool:
    """Whether the file already carries embedded album art.

    MP3: any APIC frame. MP4: a non-empty ``covr`` atom. False on read errors so
    a bad file is treated as blank (and the fill-blanks preview offers to fill).
    """
    kind = format_kind(path)
    try:
        if kind == 'mp3':
            try:
                audio = ID3(path)
            except ID3NoHeaderError:
                return False
            return any(k == 'APIC' or k.startswith('APIC:') for k in audio.keys())
        if kind == 'mp4':
            audio = MP4(path)
            return bool(audio.tags and audio.tags.get('covr'))
    except Exception:
        pass
    return False


def write_cover(path: str, data: bytes, mime: str, *, pic_type: int = 3,
                desc: str = '', overwrite: bool = False) -> WriteResult:
    """Embed ``data`` as album art on ``path`` (MP3 APIC or MP4 ``covr``).

    Honours fill-blanks: a file that already has art is left untouched unless
    ``overwrite`` is set (reported via ``skipped_existing``). MP4 ``covr`` only
    holds JPEG/PNG — anything else returns ``skipped_format=True`` rather than
    silently writing nothing. On MP3, existing APIC frames are replaced so the
    new art is the only cover.
    """
    kind = format_kind(path)
    if kind == 'unsupported':
        return WriteResult(unsupported=True)
    if not isinstance(data, bytes) or not data:
        return WriteResult(error='empty image data')

    res = WriteResult()
    try:
        if not overwrite and has_cover(path):
            res.skipped_existing.append('cover')
            return res

        if kind == 'mp3':
            from src.id3.id3_tag_handler import create_apic_frame, save_id3
            try:
                audio = ID3(path)
            except ID3NoHeaderError:
                audio = ID3()                     # fresh header for a blank MP3
            frame = create_apic_frame(data, mime, pic_type, desc)
            if frame is None:
                return WriteResult(error='could not build APIC frame')
            audio.delall('APIC')
            audio.add(frame)
            save_id3(audio, path)                 # type: ignore[arg-type]
        else:  # mp4
            fmt = (MP4Cover.FORMAT_PNG if mime == 'image/png'
                   else MP4Cover.FORMAT_JPEG if mime == 'image/jpeg' else None)
            if fmt is None:
                return WriteResult(skipped_format=True)
            audio = MP4(path)
            if audio.tags is None:
                audio.add_tags()
            tags = audio.tags
            assert tags is not None
            tags['covr'] = [MP4Cover(data, imageformat=fmt)]
            audio.save()
    except Exception as e:
        return WriteResult(error=str(e))

    res.written.append('cover')
    return res
