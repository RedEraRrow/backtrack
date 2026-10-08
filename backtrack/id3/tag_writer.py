"""Format-agnostic tag writer: the fields the bulk and CLI operations set (title,
artist, album_artist, album, track, disc, disc_subtitle, year), plus cover art,
for every kind of tag (ID3 on MP3/WAV/AIFF, MP4 atoms, Vorbis comments on
FLAC/Ogg/Opus), through the field table in tag_formats. A blank file gets fresh
tags; an unsupported format returns ``unsupported=True`` instead of raising.

Field semantics: ``track`` and ``disc`` are treated as single units that carry
their totals. "Fill blanks only" means a field is written only when its tag is
currently absent/empty, unless ``overwrite`` is set.
"""
from __future__ import annotations

import base64
from dataclasses import dataclass, field

import mutagen
from mutagen.flac import Picture
from mutagen.mp4 import MP4, MP4Cover  # type: ignore[reportPrivateImportUsage]
from backtrack.id3 import tag_registry as _reg
from backtrack.id3.tag_formats import (compilation_flag, delete, embedded_cover, field_of_frame, has_home,
                                       is_mp3, kind as format_kind, load_id3, open_tags, pair, save_id3,
                                       set_compilation, set_pair, set_text, texts)
from backbone.log import quietly

# The fields this writer understands (track/disc carry their totals). The
# compilation flag is not a user field: it rides along when a compilation is
# detected and the album artist is written.
FIELDS = ('title', 'artist', 'album_artist', 'album', 'track', 'disc',
          'disc_subtitle', 'year')

# base field → the sort field that orders it. Sort-order tags ride with their
# base field when the 'sort' pseudo-field is applied; the caller supplies the
# sort string as values['<base>_sort'] and the writer just stores it. Derived
# from the canonical table in tag_registry; `auto` marks a sort tag as one an
# ordinary write should produce.
_SORT_MAP = {t.field: field_of_frame(t.frame) for t in _reg.SORT_TAGS if t.auto}


def _is_placeholder(value) -> bool:
    """A name the app derives rather than stores (see id3.tag_handler)."""
    from backtrack.id3.tag_handler import is_placeholder_name
    return is_placeholder_name(value)


@dataclass
class WriteResult:
    written: list[str] = field(default_factory=list)          # fields actually written
    skipped_existing: list[str] = field(default_factory=list)  # chosen but already had a value
    error: str | None = None
    unsupported: bool = False
    skipped_format: bool = False   # e.g. an MP4 cover that isn't JPEG/PNG
    skipped_placeholder: list[str] = field(default_factory=list)  # derived-only names

    @property
    def changed(self) -> bool:
        """True if any field was actually written."""
        return bool(self.written)


def is_writable(path: str) -> bool:
    """True if the path's format keeps tags this writer can set. Raw .aac (ADTS)
    plays and scans, but has nowhere to keep them."""
    return format_kind(path) != 'unsupported'


def writable_fields(path: str) -> set:
    """Fields that can actually be written for this file's format: MP4 has no
    standard disc-subtitle atom, so the plan/preview mustn't claim that write."""
    kind = format_kind(path)
    return {f for f in FIELDS if has_home(kind, f)} if kind != 'unsupported' else set()


def _num(v) -> str:
    """A track or disc number as written. A fractional disc ('1.5', kept by
    reflow on purpose) stays as it is."""
    f = float(v)
    return str(int(f)) if f.is_integer() else str(f)


# ---------------------------------------------------------------------------
# Reading current field presence (to honour fill-blanks-only)
# ---------------------------------------------------------------------------

def _present(tags) -> dict[str, bool]:
    """Which fields already have a non-empty value in these tags, keyed by field name."""
    out = {f: bool(texts(tags, f)) for f in FIELDS if f not in ('track', 'disc')}
    for f in ('track', 'disc'):
        p = pair(tags, f)
        out[f] = bool(p and p[0] and p[0] != '0')
    out['compilation'] = compilation_flag(tags)
    return out


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def present_fields(path: str) -> dict[str, bool]:
    """Which of :data:`FIELDS` already have a non-empty value on disk.

    Used to build the preview (write vs skip) without modifying the file.
    Returns all-False if the file can't be read or isn't writable.
    """
    with quietly():
        return _present(open_tags(path)[0])
    return {f: False for f in FIELDS}


def read_number_pairs(path: str) -> dict:
    """The track/disc numbering currently stored in the file itself.

    Returns ``{'track', 'total_tracks', 'disc', 'total_discs'}`` as strings, with
    ``''`` for anything absent.  Read from disk rather than the library cache,
    because the cache is exactly what is stale in the case that matters most:
    a disc you have just renumbered by hand, before the next re-scan.  The disc
    value keeps any fraction (``'1.5'``), which is how a disc gets parked between
    two others ahead of a reflow.
    """
    out = {'track': '', 'total_tracks': '', 'disc': '', 'total_discs': ''}
    with quietly():
        tags = open_tags(path)[0]
        for f, tot in (('track', 'total_tracks'), ('disc', 'total_discs')):
            if (p := pair(tags, f)) is not None:
                out[f], out[tot] = p
    return out


def write_fields(path: str, values: dict, apply_fields, overwrite: bool = False) -> WriteResult:
    """Write the chosen fields to ``path``.

    ``values`` is a parser dict keyed by :data:`FIELDS` (plus ``total_tracks``,
    ``total_discs``, ``compilation`` and ``'<base>_sort'``).
    ``apply_fields`` is the subset of :data:`FIELDS` the user opted into.
    Only fields with a derived value are written; existing values are preserved
    unless ``overwrite`` is True.
    """
    kind = format_kind(path)
    if kind == 'unsupported':
        return WriteResult(unsupported=True)

    apply_fields = set(apply_fields)
    res = WriteResult()

    try:
        tags, save = open_tags(path)
        present = _present(tags)

        for f in FIELDS:
            if f not in apply_fields or not has_home(kind, f):
                continue                    # e.g. no MP4 disc-subtitle atom
            val = values.get(f)
            if val is None or (isinstance(val, str) and not val.strip()):
                continue                    # nothing derived for this field
            if f in ('artist', 'album_artist', 'composer') and _is_placeholder(val):
                # "Various Artists" and friends are derived by the app, not stored.
                res.skipped_placeholder.append(f)
                continue
            if present.get(f) and not overwrite:
                res.skipped_existing.append(f)
                continue
            if f in ('track', 'disc'):
                total = values.get(f'total_{f}s')
                set_pair(tags, f, _num(val), _num(total) if total else '')
            else:
                set_text(tags, f, [str(val)])
            res.written.append(f)

        # Compilation flag rides along with the album artist when detected.
        if values.get('compilation') and 'album_artist' in apply_fields:
            if overwrite or not present.get('compilation'):
                set_compilation(tags, True)
                res.written.append('compilation')

        # Sort-order tags ride with a base field *actually written* this run, so
        # the sort string always matches the value we wrote (not a skipped one).
        if 'sort' in apply_fields:
            written_now = set(res.written)
            for base, sort_field in _SORT_MAP.items():
                sval = values.get(f'{base}_sort')
                if base in written_now and sval:   # none needed for e.g. "Radiohead"
                    set_text(tags, sort_field, [str(sval)])
                    res.written.append(f'{base}_sort')

        if res.written:
            save()                      # ID3 is v2.4 iff a multi-value frame is present
    except Exception as e:                  # never let one bad file abort a bulk run
        return WriteResult(error=str(e))

    return res


def clear_fields(path: str, fields) -> WriteResult:
    """Remove the chosen fields from ``path`` entirely, whatever its kind.

    `write_fields` can only *set* a value (it skips anything empty), so removing
    a tag needs its own path. Fields already absent aren't reported as written, so
    a no-op file isn't rewritten.
    """
    if format_kind(path) == 'unsupported':
        return WriteResult(unsupported=True)

    res = WriteResult()
    try:
        tags, save = open_tags(path)
        res.written = [f for f in fields if delete(tags, f)]
        if res.written:
            save()
    except Exception as e:                  # never let one bad file abort a bulk run
        return WriteResult(error=str(e))

    return res


def write_replaygain(path: str, gain_db: float, peak: float) -> None:
    """Store a track's ReplayGain (gain in dB, linear peak) where its kind keeps
    it; ID3 also gets an RVA2 frame, which players that ignore the text read."""
    tags, save = open_tags(path)
    set_text(tags, 'replaygain_track_gain', [f"{gain_db:+.2f} dB"])
    set_text(tags, 'replaygain_track_peak', [f"{peak:.6f}"])
    if format_kind(path) == 'id3':
        from backtrack.id3.tag_handler import create_frame
        tags.delall('RVA2')
        if (rva2 := create_frame('RVA2', {'__rva2__': True, 'gain': gain_db})) is not None:
            tags.add(rva2)
    save()


def stale_length_tags(path: str) -> tuple[bool, bool]:
    """(has_tlen, has_stale_tdly) for path. MP3 only: nothing else has frames that go stale on a cut.

    TLEN (track length, ms) is suspect the instant a file is cut by any means,
    including outside backtrack. TDLY (playlist delay, ms) is equally suspect
    after a head cut, but a zero value still means "no delay" and isn't stale.
    """
    if not is_mp3(path):
        return (False, False)
    audio = load_id3(path)
    has_tlen = bool(audio.getall('TLEN'))
    has_tdly = any(t.text and str(t.text[0]) != '0' for t in audio.getall('TDLY'))
    return (has_tlen, has_tdly)


# ---------------------------------------------------------------------------
# Album art (APIC / covr), used by the per-file album-art bulk op
# ---------------------------------------------------------------------------

def has_cover(path: str) -> bool:
    """Whether the file already carries embedded album art. False on read errors
    so a bad file is treated as blank (and the fill-blanks preview offers to fill)."""
    return embedded_cover(path) is not None


def retype_cover(path: str, pic_type: int) -> WriteResult:
    """Set the *picture type* of the art already on ``path``, keeping the image.

    ID3 only (MP3/WAV/AIFF): MP4's ``covr`` atom has no picture-type field, so
    anything else comes back `unsupported`. Frames already carrying `pic_type`
    aren't rewritten, so a second run over the same files is a no-op.
    """
    if format_kind(path) != 'id3':
        return WriteResult(unsupported=True)

    res = WriteResult()
    try:
        from backtrack.id3.tag_handler import create_apic_frame
        audio = load_id3(path)
        frames = audio.getall('APIC')
        stale = [f for f in frames if int(getattr(f, 'type', 3)) != int(pic_type)]
        if not stale:
            return res
        for frame in stale:
            rebuilt = create_apic_frame(getattr(frame, 'data', b''),
                                        getattr(frame, 'mime', 'image/jpeg'),
                                        int(pic_type),
                                        getattr(frame, 'desc', '') or '')
            if rebuilt is None:
                return WriteResult(error='could not build the APIC frame')
            # Delete by the frame's own key: mutagen keys APIC by description.
            audio.delall(getattr(frame, 'HashKey', 'APIC'))
            audio.add(rebuilt)
            res.written.append('cover_type')
        save_id3(audio, path)
    except Exception as e:                         # never abort a bulk run
        return WriteResult(error=str(e))

    return res


def _vorbis_picture(data: bytes, mime: str, pic_type: int, desc: str) -> Picture:
    pic = Picture()
    pic.data, pic.mime, pic.type, pic.desc = data, mime, pic_type, desc
    return pic


def _set_vorbis_cover(path: str, pic: Picture | None) -> None:
    """Make `pic` a FLAC's or Ogg file's only picture (None: remove them all).
    FLAC keeps pictures in their own blocks; Ogg in METADATA_BLOCK_PICTURE comments."""
    f = mutagen.File(path)
    if f is None:
        raise ValueError("not a readable audio file")
    if hasattr(f, 'clear_pictures'):
        f.clear_pictures()
        if pic is not None:
            f.add_picture(pic)
    else:
        if f.tags is None:
            f.add_tags()
        for key in ('METADATA_BLOCK_PICTURE', 'COVERART', 'COVERARTMIME'):
            if key in f.tags:
                del f.tags[key]
        if pic is not None:
            f.tags['METADATA_BLOCK_PICTURE'] = [base64.b64encode(pic.write()).decode('ascii')]
    f.save()


def write_cover(path: str, data: bytes, mime: str, *, pic_type: int = 3,
                desc: str = '', overwrite: bool = False) -> WriteResult:
    """Embed ``data`` as album art on ``path`` (an ID3 APIC, an MP4 ``covr``, or
    a FLAC/Ogg picture), as its only cover.

    Honours fill-blanks: a file that already has art is left untouched unless
    ``overwrite`` is set (reported via ``skipped_existing``). MP4 ``covr`` only
    holds JPEG/PNG: anything else returns ``skipped_format=True`` rather than
    silently writing nothing.
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

        if kind == 'id3':
            from backtrack.id3.tag_handler import create_apic_frame
            audio = load_id3(path)
            frame = create_apic_frame(data, mime, pic_type, desc)
            if frame is None:
                return WriteResult(error='could not build APIC frame')
            audio.delall('APIC')
            audio.add(frame)
            save_id3(audio, path)
        elif kind == 'vorbis':
            _set_vorbis_cover(path, _vorbis_picture(data, mime, pic_type, desc))
        else:  # mp4
            fmt = (MP4Cover.FORMAT_PNG if mime == 'image/png'
                   else MP4Cover.FORMAT_JPEG if mime == 'image/jpeg' else None)
            if fmt is None:
                return WriteResult(skipped_format=True)
            audio = MP4(path)
            if audio.tags is None:
                audio.add_tags()
            audio.tags['covr'] = [MP4Cover(data, imageformat=fmt)]
            audio.save()
    except Exception as e:
        return WriteResult(error=str(e))

    res.written.append('cover')
    return res


def remove_cover(path: str) -> WriteResult:
    """Remove every embedded picture from ``path``."""
    kind = format_kind(path)
    if kind == 'unsupported':
        return WriteResult(unsupported=True)
    if not has_cover(path):
        return WriteResult()
    try:
        if kind == 'id3':
            audio = load_id3(path)
            audio.delall('APIC')
            save_id3(audio, path)
        elif kind == 'vorbis':
            _set_vorbis_cover(path, None)
        else:
            audio = MP4(path)
            audio.tags.pop('covr', None)
            audio.save()
    except Exception as e:
        return WriteResult(error=str(e))
    return WriteResult(written=['cover'])
