"""Bulk: values holding several names ("A/B/C", "A; B", "A & B") split into a
multi-value tag, one name per value, with the split reviewed first."""
from __future__ import annotations
import os
import re

from backbone import prompt, ui
from backtrack.id3 import bulk_ops as bo
from backtrack.id3.bulk_common import preview_and_apply
from backtrack.id3.tag_formats import field_of_frame, has_home, kind as tag_kind, open_tags, set_text, texts

# Fields that can hold several names, and whether every name in them is a
# person (a writer always is; an artist may be an act whose name has "&" or a
# comma in it).
FIELDS = [('composer', "Composer", True), ('lyricist', "Lyricist", True), ('conductor', "Conductor", True),
          ('artist', "Artist", False), ('album_artist', "Album artist", False)]
_LABEL = {f: label for f, label, _p in FIELDS}
_PEOPLE = {f for f, _l, p in FIELDS if p}
DEFAULT_FIELDS = ('composer', 'lyricist', 'conductor')
_LIST_SEP = re.compile(r'\s*[;/|\\]\s*')       # what only ever separates names
# A field's sort tag, split alongside it when it lists the same names.
_SORTS = {f: field_of_frame(fr) for f, fr in (('composer', 'TSOC'), ('artist', 'TSOP'), ('album_artist', 'TSO2'))}


def options(field: str, raw: str) -> list[list[str]]:
    """The ways `raw` could divide, the default first: a writer's as the name
    engine reads it, an artist's only at the plain list separators."""
    from backtrack.id3 import browser
    listed = [p for p in _LIST_SEP.split(raw) if p]
    out = browser.split_options(raw)
    lead = out[0] if field in _PEOPLE else (listed if len(listed) > 1 else [raw])
    return [lead] + [o for o in out + [listed] if o != lead and o]


def read_candidates(paths: list, fields) -> tuple[dict, int]:
    """(field, value) → the files carrying it, for every value that could hold
    more than one name; and how many files have nowhere to keep tags."""
    entries: dict = {}
    skipped = 0
    for p in ui.progress_iter(paths, "Reading tags", name=os.path.basename):
        try:
            tags, _save = open_tags(p)
        except (OSError, ValueError):
            skipped += 1
            continue
        for f in fields:
            for raw in texts(tags, f):
                if any(len(o) > 1 for o in options(f, raw)):
                    entries.setdefault((f, raw), []).append(p)
    return entries, skipped


def plan_split(entries: dict, splits: dict, skipped: int = 0) -> bo.Plan:
    """One change per file: each of its fields' values replaced by the names
    they divide into (`splits`: value → names, as reviewed; else the default)."""
    def names(f, raw):
        return splits.get(raw) or options(f, raw)[0]
    by_path: dict = {}
    for (f, raw), paths in entries.items():
        for p in paths:
            by_path.setdefault(p, set()).add(f)
    changes = []
    for p in sorted(by_path):
        tags, _save = open_tags(p)
        new, why = {}, []
        for f in sorted(by_path[p]):
            old = texts(tags, f)
            vals = [n for raw in old for n in (names(f, raw) if (f, raw) in entries else [raw])]
            if vals != old:
                new[f] = vals
                why.append(f"{_LABEL[f]}: {' · '.join(vals)}")
                sort = texts(tags, _SORTS[f]) if f in _SORTS else []
                parts = [x for x in _LIST_SEP.split(sort[0]) if x] if len(sort) == 1 else []
                if len(parts) == len(vals) > 1:
                    new[_SORTS[f]] = parts
        changes.append(bo.Change(path=p, why="; ".join(why), fields={'split': new} if new else {}))
    if not any(c.changed for c in changes):
        return bo.Plan(changes=changes, skipped=skipped, message="Nothing to split.")
    return bo.Plan(changes=changes, skipped=skipped)


def write_split(change: bo.Change):
    """Write one file's split values."""
    from backtrack.id3.tag_writer import WriteResult
    try:
        tags, save = open_tags(change.path)
        k = tag_kind(change.path)
        done = [f for f, vals in change.fields['split'].items() if has_home(k, f)]
        for f in done:
            set_text(tags, f, change.fields['split'][f])
        if done:
            save()                       # ID3 goes to v2.4 once a frame holds several values
        return WriteResult(written=done)
    except Exception as e:               # one bad file never stops a bulk run
        return WriteResult(error=str(e))


class _Review:
    """What the split review step (bulk_sort._verify_splits) reads and sets."""
    def __init__(self, entries: dict) -> None:
        self.entries = entries
        self.splits: dict = {}

    def split_options(self, field: str, raw: str) -> list:
        return options(field, raw)

    def people(self, field: str, raw: str) -> list:
        return self.splits.get(raw) or options(field, raw)[0]

    def set_split(self, raw: str, people: list) -> None:
        self.splits[raw] = list(people)


def split_values_op(paths: list, library: list, header) -> None:
    """Screens: the fields, the review of how each value divides, then the
    preview of what each file becomes."""
    from backtrack.id3.bulk_sort import _verify_splits
    picked = prompt.select("Fields to split:", multi=True, header=header(),
                           choices=[prompt.Choice(title=label, value=f, checked=f in DEFAULT_FIELDS)
                                    for f, label, _p in FIELDS])
    if not picked:
        return
    entries, skipped = read_candidates(paths, picked)
    if not entries:
        ui.show_status("No values with more than one name.")
        return
    review = _Review(entries)
    if not _verify_splits(review, header):
        return
    plan = plan_split(entries, review.splits, skipped)
    if plan.message:
        ui.show_status(plan.message)
        return
    preview_and_apply(plan, library, header, write_split, "Split values on",
                      count=ui.plural(len(plan.changes), "track"), changing="splitting",
                      unchanged=lambda c: "already one name each", skipped=skipped)
