"""Assigning values across tracks: ranged or periodic patterns, and the people and
fraction (n/total) editors."""
from __future__ import annotations
import os
import mutagen.id3
from mutagen.id3 import ID3
from backbone import prompt
from src.id3.id3_tag_handler import get_tag_info, create_frame, load_id3, save_id3
from src.id3.tag_registry import parse_composite_tag_id
from src.id3 import tag_writer as tw
from src.id3 import bulk_ops as bo
from src import bulk_pattern as bp
from backbone import ui as ui_utils
from backbone.log import log, quietly
from src import tuning as tune
from src.music_library import refresh_library_entry, first_text
from src.id3.bulk_common import _RENUMBER_COLUMNS, _walk


# Columns for the bulk people editor: role · name · coverage/state.
_PEOPLE_COLUMNS = [
    prompt.Column(style='primary', flex=True, max_frac=0.45),           # role / character (kept)
    prompt.Column(style='normal', flex=True),                           # name / actor (kept)
    prompt.Column(style='dynamic-dim', align='right', pin=True, priority=1),  # N/total or state, drops first
]


def _people_apply(current: list, edits: dict, deletes: set, adds: list) -> list:
    """One file's new people list: apply edits (replace in place), deletes, and
    adds (appended if absent), preserving order and de-duplicating. Pure."""
    out: list = []
    for e in current:
        if e in deletes:
            continue
        out.append(edits.get(e, e))
    for a in adds:
        if a not in out:
            out.append(a)
    seen: set = set()
    return [e for e in out if not (e in seen or seen.add(e))]


def _read_people(path: str, tag_id: str) -> list | None:
    """The (role, name) pairs for a people tag on one MP3, or None if unreadable."""
    try:
        audio = ID3(path)
    except mutagen.id3.ID3NoHeaderError:  # type: ignore[reportPrivateImportUsage]
        return []
    except (OSError, IOError):
        return None
    fr = audio.get(tag_id)
    if fr is None or not hasattr(fr, 'people'):
        return []
    return [(str(r).strip(), str(n).strip()) for r, n in fr.people]


def bulk_people_editor(paths: list, tag_id: str, library: list, header) -> None:
    """Edit a people list (TMCL/TIPL) across many files by *entry*.

    Aggregates the tag's distinct ``role → name`` entries across the selected
    MP3s with an ``N/total`` coverage count. Editing or removing an entry acts on
    every file that has that exact pair; adding puts it in every file that lacks
    it. Per-file ordering is preserved (edits replace in place)."""
    info = get_tag_info(tag_id)
    label = info.name[0] if info else tag_id

    mp3s = [p for p in paths if p.lower().endswith('.mp3')]
    per_file: dict = {}
    for p in mp3s:
        pl = _read_people(p, tag_id)
        if pl is not None:
            per_file[p] = pl
    if not per_file:
        ui_utils.show_status(f"No writable MP3s for {label}.")
        return
    total = len(per_file)

    # Distinct entries in first-seen order, with coverage counts.
    counts: dict = {}
    order: list = []
    for pl in per_file.values():
        for e in pl:
            if e not in counts:
                counts[e] = 0
                order.append(e)
            counts[e] += 1
    rows = [{'orig': e, 'role': e[0], 'name': e[1], 'deleted': False} for e in order]

    while True:
        choices: list = []
        for i, r in enumerate(rows):
            if r['deleted']:
                state = 'removing'
            elif r['orig'] is None:
                state = 'add all'
            else:
                state = f"{counts[r['orig']]}/{total}"
            choices.append(prompt.Choice(title=f"{r['role']} → {r['name']}", value=i,
                                         cells=[r['role'] or '-', r['name'] or '-', state]))
        choices.append(prompt.separator())
        choices.append(prompt.Choice(title="＋  Add person to all files…", value="__add__"))
        choices.append(prompt.Choice(title="✔ Save changes", value="__save__"))

        sub = f"{ui_utils.plural(total, 'file')} · Enter a row to edit/remove"
        sel = prompt.select(f"Bulk edit {label}:", choices=choices,
                            columns=_PEOPLE_COLUMNS, header=header(sub),
                            shortcuts={'a': '__add__'}, extra_hints={'a': 'add'})
        if sel is None:
            return                                          # cancel: no writes
        if sel == '__add__':
            role = prompt.text(f"{label}: role / character:")
            if role is None:
                continue
            name = prompt.text(f"{label}: name / person:")
            if name is None:
                continue
            if role.strip() or name.strip():
                rows.append({'orig': None, 'role': role.strip(), 'name': name.strip(),
                             'deleted': False})
            continue
        if sel == '__save__':
            break
        # A row was chosen (its value is the int index) → edit / remove submenu.
        r = rows[int(sel)]
        act = prompt.select(f"{r['role']} → {r['name']}:",
                            choices=["Edit", ("Keep" if r['deleted'] else "Remove"), "Cancel"])
        if act == "Edit":
            nrole = prompt.text("Role / character:", default=r['role'])
            if nrole is None:
                continue
            nname = prompt.text("Name / person:", default=r['name'])
            if nname is None:
                continue
            r['role'], r['name'] = nrole.strip(), nname.strip()
        elif act in ("Remove", "Keep"):
            r['deleted'] = not r['deleted']

    # --- Build the change set, then apply per file (order preserved) ---
    edits: dict = {}      # orig pair → new pair
    deletes: set = set()
    adds: list = []
    for r in rows:
        new = (r['role'], r['name'])
        if r['orig'] is None:
            if not r['deleted'] and (r['role'] or r['name']):
                adds.append(new)
        elif r['deleted']:
            deletes.add(r['orig'])
        elif new != r['orig']:
            edits[r['orig']] = new

    if not (edits or deletes or adds):
        ui_utils.show_status("No changes.")
        return

    changed = failed = 0
    for p, current in per_file.items():
        deduped = _people_apply(current, edits, deletes, adds)
        if deduped == current:
            continue
        frame = create_frame(tag_id, deduped) if deduped else None
        if deduped and frame is None:
            failed += 1                    # keep the file's credits as they are
            continue
        try:
            audio = load_id3(p)
            audio.delall(tag_id)
            if frame is not None:
                audio.add(frame)
            save_id3(audio, p)
        except Exception as exc:
            failed += 1
            log.warning("people edit of %s failed: %s", p, exc)
            continue
        changed += 1
        refresh_library_entry(library, p)

    ui_utils.show_status(f"Updated {label} in {ui_utils.plural(changed, 'file')}."
                         + (f" {failed} couldn't be written." if failed else ""))


def bulk_fraction_editor(paths: list, tag_id: str, library: list, header) -> None:
    """Edit a fraction tag (``n/N``: TRCK / TPOS / MVIN) across many files.

    Whichever half the selection already agrees on is seeded and editable; the
    half that differs between files is shown as a dim ``──  (varies)`` and, left
    alone, keeps each file's own value.  So you can set one common disc total
    across tracks whose disc *numbers* differ (or renumber the discs while
    leaving mixed totals intact) without the editor flattening the other half to
    whatever the first file happened to hold.
    """
    info = get_tag_info(tag_id)
    label = info.name[0] if info else tag_id
    base_id, _, _ = parse_composite_tag_id(tag_id)

    mp3s = [p for p in paths if p.lower().endswith('.mp3')]
    skipped_fmt = len(paths) - len(mp3s)
    existing: dict = {}
    for p in mp3s:
        try:
            audio = ID3(p)
        except (mutagen.id3.ID3NoHeaderError, OSError):  # type: ignore[reportPrivateImportUsage]
            continue
        fr = audio.get(tag_id)
        raw = first_text(fr)
        cur, _, tot = raw.partition('/')
        existing[p] = (cur.strip(), tot.strip())
    if not existing:
        ui_utils.show_status(f"No MP3s carry {label}.")
        return

    currents = {c for c, _ in existing.values()}
    totals = {t for _, t in existing.values()}
    varies = set()
    if len(currents) > 1:
        varies.add('current')
    if len(totals) > 1:
        varies.add('total')

    seed_cur = next(iter(currents)) if len(currents) == 1 else ''
    seed_tot = next(iter(totals)) if len(totals) == 1 else ''
    seed = f"{seed_cur}/{seed_tot}" if seed_tot else seed_cur
    note = {frozenset(): 'all files agree',
            frozenset({'current'}): 'the numbers differ, the total is shared',
            frozenset({'total'}): 'the totals differ, the number is shared',
            frozenset({'current', 'total'}): 'numbers and totals both differ',
            }[frozenset(varies)]
    res = prompt.fraction_edit(f"{label} across {ui_utils.plural(len(existing), 'file')}: {note}:",
                               tag=base_id, value=seed, varies=varies)
    if res is None or res is prompt.MODE_TOGGLE:
        return
    new_cur, new_tot = res.get('current'), res.get('total')

    plan: dict = {}
    for p, (cur, tot) in existing.items():
        # None means "this half varied and was left alone": keep the file's own.
        c = cur if new_cur is None else new_cur.strip()
        t = tot if new_tot is None else new_tot.strip()
        if not c:
            continue                       # nothing to write without an index
        plan[p] = f"{c}/{t}" if t else c

    ordered = [p for p in mp3s if p in plan]
    pos = {p: i + 1 for i, p in enumerate(ordered)}
    choices, n_changed = [], 0
    for p in ordered:
        cur, tot = existing[p]
        old = f"{cur}/{tot}" if tot else (cur or '-')
        changed = old != plan[p]
        n_changed += changed
        choices.append(prompt.Choice(
            title=os.path.basename(p), value=p, checked=changed,
            cells=[str(pos[p]), os.path.basename(p),
                   f"{old} → {plan[p]}" if changed else 'no change']))
    if not choices:
        ui_utils.show_status("Nothing to write.")
        return

    def _frac_header():
        """Live counts for the fraction preview."""
        nk = sum(1 for ch in choices if ch.checked)
        bits = [f"{label}", ui_utils.plural(len(choices), "file"), f"{n_changed} changing", f"{nk} ticked"]
        if skipped_fmt:
            bits.append(f"{skipped_fmt} non-MP3 skipped")
        return header(' · '.join(bits))()

    sel = prompt.select("Preview (↵ applies):", choices=choices,
                        columns=_RENUMBER_COLUMNS, header=_frac_header, multi=True)
    if sel is None:
        return
    apply_set = set(sel)
    if not apply_set:
        ui_utils.show_status("No files selected.")
        return

    count = errors = 0
    for p in ordered:
        if p not in apply_set:
            continue
        try:
            audio = load_id3(p)
            frame = create_frame(tag_id, plan[p])
            if frame is None:
                continue
            audio.delall(tag_id)
            audio.add(frame)
            save_id3(audio, p)
            count += 1
            with quietly():
                refresh_library_entry(library, p)
        except Exception:
            errors += 1

    msg = f"Set {label} on {ui_utils.plural(count, 'file')}."
    if skipped_fmt:
        msg += f" {skipped_fmt} non-MP3 skipped."
    if errors:
        msg += f" {ui_utils.plural(errors, 'error')}."
    ui_utils.show_status(msg)


# Preview columns for the pattern assignment: position · file · assigned value.
# Position is just a list index, so it drops first; file and the assigned value
# (the pending change) are kept.
# Per-range schedule table. START holds a whole "YYYY-MM-DD HH:MM:SS" (19 cols)
# at any terminal width; FROM/TO/EVERY/STEP are short and shrink around it.
_SCHEDULE_COL_MINS = (4, 4, 19, 5, 5)


# The numeric columns sit at their minimums and START gets just enough for a
# whole stamp; whatever the terminal has left over falls into the last column as
# trailing space, where it reads as a margin rather than a gap mid-row.
_SCHEDULE_COL_RATIOS = (4, 4, 21, 5, 46)


_PATTERN_COLUMNS = [
    prompt.Column(style='dynamic-dim', align='right', max_width=4, priority=1),
    prompt.Column(style='primary', flex=True),
    prompt.Column(style='normal', max_frac=0.42),
]


def assign_by_pattern(paths: list, library: list, header) -> None:
    """Assign one tag across an ordered selection by ranges, an every-N grouping,
    or a date schedule. MP3/ID3 only."""
    by_path = {s['path']: s for s in library}
    # Overlay the on-disk disc/track numbers over the cached ones: the ranges and
    # the per-disc seeding are position-based, so ordering from a stale cache
    # would point every range at the wrong tracks.
    merged = []
    for p in paths:
        s = dict(by_path.get(p, {'path': p}))
        s['path'] = p
        pairs = tw.read_number_pairs(p)
        if pairs['disc']:
            s['disc'] = pairs['disc']
        if pairs['track']:
            s['track'] = pairs['track']
        merged.append(s)
    ordered = bp.order_tracks(merged)
    if not ordered:
        ui_utils.show_status("No tracks.")
        return
    n = len(ordered)

    # Every answer is kept here, so a screen reopened by walking back holds what
    # it was left holding, typed rows and schedules included.
    state: dict = {'tag': '', 'mode': None, 'rows': [], 'sched': None, 'gs': '',
                   'tmpl': '', 'mode2': "Fill blanks only", 'apply_set': None}

    def _ask_tag() -> bool:
        """Which frame to assign. A name that isn't a frame is asked again here
        rather than reported as a back, which on the first screen would end the
        operation over a typo."""
        while True:
            raw = prompt.text("Tag to assign (e.g. TSST, TIT1, TDRC):",
                              default=state['tag'])
            if not raw:
                return False
            tag_id = raw.strip().upper()
            if get_tag_info(tag_id):
                state['tag'] = tag_id
                return True
            ui_utils.show_status(f"Unknown tag: {tag_id}")
            state['tag'] = tag_id            # so the typo is there to correct

    def _modes() -> list:
        """The assignment modes this tag supports (schedules need a date frame)."""
        info = get_tag_info(state['tag'])
        modes = ["Ranges (from-to → value)", "Every N tracks → value"]
        if info and info.format_spec == 'ISO8601':
            modes += ["Date schedule", "Schedule per range (own start + interval)"]
        return modes

    def _ask_mode() -> bool:
        """Ranges, every N, or a date schedule."""
        modes = _modes()
        idx = modes.index(state['mode']) if state['mode'] in modes else 0
        picked = prompt.select(
            "Assignment mode:", choices=modes, index=idx,
            header=header(f"{state['tag']} · {n} tracks in disc/track order"))
        if not picked:
            return False
        state['mode'] = picked
        return True

    def _int(s, what):
        """Parse s as an int, or show a status error naming `what` and return None."""
        try:
            return int(str(s).strip())
        except (TypeError, ValueError):
            ui_utils.show_status(f"{what} must be a number.")
            return None

    def _ask_spec() -> bool:
        """Ask whatever the chosen mode needs, and build the assignments.

        One step rather than one per question: these sub-flows loop and branch
        (a time per range, a group size only for "per group"), and threading a
        walk through them would tangle more than it helps. A back inside returns
        to the mode question, with typed rows kept for the next attempt.
        """
        tag_id = state['tag']
        mode = state['mode']
        assignments: dict = {}
        if mode.startswith("Ranges"):
            rows = prompt.list_edit(f"Ranges for {tag_id} (positions 1-{n}; range no. as "
                                    f"{{n}} 3 / {{r}} III / {{en}} Three):",
                                    state['rows'], ("FROM", "TO", "VALUE"))
            if not rows:
                return False
            state['rows'] = rows              # kept, in case this is walked back to
            ranges = []
            for r in rows:
                cells = list(r) if isinstance(r, (list, tuple)) else [r]
                if len(cells) < 3:
                    continue
                lo, hi = _int(cells[0], "FROM"), _int(cells[1], "TO")
                if lo is None or hi is None:
                    return False
                ranges.append((lo, hi, str(cells[2]).strip()))
            assignments = bp.assign_ranges(ordered, ranges)
        elif mode.startswith("Every"):
            gs = _int(prompt.text("Group size (N tracks per group):",
                                  default=state['gs']), "Group size")
            if gs is None:
                return False
            state['gs'] = str(gs)
            tmpl = prompt.text("Value: group number as {n} 3, {r} III or {en} Three "
                               "(e.g. Series {n}, Act {r}, Series {en}):",
                               default=state['tmpl'])
            if tmpl is None:
                return False
            state['tmpl'] = tmpl
            assignments = bp.assign_periodic(ordered, gs, tmpl)
        elif mode.startswith("Schedule per range"):
            # Seeded with one row per disc: the common case is "each disc/series has
            # its own start date and cadence", so the positions are filled in from
            # the real disc boundaries and only START/EVERY need typing.
            runs = bp.disc_ranges(ordered)
            seeded = [[str(lo), str(hi), '', '7', 'track'] for lo, hi, _ in runs]

            labels = [lab for _, _, lab in runs]
            shown = ', '.join(labels[:6]) + ('…' if len(labels) > 6 else '')

            def _sched_hints(col: int, row: list) -> list:
                """Barrel-pickable values for the STEP column."""
                return ['track', 'disc'] if col == 4 else []

            rows = prompt.list_edit(
                f"Per-range {tag_id} schedule, a row per disc ({shown}); "
                f"type digits into START, EVERY = days, STEP = track/disc:",
                state['sched'] or seeded, ("FROM", "TO", "START", "EVERY", "STEP"),
                col_hints=_sched_hints,
                # START edits as a split date/time field and is given the width to
                # show a whole stamp; the short numeric columns give way to it.
                col_types={2: 'timestamp'},
                col_mins=_SCHEDULE_COL_MINS,
                col_ratios=_SCHEDULE_COL_RATIOS)
            if not rows:
                return False
            state['sched'] = rows             # kept, in case this is walked back to

            # Every row is checked before anything is written, and a bad one is named
            # rather than quietly dropping out of the result.
            specs, row_errors = bp.validate_schedule_rows(rows, n)
            if row_errors:
                ui_utils.show_status("  ·  ".join(row_errors[:3])
                                     + (f"  (+{len(row_errors) - 3} more)"
                                        if len(row_errors) > 3 else ""), duration=tune.STATUS_WARNING_S)
                if not specs:
                    return False
                if not prompt.confirm(f"{ui_utils.plural(len(row_errors), 'row')} unusable: "
                                      f"apply the other {len(specs)}?"):
                    return False
            if not specs:
                ui_utils.show_status("No usable schedule rows.")
                return False

            # A time typed into START is kept per range. Only when no row carried one
            # is a time worth asking about, and "No time" sits first, so Enter
            # accepts the plain dates most archives want.
            if not any(spec[3] for spec in specs):
                tchoices = ["No time", "Same time for all"]
                if 1 < len(specs) <= 12:
                    tchoices.append("Per range")
                tsel = prompt.select("Time of day:", choices=tchoices, header=header())
                if not tsel:
                    return False
                if tsel == "Same time for all":
                    tval = bp.norm_time(prompt.text("Time (HH:MM, 24-hour):"))
                    if not tval:
                        ui_utils.show_status("Not a valid 24-hour time.")
                        return False
                    specs = [(lo, hi, st, tval, ev, sp) for lo, hi, st, _, ev, sp in specs]
                elif tsel == "Per range":
                    filled = []
                    for lo, hi, st, _, ev, sp in specs:
                        raw = prompt.text(f"Time for positions {lo}-{hi} "
                                          f"(HH:MM, blank = none):")
                        if raw is None:
                            return False
                        filled.append((lo, hi, st, bp.norm_time(raw), ev, sp))
                    specs = filled

            assignments = bp.assign_range_schedules(ordered, specs)
        else:                                            # Date schedule (ISO8601 tags)
            start = prompt.calendar_select("Start date:")
            if not start:
                return False
            iv = _int(prompt.text("Interval in days (7 = weekly):", default="7"), "Interval")
            if iv is None:
                return False
            gsel = prompt.select("Step the date:",
                                 choices=["Per track", "Per disc", "Per group of N"])
            if not gsel:
                return False
            gran, gsize = 'track', 1
            if gsel.startswith("Per disc"):
                gran = 'disc'
            elif gsel.startswith("Per group"):
                gsize = _int(prompt.text("Group size (N):"), "Group size")
                if gsize is None:
                    return False
                gran = 'group'

            # Optional time of day → full ISO timestamps. Per-group times (e.g. each
            # series at a different time) are offered when the groups are few.
            times = None
            tmode_choices = ["No time", "Same time for all"]
            groups = bp.date_groups(ordered, gran, gsize)
            if gran != 'track' and 1 < len(groups) <= 12:
                tmode_choices.append("Per group")
            tmode = prompt.select("Time of day:", choices=tmode_choices, header=header())
            if not tmode:
                return False
            if tmode == "Same time for all":
                times = prompt.text("Time (HH:MM, 24-hour):")
                if not times:
                    return False
            elif tmode == "Per group":
                times = {}
                for g in groups:
                    t = prompt.text(f"Time for group {g} (HH:MM, blank = none):")
                    if t:
                        times[g] = t
            assignments = bp.assign_dates(ordered, start, iv, gran, gsize, times=times)

        assignments = {p: v for p, v in assignments.items() if v}
        if not assignments:
            ui_utils.show_status("Nothing to assign: check the ranges/positions.")
            return False
        state['assignments'] = assignments
        return True

    _MODE2 = ["Fill blanks only", "Overwrite existing"]

    def _ask_mode2() -> bool:
        """Fill blanks, or overwrite what is already there."""
        picked = prompt.select("When the tag already has a value:", choices=_MODE2,
                               index=_MODE2.index(state['mode2']), header=header())
        if not picked:
            return False
        state['mode2'] = picked
        return True

    def _ask_preview() -> bool:
        """Show the value each track would get, and take the selection."""
        tag_id, assignments = state['tag'], state['assignments']
        pos = {s['path']: i + 1 for i, s in enumerate(ordered)}
        targets = [s for s in ordered if s['path'] in assignments]
        n_mp4 = sum(1 for s in targets if not s['path'].lower().endswith('.mp3'))
        keep = state['apply_set']
        choices = [
            prompt.Choice(title=os.path.basename(s['path']), value=s['path'],
                          checked=keep is None or s['path'] in keep,
                          cells=[str(pos[s['path']]), os.path.basename(s['path']),
                                 assignments[s['path']]])
            for s in targets if s['path'].lower().endswith('.mp3')
        ]
        if not choices:
            ui_utils.show_status("No MP3s to assign (this operation is MP3-only).")
            return False
        sub = f"{tag_id} · {ui_utils.plural(len(choices), 'file')}" + (
            f" · {n_mp4} non-MP3 skipped" if n_mp4 else "")
        sel = prompt.select("Preview (↵ applies):", choices=choices,
                            columns=_PATTERN_COLUMNS, header=header(sub), multi=True)
        if sel is None:
            return False
        state['targets'] = targets
        state['apply_set'] = set(sel)
        return True

    if not _walk([_ask_tag, _ask_mode, _ask_spec, _ask_mode2, _ask_preview]):
        return

    tag_id = state['tag']
    assignments = state['assignments']
    targets = state['targets']
    apply_set = state['apply_set']
    overwrite = (state['mode2'] == "Overwrite existing")
    if not apply_set:
        ui_utils.show_status("No files selected.")
        return

    # MP3 only: create_frame/save_id3 write ID3.
    writable = [s for s in targets if tw.format_kind(s['path']) == 'mp3']
    n_other = len(targets) - len(writable)
    applied = bo.apply_frame_writes(
        {s['path']: [(tag_id, assignments[s['path']])]
         for s in writable if s['path'] in apply_set},
        library, overwrite=overwrite)
    applied.skipped = n_other
    ui_utils.show_status(bo.summarise(applied, f"Assigned {tag_id} to",
                                      skipped_note="non-MP3 skipped",
                                      kept_note="kept an existing value"))
