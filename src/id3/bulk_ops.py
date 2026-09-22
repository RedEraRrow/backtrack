"""The plan/apply core behind the bulk operations.

Every one of these operations has the same five steps — read the files, work out
what each one becomes, show it, write the ticked rows, say what happened — and
four of them had grown their own copy of the last two. The copies had already
drifted (one counted a skipped file as an error, another refreshed the library
entry only on some paths), which is the usual cost of writing an apply loop five
times.

This module owns the halves that are not a screen: a `plan_*` reads the files and
returns what each one would become, and `apply_changes` writes a plan. The bulk
menu supplies the preview between them; the CLI supplies flags. Neither owns the
logic, so they cannot disagree about what an operation does.

Per the house recipe (docs/DEVELOPER.md), the transforms themselves stay in the
pure modules — `bulk_pattern` for numbering, `filename_parser` for derivation,
`file_namer` for names, `cover_matcher` for art. This layer is the file I/O
either side of them.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Callable

import mutagen.id3
from mutagen.id3 import ID3

from src import bulk_pattern as bp
from src.id3 import tag_writer as tw
from src.id3.id3_tag_handler import apply_bulk_edit, save_id3
from src.music_library import refresh_library_entry


@dataclass
class Change:
    """What one file would become.

    `why` is the one-line reason shown in the preview ("disc 1/1 → —"); an empty
    `why` means this file is a candidate the operation leaves alone, which is
    still listed — unticked — so the selection is visibly complete rather than
    silently filtered. `fields` is the same decision as data, for `--json` and
    `--dry-run`.
    """
    path: str
    why: str = ""
    fields: dict = field(default_factory=dict)

    @property
    def changed(self) -> bool:
        """Whether this file would actually be written."""
        return bool(self.why)


@dataclass
class Plan:
    """Every candidate file for one operation, in the order it will be shown.

    `skipped` counts files the operation cannot touch at all (wrong format).
    `message` is set when the plan cannot proceed — no writable files, or nothing
    that needs changing — and carries the sentence to show instead of a preview.
    """
    changes: list[Change] = field(default_factory=list)
    skipped: int = 0
    message: str | None = None

    @property
    def changed(self) -> list[Change]:
        """The subset that would actually be written."""
        return [c for c in self.changes if c.changed]

    def __bool__(self) -> bool:
        """Truthy when there is something to preview."""
        return self.message is None and bool(self.changed)


@dataclass
class Applied:
    """What a run of `apply_changes` actually did.

    `kept` and `unsupported` come straight off `tag_writer.WriteResult`: a file
    that already had a value under fill-blanks, and one whose format cannot hold
    what was asked (an MP4 cover that isn't JPEG/PNG). Counted here rather than
    in each operation, because every operation that writes through `tag_writer`
    can produce them and most were quietly folding them into "written".
    """
    written: int = 0
    errors: int = 0
    skipped: int = 0
    kept: int = 0
    unsupported: int = 0


def _gather(paths: list, reader: Callable[[str], dict],
            accepts: Callable[[str], bool]) -> tuple[list, int]:
    """Read every acceptable file into a song dict; count the rest as skipped.

    Numbering is always read from the files rather than the library cache: these
    operations are what you reach for right after hand-numbering a disc, when the
    cache has not caught up. A stale cache flattened a multi-disc selection into
    one group and wrote a single continuous run over every disc.
    """
    songs = []
    for p in paths:
        if not accepts(p):
            continue
        try:
            songs.append(reader(p))
        except (OSError, mutagen.id3.ID3NoHeaderError):  # type: ignore[reportPrivateImportUsage]
            continue
    return songs, len(paths) - len(songs)


def apply_changes(plan: Plan, library: list, writer: Callable[[Change], object], *,
                  selected: set | None = None,
                  on_event: Callable[[str, Change, str], None] | None = None) -> Applied:
    """Write the selected changes, refreshing the library entry for each one.

    `writer` performs one file's write and returns a `tag_writer.WriteResult`, or
    None when it wrote by some other route (raising on failure). `selected`
    defaults to every changed file. `on_event` is called as
    `(kind, change, detail)` with kind `'written'`, `'skipped'` or `'error'` — it
    is how progress reaches a terminal and how `--json` emits one line per file.

    A failed library refresh is swallowed: the tag write already succeeded, and
    the background sync will pick the file up regardless.
    """
    sel = selected if selected is not None else {c.path for c in plan.changed}
    out = Applied(skipped=plan.skipped)
    for change in plan.changes:
        if change.path not in sel:
            continue
        try:
            result = writer(change)
        except Exception as exc:
            out.errors += 1
            _emit(on_event, 'error', change, str(exc))
            continue
        failure = getattr(result, 'error', None)
        if failure:
            out.errors += 1
            _emit(on_event, 'error', change, str(failure))
        elif getattr(result, 'skipped_format', False):
            out.unsupported += 1
            _emit(on_event, 'unsupported', change, change.why)
        elif getattr(result, 'skipped_existing', None) and not getattr(result, 'written', None):
            out.kept += 1
            _emit(on_event, 'kept', change, change.why)
        elif getattr(result, 'written', True):
            out.written += 1
            try:
                refresh_library_entry(library, change.path)
            except Exception:
                pass
            _emit(on_event, 'written', change, change.why)
        else:
            _emit(on_event, 'skipped', change, change.why)
    return out


def _emit(on_event, kind: str, change: Change, detail: str) -> None:
    """Report one file's outcome, if anyone is listening."""
    if on_event is not None:
        on_event(kind, change, detail)


def summarise(applied: Applied, verb: str, noun: str = "file",
              skipped_note: str = "unsupported skipped",
              kept_note: str = "kept existing (fill blanks only)",
              unsupported_note: str = "skipped (format cannot hold it)") -> str:
    """The one-line report every operation ends with.

    Reads "Renumbered 12 files. 2 unsupported skipped. 1 error." — the existing
    wording, in one place rather than five, and counted through `ui_utils.plural`
    like the previews above it. These five messages were the last "file(s)" left
    in the app.
    """
    from src.utils import ui_utils
    msg = f"{verb} {ui_utils.plural(applied.written, noun)}."
    if applied.kept:
        msg += f" {applied.kept} {kept_note}."
    if applied.unsupported:
        msg += f" {applied.unsupported} {unsupported_note}."
    if applied.skipped:
        msg += f" {applied.skipped} {skipped_note}."
    if applied.errors:
        msg += f" {ui_utils.plural(applied.errors, 'error')}."
    return msg


# ---------------------------------------------------------------------------
# The plans. Each reads the files, asks `bulk_pattern` what the numbering
# becomes, and describes the difference. No terminal output, no prompting.
# ---------------------------------------------------------------------------

def read_numbering(paths: list) -> tuple[list, int]:
    """Every writable file's stored track/disc numbering, in disc/track order.

    Returns `(ordered, skipped)`. Split from the plans because a preview screen
    re-plans every time it is shown — the numbering is read once, and changing
    the mode then costs no disc I/O.
    """
    songs, skipped = _gather(
        paths,
        lambda p: dict(tw.read_number_pairs(p), path=p),
        tw.is_writable)
    return bp.order_tracks(songs), skipped


def plan_renumber(ordered: list, skipped: int, mode: str) -> Plan:
    """Renumber track numbers continuously across the album or per disc.

    `mode` is 'continuous' or 'per_disc'. MP3 and MP4 both write.
    """
    if not ordered:
        return Plan(skipped=skipped, message="No MP3/MP4 tracks to renumber.")

    numbering = bp.renumber_tracks(ordered, mode)
    changes = []
    for s in ordered:
        trk, total = numbering[s['path']]
        was = str(s.get('track', '') or '?')
        changes.append(Change(
            path=s['path'],
            why=f"{was} → {trk}/{total}",
            fields={'track': trk, 'total_tracks': total}))
    return Plan(changes=changes, skipped=skipped)


def plan_reflow(ordered: list, skipped: int, *, renumber: bool = True,
                disc_totals: bool = True, track_totals: bool = False) -> Plan:
    """Re-flow disc numbering onto a dense 1…N and fix the totals.

    Renumbering the distinct disc values handles an inserted `1.5`, a deleted
    disc and an appended one with the same rule. Rows the reflow leaves exactly
    as they were are listed with no `why`, so they stay visible but unticked.
    """
    if not ordered:
        return Plan(skipped=skipped, message="No MP3/MP4 tracks to reflow.")

    runs = bp.disc_ranges(ordered)
    flowed = bp.reflow_discs(ordered, renumber=renumber,
                             disc_totals=disc_totals, track_totals=track_totals)

    def _old(s: dict, cur: str, tot: str) -> str:
        """The track's existing 'n/N' for a pair field, as stored."""
        raw = str(s.get(cur, '') or '').strip()
        if '/' in raw:
            return raw
        total = str(s.get(tot, '') or '').strip()
        return f"{raw or '?'}/{total}" if total else (raw or '?')

    def _new(f: dict, cur: str, tot: str) -> str:
        """The planned 'n/N' for a pair field."""
        return f"{f[cur]}/{f[tot]}" if tot in f else str(f[cur])

    changes = []
    for s in ordered:
        f = flowed[s['path']]
        bits = []
        d_old, d_new = _old(s, 'disc', 'total_discs'), _new(f, 'disc', 'total_discs')
        if d_old != d_new:
            bits.append(f"disc {d_old} → {d_new}")
        if track_totals:
            t_old, t_new = _old(s, 'track', 'total_tracks'), _new(f, 'track', 'total_tracks')
            if t_old != t_new:
                bits.append(f"track {t_old} → {t_new}")
        changes.append(Change(path=s['path'], why=' · '.join(bits), fields=f))

    plan = Plan(changes=changes, skipped=skipped)
    if not plan.changed:
        # Already dense with the right totals — say so, rather than showing an
        # all-unticked preview that ends in "No tracks selected".
        plan.message = (f"Disc numbering is already 1…{len(runs)} with matching "
                        "totals — nothing to do.")
    return plan


def plan_strip_single_disc(ordered: list, skipped: int) -> Plan:
    """Remove the disc number from tracks that are disc 1 of 1.

    A single-disc release doesn't need a disc tag: "1/1" is noise that shows up
    as a disc header in browse lists and in names derived from tags. A bare "1"
    with no total counts too, but only when nothing else in the selection sits on
    another disc — on a real multi-disc album an untotalled "1" is meaningful.
    """
    if not ordered:
        return Plan(skipped=skipped, message="No MP3/MP4 tracks to change.")

    single_disc = not {str(s['disc']).strip() for s in ordered} - {'', '1'}

    changes = []
    for s in ordered:
        disc = str(s['disc']).strip()
        total = str(s['total_discs']).strip()
        if not disc:
            why = ""                                   # nothing to remove
        elif total == '1':
            why = f"disc {disc}/{total} → —"
        elif not total and disc == '1' and single_disc:
            why = "disc 1 → —"
        else:
            why = ""
        stored = disc + (f"/{total}" if total else "")
        changes.append(Change(
            path=s['path'], why=why,
            fields={'clear': ['disc'], 'was': stored} if why else {'keeps': stored}))

    plan = Plan(changes=changes, skipped=skipped)
    if not plan.changed:
        plan.message = "No tracks are disc 1 of 1 — nothing to remove."
    return plan


def read_length_tags(paths: list) -> tuple[list, int]:
    """Every MP3's stale-length-tag state: `([{path, tags}], skipped)`."""
    return _gather(
        paths,
        lambda p: {'path': p, 'tags': tw.stale_length_tags(p)},
        lambda p: tw.format_kind(p) == 'mp3')


def plan_strip_length_tags(songs: list, skipped: int) -> Plan:
    """Remove stale TLEN (track length) and non-zero TDLY (playlist delay).

    Both hold millisecond values nothing recomputes once a file is cut by any
    means, including a trim done outside backtrack. MP3 only — neither frame has
    an MP4 analogue.
    """
    if not songs:
        return Plan(skipped=skipped, message="No MP3 tracks to check.")

    changes = []
    for s in songs:
        tlen, tdly = s['tags']
        stale = [name for name, present in (('TLEN', tlen), ('TDLY', tdly)) if present]
        changes.append(Change(path=s['path'], why=' · '.join(stale),
                              fields={'delete': stale}))

    plan = Plan(changes=changes, skipped=skipped)
    if not plan.changed:
        plan.message = "No stale TLEN/TDLY tags found."
    return plan


def strip_length_writer(change: Change) -> None:
    """Delete the stale frames named in a `plan_strip_length_tags` change.

    Not a `tag_writer` call — `write_fields` can only set a value, and these are
    raw ID3 frames with no cross-format equivalent.
    """
    audio = ID3(change.path)
    for frame in change.fields.get('delete', ()):
        apply_bulk_edit(audio, frame, 'delete')
    save_id3(audio, change.path)


def picture_type_name(pic_type) -> str:
    """Human label for an APIC picture-type byte ("Cover (front)", "Other"…)."""
    from src.id3.id3_tag_handler import _PICTURE_TYPES
    return dict(_PICTURE_TYPES).get(int(pic_type), f"type {pic_type}")


def read_picture_types(paths: list) -> tuple[list, int]:
    """Every MP3 in `paths` that has embedded art, with the types it carries.

    Read on its own because the picture-type picker's header shows what is
    already in the selection ("Other ×495 · Cover (front) ×411") before asking
    what to change it to — so the read happens before the question, not after.
    """
    art = []
    for path in paths:
        if not path.lower().endswith('.mp3'):
            continue
        try:
            tags = ID3(path)
        except (mutagen.id3.ID3NoHeaderError, OSError):  # type: ignore[reportPrivateImportUsage]
            continue
        frames = [tags[k] for k in tags if k.startswith('APIC')]
        if frames:
            art.append({'path': path,
                        'types': [int(getattr(f, 'type', 3)) for f in frames]})
    return art, len(paths) - len(art)


def plan_set_picture_type(art: list, skipped: int, pic_type: int) -> Plan:
    """Retype embedded art without touching the image itself.

    Rippers routinely tag a front cover as "Other" (type 0), which anything
    looking specifically for a front cover then misses. MP3 only — MP4's `covr`
    atom has no type field.
    """
    _name = picture_type_name
    if not art:
        return Plan(skipped=skipped,
                    message="No MP3s with embedded art in this selection.")

    changes = []
    for a in art:
        stale = [t for t in a['types'] if t != pic_type]
        was = ' · '.join(_name(t) for t in a['types'])
        changes.append(Change(
            path=a['path'],
            why=f"{was} → {_name(pic_type)}" if stale else "",
            fields={'picture_type': pic_type, 'was': a['types']}))

    plan = Plan(changes=changes, skipped=skipped)
    if not plan.changed:
        plan.message = f"Every image is already {_name(pic_type)}."
    return plan


def position_rows(plan: Plan) -> list[tuple[int, str, str]]:
    """`(position, basename, why)` per change — the preview's three columns.

    Shared so the bulk menu's table and the CLI's table cannot drift apart in
    what they show or the order they show it in.
    """
    return [(i + 1, os.path.basename(c.path), c.why)
            for i, c in enumerate(plan.changes)]


# ---------------------------------------------------------------------------
# Derivation. `filename_parser` does the parsing; these decide what of it is
# actually written, which is the half the preview and the CLI both need.
# ---------------------------------------------------------------------------

def _num_pair(num, total) -> str:
    """Format a "num/total" string, or just "num" when total is falsy."""
    return f"{num}/{total}" if total else f"{num}"


def _sort_value(base_id: str, raw: str) -> str | None:
    """Top smart sort-order candidate for a value, or None when none is needed.

    Reuses the sort engine (name-inversion for artists, article-move for
    album/title). "Various Artists" sorts as itself, so no tag is generated.
    """
    from src.id3.id3_tag_handler import is_placeholder_name
    if not raw or is_placeholder_name(raw):
        return None                     # derived names sort as themselves
    from src.id3.id3_browser import _sort_candidates
    cands = _sort_candidates(base_id, str(raw))
    return cands[0] if cands else None


def sort_base() -> list[tuple[str, str]]:
    """base field -> sort frame id, for the fields a derive run produces.

    The `auto` rows of the canonical table, so composer — never derived from a
    filename — is not among them.
    """
    from src.id3 import tag_registry as _reg
    return [(t.field, t.frame) for t in _reg.SORT_TAGS if t.auto]


def augment_sort(vals: dict, apply_fields: set) -> dict:
    """Add '<base>_sort' entries for the derived fields being written."""
    for base, frame in sort_base():
        if base in apply_fields and vals.get(base):
            sv = _sort_value(frame, str(vals[base]))
            if sv:
                vals[f'{base}_sort'] = sv
    return vals


def plan_write(derived, apply_fields: set, overwrite: bool,
               present: dict, path: str) -> dict:
    """Fields that would actually be written for one file: {field: value_str}.

    Honours fill-blanks (skip fields already present unless overwrite), only
    includes fields with a derived value, and — crucially — only fields the
    file's *format* can store (so the preview never claims a write it can't
    perform, e.g. disc subtitle on MP4). Pure — used for the preview and tests.
    """
    supported = tw.writable_fields(path)
    d = derived.as_dict()
    planned: dict = {}
    for f in tw.FIELDS:
        if f not in apply_fields or f not in supported:
            continue
        val = d.get(f)
        if val is None or (isinstance(val, str) and not val.strip()):
            continue
        if present.get(f) and not overwrite:
            continue
        if f == 'track':
            planned[f] = _num_pair(d['track'], d.get('total_tracks'))
        elif f == 'disc':
            planned[f] = _num_pair(d['disc'], d.get('total_discs'))
        else:
            planned[f] = str(val)
    return planned


def plan_derive(paths: list, apply_fields: set, *, overwrite: bool = False,
                template: str | None = None, regex: str | None = None,
                regex_base: str | None = None) -> tuple[Plan, dict]:
    """Derive tags from file and folder names for every writable file.

    Returns `(plan, derived)` — the plan drives the preview and the write, and
    the raw `derived` mapping is kept because the detail view shows what was
    parsed, including the fields the plan then declines to write.
    """
    from src.id3 import filename_parser as fp

    writable = [p for p in paths if tw.is_writable(p)]
    skipped = len(paths) - len(writable)
    if not writable:
        return Plan(skipped=skipped, message="No MP3/MP4 tracks to derive from."), {}

    derived = fp.derive_all(writable, template=template, regex=regex,
                            regex_base=regex_base)
    present = {p: tw.present_fields(p) for p in writable}
    changes = []
    for p in writable:
        planned = plan_write(derived[p], apply_fields, overwrite, present[p], p)
        changes.append(Change(
            path=p,
            why=' · '.join(f"{f}={v}" for f, v in planned.items()),
            fields=planned))

    plan = Plan(changes=changes, skipped=skipped)
    if not plan.changed:
        plan.message = ("Nothing to write — selected fields are already set "
                        "(try Overwrite).")
    return plan, derived


def derive_writer(derived: dict, apply_fields: set, overwrite: bool) -> Callable:
    """A writer for `apply_changes` that writes one file's whole derivation.

    The full derived value set is written rather than the planned subset, so
    `tag_writer` keeps its own fill-blanks and placeholder handling; the plan is
    what decides whether the file is offered at all.
    """
    def _write(change: Change):
        """Write one derived file, adding sort orders when they were asked for."""
        vals = derived[change.path].as_dict()
        if 'sort' in apply_fields:
            augment_sort(vals, apply_fields)
        return tw.write_fields(change.path, vals, apply_fields, overwrite=overwrite)
    return _write


# ---------------------------------------------------------------------------
# Writes that are not a `tag_writer` field set: renaming the file itself, art,
# and raw ID3 frames.
# ---------------------------------------------------------------------------

def rename_files(pairs: list, library: list, *,
                 on_event: Callable[[str, Change, str], None] | None = None) -> Applied:
    """Rename `(old, new)` pairs collision-safely, keeping the library in step.

    Two phases, because a target can be another selected file's current name:
    every source moves to a unique temp name first, then each temp to its final
    name. A failure in phase two rolls that one file back rather than leaving it
    parked under a dot-name.
    """
    import tempfile

    out = Applied()
    staged: list[tuple[str, str, str]] = []
    for orig, final in pairs:
        try:
            fd, tmp = tempfile.mkstemp(prefix='.rn_', dir=os.path.dirname(orig),
                                       suffix=os.path.splitext(orig)[1])
            os.close(fd)
            os.replace(orig, tmp)
            staged.append((orig, tmp, final))
        except OSError as exc:
            out.errors += 1
            _emit(on_event, 'error', Change(path=orig), str(exc))

    for orig, tmp, final in staged:
        try:
            os.replace(tmp, final)
        except OSError as exc:
            out.errors += 1
            _emit(on_event, 'error', Change(path=orig), str(exc))
            try:
                os.replace(tmp, orig)   # roll this one back
            except OSError:
                pass
            continue
        out.written += 1
        for track in library:
            if track.get('path') == orig:
                track['path'] = final
                break
        try:
            refresh_library_entry(library, final)
        except Exception:
            pass
        _emit(on_event, 'written', Change(path=orig, why=os.path.basename(final)), final)
    return out


def apply_covers(covers: dict, library: list, *, overwrite: bool = False,
                 selected: set | None = None,
                 on_event: Callable[[str, Change, str], None] | None = None) -> Applied:
    """Embed `{track path: image path}`, reading each image at most once.

    An album's tracks nearly all share one cover, so the read is cached — without
    it a 40-track album decoded the same JPEG 40 times.
    """
    from src.id3 import cover_matcher as cm

    out = Applied()
    cache: dict[str, tuple | None] = {}
    for path, image in covers.items():
        if not image or (selected is not None and path not in selected):
            continue
        if image not in cache:
            cache[image] = cm.read_image(image)
        read = cache[image]
        change = Change(path=path, why=os.path.basename(image),
                        fields={'cover': image})
        if not read:
            out.errors += 1
            _emit(on_event, 'error', change, f"could not read {image}")
            continue
        data, mime = read
        result = tw.write_cover(path, data, mime, pic_type=3, desc='',
                                overwrite=overwrite)
        if result.skipped_format:
            out.unsupported += 1
            _emit(on_event, 'unsupported', change, "cover needs JPEG/PNG")
        elif result.skipped_existing:
            out.kept += 1
            _emit(on_event, 'kept', change, "already has art")
        elif result.error:
            out.errors += 1
            _emit(on_event, 'error', change, str(result.error))
        elif result.written:
            out.written += 1
            try:
                refresh_library_entry(library, path)
            except Exception:
                pass
            _emit(on_event, 'written', change, image)
    return out


def apply_frame_writes(per_path: dict, library: list, *,
                       overwrite: bool = True,
                       on_event: Callable[[str, Change, str], None] | None = None) -> Applied:
    """Write raw ID3 frames: `{path: [(frame_id, value), …]}`. MP3 only.

    The shared tail of *Assign by range/schedule* and *Apply sort orders*, which
    both replace whole frames rather than writing a `tag_writer` field. Under
    fill-blanks a frame that already holds text is left alone.
    """
    from src.id3.id3_tag_handler import create_frame

    out = Applied()
    for path, writes in per_path.items():
        change = Change(path=path,
                        why=' · '.join(f"{fid}={val}" for fid, val in writes),
                        fields={fid: val for fid, val in writes})
        try:
            try:
                audio = ID3(path)
            except mutagen.id3.ID3NoHeaderError:  # type: ignore[reportPrivateImportUsage]
                audio = ID3()
            changed = False
            for frame_id, value in writes:
                if not overwrite:
                    existing = audio.get(frame_id)
                    if existing is not None and getattr(existing, 'text', None) \
                            and str(existing.text[0]).strip():
                        continue
                frame = create_frame(frame_id, value)
                if frame is None:
                    continue
                audio.delall(frame_id)
                audio.add(frame)
                changed = True
            if not changed:
                out.kept += 1
                _emit(on_event, 'kept', change, change.why)
                continue
            save_id3(audio, path)
        except Exception as exc:
            out.errors += 1
            _emit(on_event, 'error', change, str(exc))
            continue
        out.written += 1
        try:
            refresh_library_entry(library, path)
        except Exception:
            pass
        _emit(on_event, 'written', change, change.why)
    return out
