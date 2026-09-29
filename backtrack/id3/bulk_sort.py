"""Bulk sort orders: filling the sort tags, with the split and people review screens."""
from __future__ import annotations
import re
import mutagen.id3
from mutagen.id3 import ID3
from backbone import prompt
from backtrack.id3 import bulk_ops as bo
from backtrack.id3 import tag_registry as _reg
from backbone import ui
from backtrack.id3.bulk_common import _SKIP, _SORT_SRC, _sort_value, _walk
from backtrack.config import setting
from backtrack.music_library import first_text


_SORT_VALUE_COLUMNS = [
    prompt.Column(style='primary', flex=True, max_frac=0.4),                      # the name itself
    prompt.Column(style='dynamic-dim', flex=True),                                # sort order
    prompt.Column(style='dynamic-dim', max_width=22, priority=2),                 # where it came from
    prompt.Column(style='dynamic-dim', align='right', max_width=9, priority=1),   # how many files
]


_SPLIT_COLUMNS = [
    prompt.Column(style='primary', flex=True, max_frac=0.4),                      # the value as tagged
    prompt.Column(style='dynamic-dim', flex=True),                                # the names in it
    prompt.Column(style='dynamic-dim', max_width=8, priority=2),                  # how many
    prompt.Column(style='dynamic-dim', align='right', max_width=9, priority=1),   # how many files
]


_SORT_TAG_LABEL = {t.frame: t.label for t in _reg.SORT_TAGS}


class _SortPlan:
    """What a bulk sort-order run will write, keyed by source value, not by file.

    One row per distinct artist/album value however many tracks carry it, so a
    decision made once covers all of them. Decisions are held per *person*
    rather than per value: settling "Somebody Else" as "Else, Somebody" in one
    collaboration settles it in every other value that person appears in, and on
    their solo tracks, which is the copying-down that makes reviewing a library
    of repeats bearable.
    """

    def __init__(self, entries: dict, delim: str) -> None:
        from backtrack.id3 import browser
        self._nb = browser                 # lazy: pulls in cv2, numpy and the editors
        self.entries = entries                 # (sort_tag, raw) → [paths]
        self.delim = delim
        self.chosen: dict[str, str] = {}       # person → the sort text they were given
        self.splits: dict[str, list] = {}      # raw → the names it was verified to hold
        self.custom: dict[tuple, str] = {}     # entry → a whole value typed by hand

    # ── Splitting a value into people ──────────────────────────────────────
    def split_options(self, sort_tag: str, raw: str) -> list:
        """The ways this value could be read as a list of names (album tags: none)."""
        if sort_tag not in self._nb._NAME_SORT_TAGS:
            return []
        return self._nb.split_options(raw)

    def people(self, sort_tag: str, raw: str) -> list:
        """The individuals in one value: as verified, else as the engine reads it."""
        options = self.split_options(sort_tag, raw)
        if not options:
            return []
        return self.splits.get(raw) or options[0]

    def set_split(self, raw: str, people: list) -> None:
        """Record how a value really divides (the verification step's whole job)."""
        self.splits[raw] = list(people)

    # ── Sort values ────────────────────────────────────────────────────────
    def candidates(self, person: str) -> list:
        """Ranked sort orders offered for one person, their current pick first."""
        cur = self.person_sort(person)
        out = [cur]
        for c in self._nb._sort_single_name(person):
            if c not in out:
                out.append(c)
        if person not in out:
            out.append(person)                 # "leave it alone" is always an option
        return out

    def person_sort(self, person: str) -> str:
        """How one person sorts: their decision if made, else the engine's pick."""
        if person in self.chosen:
            return self.chosen[person]
        if self._nb._looks_inverted(person):
            return person                      # already in sort order
        return (self._nb._sort_single_name(person) or [person])[0]

    def value(self, sort_tag: str, raw: str) -> str:
        """The sort string this entry would write."""
        typed = self.custom.get((sort_tag, raw))
        if typed:
            return typed
        people = self.people(sort_tag, raw)
        if not people:
            return _sort_value(sort_tag, raw) or raw
        return self.delim.join(self.person_sort(p) for p in people)

    def writes(self, sort_tag: str, raw: str) -> bool:
        """Whether this entry still has something worth writing."""
        v = self.value(sort_tag, raw)
        return bool(v) and v != raw


def _split_keys(plan: _SortPlan) -> list:
    """Values that could be read more than one way: the only ones worth checking."""
    keys = [k for k in plan.entries if len(plan.split_options(*k)) > 1]
    keys.sort(key=lambda k: k[1].lower())
    return keys


def _verify_splits(plan: _SortPlan, header, note: str = "") -> bool:
    """Confirm how each value divides into names, before any of them is sorted.

    Splitting is guesswork: an ampersand joins two artists in one credit and is
    part of one act's name in the next, and a comma does three different jobs,
    and every sort order downstream is built on the answer. So the guesses come
    first, one row per value that could be read more than one way, and `e` cycles
    the readings: the engine's, the value whole as a single name, the maximal
    split, or one typed by hand with " / " between the names.

    Values with only one reading never appear; nor do albums, which hold nobody.
    Returns False if the step was abandoned.
    """
    keys = _split_keys(plan)
    if not keys:
        return True

    def _shown(people: list) -> str:
        return " / ".join(people)

    def _cells(key: tuple) -> list:
        people = plan.people(*key)
        return [key[1], _shown(people),
                ui.plural(len(people), 'name'),
                ui.plural(len(plan.entries[key]), 'file')]

    rows = {k: prompt.Choice(title=k[1], value=k, cells=_cells(k)) for k in keys}

    def _options(key: tuple) -> list:
        return [_shown(o) for o in plan.split_options(*key)]

    def _commit(key: tuple, text: str) -> None:
        """Take the chosen (or typed) splitting for this value."""
        people = [p.strip() for p in re.split(r'\s*[/·]\s*', text) if p.strip()]
        if people:
            plan.set_split(key[1], people)
            rows[key].cells = _cells(key)

    sub = f"{ui.plural(len(keys), 'value')} to check{note}"
    sel = prompt.select("Do these divide correctly? (↵ continues)",
                        choices=[rows[k] for k in keys], columns=_SPLIT_COLUMNS,
                        header=header(sub), row_edit=_options,
                        row_edit_commit=_commit, row_edit_col=1)
    return sel is not None


def _review_sort_people(plan: _SortPlan, header, note: str = "") -> set | None:
    """One flat list of everything that needs a sort order, each thing once.

    Not one row per tag value but one per *individual*: an artist, an album
    artist and a composer with the same name are one person and one decision,
    however many tracks and however many collaborations they turn up in. Albums,
    having nobody in them, are rows of their own. Who the people are was settled
    in the split-verification step before this one.

    `e` cycles a row's sort order through its candidates in place, one press per
    option, with one step past the last being a text field to type your own; no
    second screen for any of it. ↵ writes the checked rows; unchecking a person
    leaves every value they appear in alone.
    """
    def _index() -> tuple:
        """Group the entries into people and albums."""
        people: dict = {}
        albums = []
        for key, paths in plan.entries.items():
            sort_tag, raw = key
            if not plan.split_options(sort_tag, raw):
                albums.append(key)
                continue
            for person in plan.people(sort_tag, raw):
                rec = people.setdefault(person, {'paths': set(), 'tags': set()})
                rec['paths'].update(paths)
                rec['tags'].add(_SORT_TAG_LABEL.get(sort_tag, sort_tag))
        return people, albums

    def _rows() -> list:
        """The flat list: people, then albums."""
        people, albums = _index()
        out = []
        for name in sorted(people, key=str.lower):
            rec = people[name]
            out.append((('person', name),
                        [name, plan.person_sort(name),
                         " · ".join(sorted(rec['tags'])),
                         ui.plural(len(rec['paths']), 'file')]))
        for key in sorted(albums, key=lambda k: k[1].lower()):
            out.append((('album', key),
                        [key[1], plan.value(*key), 'album',
                         ui.plural(len(plan.entries[key]), 'file')]))
        return out

    # Rows are rebuilt on every edit, but Choice objects are reused where the row
    # survives, so checkbox state and the cursor stay put.
    made: dict = {}

    def _choices() -> list:
        out = []
        for rid, cells in _rows():
            choice = made.get(rid)
            if choice is None:
                choice = made[rid] = prompt.Choice(title=cells[0], value=rid, checked=True)
            choice.cells = cells
            out.append(choice)
        return out

    def _options(rid: tuple) -> list:
        """What `e` cycles this row through, its current value first."""
        kind, payload = rid
        if kind == 'person':
            return plan.candidates(payload)
        return [plan.value(*payload)] + list(plan._nb._sort_candidates(*payload))

    def _commit(rid: tuple, text: str) -> None:
        """Record one row's choice, then refresh them all (a person reaches many)."""
        kind, payload = rid
        if kind == 'person':
            plan.chosen[payload] = text
        else:
            plan.custom[payload] = text
        for rid_, cells in _rows():
            if rid_ in made:
                made[rid_].cells = cells

    choices = _choices()
    files = len({p for ps in plan.entries.values() for p in ps})
    sub = f"{ui.plural(len(choices), 'name')} · {ui.plural(files, 'file')}{note}"
    sel = prompt.select("Preview (↵ applies):", choices=choices,
                        columns=_SORT_VALUE_COLUMNS, header=header(sub), multi=True,
                        row_edit=_options, row_edit_commit=_commit, row_edit_col=1)
    return None if sel is None else set(sel)


def apply_sort_orders(paths: list, library: list, header) -> None:
    """Generate smart sort-order tags (TSOP/TSO2/TSOC/TSOA) from each file's
    existing artist/album-artist/composer/album, via the sort-order engine in browser. MP3/ID3 only.

    Runs as a sequence of screens you can walk backwards through: back on the
    first one leaves, and back on any other returns to the one before it with
    everything you had already decided still there. Splits are confirmed before
    sort orders (see _verify_splits), then reviewed one individual at a time
    (_review_sort_people). No title sort: a title sorts on itself, so TSOT is
    never generated. Nor is a sort tag whose value would equal its source.
    """
    mp3s = [p for p in paths if p.lower().endswith('.mp3')]
    skipped_fmt = len(paths) - len(mp3s)
    note = f" · {skipped_fmt} non-MP3 skipped" if skipped_fmt else ""

    from backtrack.config import load_config
    from backtrack.id3 import browser as nb
    delim = setting(load_config(), 'sort_list_delimiter')

    _FIELDS = [('artist', "Artist sort (TSOP)"), ('album_artist', "Album-artist sort (TSO2)"),
               ('composer', "Composer sort (TSOC)"), ('album', "Album sort (TSOA)")]
    _MODES = ["Fill blanks only", "Overwrite existing"]

    chosen: set = {f for f, _ in _FIELDS}
    mode = _MODES[0]
    plan: _SortPlan | None = None
    scanned_for: tuple | None = None       # what the current plan was scanned for
    checked: set = set()

    def _scan(overwrite: bool) -> dict:
        """Read the selection into (sort_tag, raw) → paths."""
        entries: dict = {}
        for p in mp3s:
            try:
                audio = ID3(p)
            except (mutagen.id3.ID3NoHeaderError, OSError):  # type: ignore[reportPrivateImportUsage]
                continue
            for field, src, sort_tag in _SORT_SRC:
                if field not in chosen:
                    continue
                fr = audio.get(src)
                # A source frame can hold several values (TCOM takes one
                # composer each) while the sort frame is single, so they join on
                # the delimiter and the engine reads them straight back as a list.
                vals = [str(t).strip() for t in fr.text if str(t).strip()] if (
                    fr and getattr(fr, 'text', None)) else []
                raw = delim.join(vals)
                if not raw:
                    continue
                # A value that sorts as itself is still worth carrying when it
                # could be split more than one way: that judgement is the
                # verification step's to confirm, and "Blank & Jones" needing no
                # tag is exactly the sort of guess someone may want to overrule.
                if not _sort_value(sort_tag, raw) and not (
                        sort_tag in nb._NAME_SORT_TAGS and len(nb.split_options(raw)) > 1):
                    continue
                ex = audio.get(sort_tag)
                if not overwrite and first_text(ex):
                    continue
                entries.setdefault((sort_tag, raw), []).append(p)
        return entries

    def _ask_fields() -> bool:
        """Which sort frames to generate."""
        nonlocal chosen
        picked = prompt.select(
            "Sort tags to generate:", multi=True, header=header(),
            choices=[prompt.Choice(title=label, value=f, checked=f in chosen)
                     for f, label in _FIELDS])
        if not picked:
            return False                   # first screen: back leaves the op
        chosen = set(picked)
        return True

    def _ask_mode() -> bool:
        """Fill blanks, or overwrite what is already there."""
        nonlocal mode
        picked = prompt.select("When a sort tag already has a value:", choices=_MODES,
                               index=_MODES.index(mode), header=header())
        if not picked:
            return False
        mode = picked
        return True

    def _read_files():
        """Not a screen: re-read the files when an answer above has changed.

        Transparent to the walk in both directions (it steps aside once the plan
        matches the answers) but reports a back when the selection turns up
        nothing, so the walk lands on the question worth changing.
        """
        nonlocal plan, scanned_for
        overwrite = (mode == "Overwrite existing")
        if scanned_for == (frozenset(chosen), overwrite):
            return _SKIP
        entries = _scan(overwrite)
        if not entries:
            ui.show_status("No sort orders to write: already set, or none needed.")
            return False
        # Carry every decision across the rescan: they are keyed by person and by
        # value, so they outlive the entries they were made against.
        fresh = _SortPlan(entries, delim)
        if plan is not None:
            fresh.chosen.update(plan.chosen)
            fresh.splits.update(plan.splits)
            fresh.custom.update(plan.custom)
        plan = fresh
        scanned_for = (frozenset(chosen), overwrite)
        return _SKIP

    def _ask_verify():
        """Confirm who the people are; skipped when nothing reads two ways."""
        assert plan is not None
        if not _split_keys(plan):
            return _SKIP
        return _verify_splits(plan, header, note)

    def _ask_review() -> bool:
        """The flat list of individuals, and what gets written."""
        nonlocal checked
        assert plan is not None
        got = _review_sort_people(plan, header, note)
        if got is None:
            return False
        checked = got
        return True

    if not _walk([_ask_fields, _ask_mode, _read_files, _ask_verify, _ask_review]):
        return

    if not checked:
        ui.show_status("Nothing selected.")
        return
    assert plan is not None

    # Rows are people; values are what gets written. A value goes out when
    # everyone in it is checked; unchecking one person leaves every value they
    # appear in untouched rather than half-sorted.
    per_path: dict = {}
    for key in plan.entries:
        sort_tag, raw = key
        people = plan.people(sort_tag, raw)
        if people:
            if any(('person', person) not in checked for person in people):
                continue
        elif ('album', key) not in checked:
            continue
        if not plan.writes(sort_tag, raw):
            continue
        for p in plan.entries[key]:
            per_path.setdefault(p, []).append((sort_tag, plan.value(sort_tag, raw)))

    applied = bo.apply_frame_writes(per_path, library)
    applied.skipped = skipped_fmt
    ui.show_status(bo.summarise(applied, "Wrote sort orders for",
                                      skipped_note="non-MP3 skipped"))
