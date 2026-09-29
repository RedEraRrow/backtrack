"""Bulk album art: setting the cover from nearby images, and the picture type."""
from __future__ import annotations
import os
from src.utils import prompt
from src.id3.id3_tag_handler import _prompt_for_picture_type, pick_nearby_cover, CLEAR_COVER
from src.id3 import tag_writer as tw
from src.id3 import bulk_ops as bo
from src.id3 import file_namer as fnm
from src.id3 import cover_matcher as cm
from src.utils import ui_utils
from collections import Counter
from src.id3.bulk_common import _RENAME_PICK_COLUMNS, _RENUMBER_COLUMNS, _SKIP, _show_tokens, _walk


_picture_type_name = bo.picture_type_name


def set_picture_type_op(paths: list, library: list, header) -> None:
    """Set the picture type on art that's already embedded.

    Rippers routinely tag a front cover as "Other" (type 0), which anything
    looking specifically for a front cover then misses. This retypes in bulk
    without touching the image. MP3 only — MP4's `covr` atom has no type field.
    """
    art, skipped = bo.read_picture_types(paths)
    if not art:
        ui_utils.show_status("No MP3s with embedded art in this selection.")
        return

    counts = Counter(t for a in art for t in a['types'])
    seen = ' · '.join(f"{_picture_type_name(t)} ×{n}" for t, n in counts.most_common())

    pic_type = _prompt_for_picture_type(
        initial=3, header=lambda: header(f"{len(art)} file(s) with art · {seen}")())
    if pic_type is None:
        return

    plan = bo.plan_set_picture_type(art, skipped, pic_type)
    if plan.message:
        ui_utils.show_status(plan.message)
        return

    choices = [
        prompt.Choice(title=name, value=c.path, checked=c.changed,
                      cells=[str(pos), name, why or "already correct"])
        for (pos, name, why), c in zip(bo.position_rows(plan), plan.changes)]
    n_changed = len(plan.changed)

    def _type_header():
        """Live counts for the preview."""
        nk = sum(1 for ch in choices if ch.checked)
        bits = [f"{len(art)} file(s) with art", f"{n_changed} to retype", f"{nk} ticked"]
        if skipped:
            bits.append(f"{skipped} without art or not MP3")
        return header(' · '.join(bits))()

    sel = prompt.select("Preview — ↵ applies:", choices=choices,
                        columns=_RENUMBER_COLUMNS, header=_type_header, multi=True)
    if sel is None:
        return
    apply_set = set(sel)
    if not apply_set:
        ui_utils.show_status("No tracks selected.")
        return

    applied = bo.apply_changes(
        plan, library, lambda c: tw.retype_cover(c.path, pic_type), selected=apply_set)
    ui_utils.show_status(bo.summarise(applied, f"Set {_picture_type_name(pic_type)} on",
                                      skipped_note="without art or not MP3"))


# Per-file album art: track · matched cover image · confidence. Confidence is
# advisory, so it drops first; track and the matched image (the change) are kept.
_COVER_PREVIEW_COLUMNS = [
    prompt.Column(style='primary', max_frac=0.42),
    prompt.Column(style='normal', flex=True),
    prompt.Column(style='dynamic-dim', align='right', pin=True, priority=1),
]


# Image-name patterns offered for the "%token%" match mode.
_COVER_PATTERN_PRESETS = [
    ('%track%', '01'),
    ('%track% %title%', '01 Song'),
    ('%title%', 'Song'),
    ('%tracknopad%', '1'),
    ('%album% %track%', 'Album 01'),
]


def _img_label(image_path: str, track_dir: str) -> str:
    """Image name, prefixed with its subfolder when it isn't beside the track."""
    d = os.path.dirname(os.path.abspath(image_path))
    if os.path.abspath(track_dir) != d:
        return f"{os.path.basename(d)}/{os.path.basename(image_path)}"
    return os.path.basename(image_path)


def _cover_pattern_prompt(header) -> str | None:
    """Pick / type a %token% pattern for matching image file names."""
    choices: list = []
    for pat, ex in _COVER_PATTERN_PRESETS:
        choices.append(prompt.Choice(title=pat, value=pat, cells=[pat, f"e.g. {ex}"]))
    choices.append(prompt.separator())
    choices.append(prompt.Choice(title="Custom pattern…", value="__custom__",
                                 cells=["Custom pattern…", "type your own %token%"]))
    choices.append(prompt.Choice(title="Show all tokens", value="__tokens__",
                                 cells=["Show all tokens", f"{len(fnm.TOKENS)} available"]))
    while True:
        sel = prompt.select("Image-name pattern (matched against the image files):",
                            choices=choices, columns=_RENAME_PICK_COLUMNS,
                            header=header("cover-name pattern"))
        if not sel:
            return None
        if sel == "__tokens__":
            _show_tokens(header)
            continue
        if sel == "__custom__":
            raw = prompt.text("Pattern (e.g. %track% %title% — 'Show all tokens' lists them):")
            if not raw:
                continue
            unk = fnm.unknown_tokens(raw)
            if unk:
                ui_utils.show_status(f"Unknown token(s) render blank: {', '.join(unk)}")
            return raw
        return sel


def set_album_art_op(paths: list, library: list, header) -> None:
    """Embed a per-track cover image, pairing each track with the image file that
    belongs to it (a folder of ``01 - Song.jpg`` … or ``covers/1.png`` …).

    Four pairing modes (auto / file name / track order / %token% pattern); a
    preview where ``d`` opens a ranked per-file picker to choose or change the
    cover; fill-blanks by default with per-row checkboxes as the final word.
    MP3 (APIC) and MP4 (``covr``; JPEG/PNG only) both write."""
    writable = [p for p in paths if tw.is_writable(p)]
    skipped_fmt = len(paths) - len(writable)
    if not writable:
        ui_utils.show_status("No MP3/MP4 files here to set art on.")
        return

    tokens = {p: fnm.read_tokens(p) for p in writable}

    # Candidate images per directory (a box set's disc folders keep their own art).
    dir_images: dict[str, list] = {}
    for p in writable:
        d = os.path.dirname(os.path.abspath(p))
        if d not in dir_images:
            dir_images[d] = cm.find_images(d)
    if not any(dir_images.values()):
        ui_utils.show_status("No images found beside these tracks "
                             "(looked in the folder + artwork/covers/scans).")
        return
    n_images = len({img for imgs in dir_images.values() for img in imgs})

    # 1) Matching strategy. Each answer is kept in `state`, so a screen reopened
    # by walking back holds what it was left holding — including covers chosen by
    # hand in the preview, which survive the plan being rebuilt.
    _MODES = ["Auto-detect (recommended)",
              "One cover per disc / series / work",
              "Matching file name",
              "Track number / order",
              "Name pattern (%token%)"]
    _GROUPS = {"Auto (disc, else series, else work)": 'auto',
               "Disc number": 'disc',
               "Series / season (SxxExx)": 'season',
               "Work / grouping": 'work'}
    _POLICIES = ["Fill blanks only", "Overwrite existing"]
    state: dict = {'mode': _MODES[0], 'group': list(_GROUPS)[0], 'pattern': None,
                   'policy': _POLICIES[0], 'manual': {}, 'apply_paths': None}

    def _ask_mode() -> bool:
        """How images pair with tracks."""
        picked = prompt.select("Match cover images to tracks by:", choices=_MODES,
                               index=_MODES.index(state['mode']),
                               header=header(f"{n_images} image(s) found"))
        if not picked:
            return False
        state['mode'] = picked
        return True

    def _ask_group():
        """What counts as a group — only when one cover covers a group."""
        if not state['mode'].startswith("One cover per"):
            return _SKIP
        picked = prompt.select("Group tracks by:", choices=list(_GROUPS),
                              index=list(_GROUPS).index(state['group']), header=header())
        if not picked:
            return False
        state['group'] = picked
        return True

    def _ask_pattern():
        """The image-name pattern — only for pattern matching."""
        if state['mode'] != "Name pattern (%token%)":
            return _SKIP
        picked = _cover_pattern_prompt(header)
        if picked is None:
            return False
        state['pattern'] = picked
        return True

    def _ask_policy() -> bool:
        """Whether tracks that already have art start ticked."""
        picked = prompt.select("For tracks that already have art:", choices=_POLICIES,
                               index=_POLICIES.index(state['policy']), header=header())
        if not picked:
            return False
        state['policy'] = picked
        return True

    def _ask_preview() -> bool:
        """Pair everything up, then show it — `d` re-chooses one track's cover."""
        mode, pattern = state['mode'], state['pattern']
        grouped = mode.startswith("One cover per")
        group_by = _GROUPS[state['group']]
        overwrite_default = (state['policy'] == "Overwrite existing")

        # Build the plan per directory, so images only pair with tracks beside them.
        by_dir: dict[str, list] = {}
        for p in writable:
            by_dir.setdefault(os.path.dirname(os.path.abspath(p)), []).append(p)
        plan: dict = {}
        for d, group in by_dir.items():
            imgs = dir_images[d]
            if not imgs:
                plan.update({p: None for p in group})
            elif grouped:
                plan.update(cm.plan_grouped(group, imgs, group_by, tokens))
            elif mode.startswith("Auto"):
                plan.update(cm.plan_best(group, imgs, tokens))
            elif mode == "Matching file name":
                plan.update(cm.plan_basename(group, imgs, tokens))
            elif mode == "Track number / order":
                plan.update(cm.plan_positional(group, imgs, tokens))
            else:
                plan.update(cm.plan_template(group, imgs, pattern or '', tokens))
        # Covers chosen by hand in a previous pass through this screen win over
        # whatever the matcher would pair now.
        plan.update(state['manual'])

        existing_art = {p: tw.has_cover(p) for p in writable}

        # Only tracks whose folder has images are actionable (the rest have nothing
        # to pair with, auto or by hand).
        shown = [p for p in writable if dir_images[os.path.dirname(os.path.abspath(p))]]

        # A cover shared by 2+ tracks is group art — badge every one of its rows with
        # the group (disc/season/work) so the column stays consistent, instead of one
        # row flipping to "high" just because its track number happens to match the
        # cover's number. Per-track (unique) covers keep the match-confidence label.
        shared = {img for img, n in Counter(v for v in plan.values() if v).items() if n >= 2}

        def _conf(p: str) -> str:
            """Confidence/group label for a track's planned cover, or '' if unmatched."""
            img = plan.get(p)
            if not img:
                return ''
            if grouped or img in shared:
                return cm.group_label(p, tokens[p], group_by)
            return cm.confidence(cm.score_match(p, tokens[p], img))

        def _cover_cell(p: str):
            """Cell text for a track's planned cover: label, plus a "has art" badge if replacing."""
            img = plan.get(p)
            if not img:
                return "— none (d to choose) —"
            label = _img_label(img, os.path.dirname(os.path.abspath(p)))
            if existing_art[p]:
                return [(label, 'normal'), ('  · has art', 'static-dim')]
            return label

        def _checked(p: str) -> bool:
            """Default tick state: matched, and either overwriting or no existing art."""
            return bool(plan.get(p)) and (overwrite_default or not existing_art[p])

        choice_by_path: dict = {}
        preview_choices: list = []
        for p in shown:
            ch = prompt.Choice(title=os.path.basename(p), value=p, checked=_checked(p),
                               cells=[os.path.basename(p), _cover_cell(p), _conf(p)])
            choice_by_path[p] = ch
            preview_choices.append(ch)

        def _set_cover(p: str, img) -> None:
            """Point track ``p`` at ``img`` (or None) and refresh its preview row."""
            ch = choice_by_path[p]
            state['manual'][p] = img        # remembered if this screen is revisited
            if img is None:
                plan[p] = None
                ch.cells = [os.path.basename(p), "— none (d to choose) —", '']
                ch.checked = False
            else:
                plan[p] = img
                ch.cells = [os.path.basename(p), _cover_cell(p), _conf(p)]
                ch.checked = True               # an explicit pick means write it

        def _copy_scope(picked: str, path: str) -> list | None:
            """Ask how far a hand-picked cover should carry, and return the tracks it
            applies to (None if the user backed out).

            A cover chosen for an unmatched track is usually right for its
            neighbours too, but not always for the whole selection — a box set
            changes art per disc.  So the choice is: every track, everything from
            here down, a counted run from here, or this track alone.
            """
            below = shown[shown.index(path):]
            choices = [prompt.Choice(title=f"All {len(shown)} tracks", value="all")]
            if len(below) > 1 and len(below) != len(shown):
                choices.append(prompt.Choice(
                    title=f"This track and the {len(below) - 1} below it", value="down"))
            if len(below) > 1:
                choices.append(prompt.Choice(title="This track and the next N…", value="count"))
            choices.append(prompt.Choice(title="Just this track", value="one"))

            scope = prompt.select(f"Apply {os.path.basename(picked)} to:", choices=choices,
                                  header=header("apply cover"))
            if scope is None:
                return None                     # backed out — leave the row unchanged
            if scope == "all":
                return shown
            if scope == "down":
                return below
            if scope == "count":
                raw = prompt.text(f"How many tracks from here (1-{len(below)}):",
                                  default=str(len(below)))
                if raw is None:
                    return None
                try:
                    n = int(raw.strip())
                except ValueError:
                    ui_utils.show_status("Not a number — applied to this track only.")
                    return [path]
                n = max(1, min(n, len(below)))
                return below[:n]
            return [path]

        def _reassign(path: str) -> None:
            """Let the user pick/clear the cover for one track, then choose how far
            that cover carries down the selection."""
            d = os.path.dirname(os.path.abspath(path))
            picked = pick_nearby_cover(path, tokens=tokens[path], images=dir_images[d],
                                       current=plan.get(path), allow_none=True, header=header)
            if picked is None:
                return                          # backed out, no change
            if picked is CLEAR_COVER or not isinstance(picked, str):
                _set_cover(path, None)
                return
            targets = [path]
            if len(shown) > 1:
                targets = _copy_scope(picked, path)
                if targets is None:
                    return
            for tp in targets:
                _set_cover(tp, picked)

        n_match = sum(1 for p in shown if plan.get(p))
        n_nodir = len(writable) - len(shown)

        # Rebuilt on every render (select calls the header each frame), so the ticked
        # count tracks live as you Space/'a'/'d' through the list.
        def _preview_header():
            """Build the live status line (matched/ticked/skipped counts) for the cover preview."""
            nk = sum(1 for ch in preview_choices if ch.checked)
            bits = [ui_utils.plural(len(shown), "track"), f"{n_match} matched"]
            if n_nodir:
                bits.append(f"{n_nodir} without images")
            if skipped_fmt:
                bits.append(f"{skipped_fmt} unsupported skipped")
            if nk:
                bits.append(f"{nk} ticked")
            elif n_match:
                bits.append("0 ticked — existing art; Overwrite or Space/a to tick")
            return header(" · ".join(bits))()

        sel = prompt.select(
            "Preview — ↵ applies:",
            choices=preview_choices, columns=_COVER_PREVIEW_COLUMNS,
            header=_preview_header, multi=True,
            extra_hints={'d': 'choose cover'}, on_inspect=_reassign)
        if sel is None:
            return False
        state.update(plan=plan, shown=shown, apply_paths=set(sel))
        return True

    if not _walk([_ask_mode, _ask_group, _ask_pattern, _ask_policy, _ask_preview]):
        return
    plan, shown = state['plan'], state['shown']
    apply_paths = state['apply_paths']
    if not apply_paths:
        ui_utils.show_status("No tracks selected.")
        return

    # 4) Apply. "Fill blanks only" is a hard flag here, as it is for derive /
    #    sort / assign — not merely the state the checkboxes opened in. Ticking
    #    a row by hand (or with 'a') under that policy no longer overwrites the
    #    art it was chosen to preserve; those tracks are counted and reported
    #    rather than silently passed over.
    overwrite = state['policy'] == "Overwrite existing"
    applied = bo.apply_covers({p: plan.get(p) for p in shown}, library,
                              overwrite=overwrite, selected=apply_paths)
    applied.skipped = skipped_fmt
    ui_utils.show_status(bo.summarise(
        applied, "Set album art on", noun="track",
        kept_note="kept existing art (fill blanks only)",
        unsupported_note="MP4 skipped (cover needs JPEG/PNG)"))
