"""The command tree and its handlers.

Every handler is thin by design: resolve the arguments, call the function the
menus already call, hand the result to `utils.output`. If a handler starts
deciding *what an operation does*, that decision belongs in a shared module
instead — see `id3.bulk_ops` for the pattern.

Handlers return an exit code (see `utils.output`); returning None means OK.
"""
from __future__ import annotations

import os

from src.cli import Arg, Cmd, Ctx, Flag
from src.utils import output as out
from src.utils import prompt_core as pc


# --- shared column specs ----------------------------------------------------
# Deliberately the same shapes the browse and search screens use, so a table
# read in the terminal looks like the app it came from.

_TRACK_COLS = [
    pc.Column(style='primary', flex=True, max_frac=0.40),          # title
    pc.Column(style='normal', flex=True, max_frac=0.30),           # artist
    pc.Column(style='dynamic-dim', flex=True, max_frac=0.30, priority=2),  # album
    pc.Column(style='dynamic-dim', align='right', pin=True, priority=1),   # duration
]

_GROUP_COLS = [
    pc.Column(style='primary', flex=True),                         # name
    pc.Column(style='dynamic-dim', align='right', pin=True),       # track count
]

_KV_COLS = [
    pc.Column(style='primary', min_width=16),                      # key
    pc.Column(style='normal', flex=True),                          # value
]


def _dur(seconds) -> str:
    """A track duration as m:ss, or blank when the cache has no length."""
    from src.utils import ui_utils
    try:
        value = float(seconds or 0)
    except (TypeError, ValueError):
        return ''
    return ui_utils.format_time(value) if value > 0 else ''


def _track_row(song: dict) -> dict:
    """One track as the flat object both the table and `--json` are built from."""
    return {
        'path': song.get('path', ''),
        'title': song.get('title', ''),
        'artist': song.get('artist', ''),
        'album_artist': song.get('album_artist', ''),
        'album': song.get('album', ''),
        'genre': song.get('genre', ''),
        'year': song.get('date', ''),
        'track': song.get('track', ''),
        'disc': song.get('disc', ''),
        'duration': song.get('duration', 0),
    }


def _track_cells(row: dict) -> list:
    """The four columns a track table shows."""
    return [row['title'], row['artist'], row['album'], _dur(row['duration'])]


def _filtered(ctx: Ctx) -> list:
    """The library narrowed by the --artist / --album / --genre filters.

    Matching is case-insensitive and substring, which is what a hand-typed
    filter wants; `search` is there when a fuzzy match is wanted instead.
    """
    from src.music_library import split_tag_values

    songs = ctx.library
    for field, value in (('artist', ctx.args.artist), ('album', ctx.args.album),
                         ('genre', ctx.args.genre)):
        if not value:
            continue
        needle = value.casefold()
        songs = [s for s in songs
                 if any(needle in str(v).casefold()
                        for v in split_tag_values(s.get(field, '')))
                 or needle in str(s.get(f'album_{field}', '')).casefold()]
    return songs


# --- library ----------------------------------------------------------------

def _library_scan(ctx: Ctx) -> int:
    """Rebuild the library cache from disk."""
    from src.config import music_dirs
    from src.music_library import build_library, save_library_cache

    roots = music_dirs(ctx.config)
    if not roots:
        return out.fail(out.USAGE, "No music directory configured.",
                        hint="pass --library DIR, or set one with "
                             "`backtrack library dirs --add DIR`")
    missing = [r for r in roots if not os.path.isdir(r)]
    if missing:
        return out.fail(out.NOT_FOUND, "Music directory does not exist.",
                        directories=', '.join(missing))
    if ctx.dry_run():
        out.event('plan', action='scan', detail=f"would scan {', '.join(roots)}")
        return out.OK

    library = build_library(
        roots, ignore_hidden=ctx.config.get('ignore_hidden_files', False))
    save_library_cache(library, _async=False)
    out.record('library', {'tracks': len(library), 'directories': roots},
               human=f"  Scanned {len(library)} tracks in {len(roots)} "
                     f"director{'y' if len(roots) == 1 else 'ies'}.")
    return out.OK


def _library_list(ctx: Ctx) -> int:
    """List the library grouped by artist, album or genre."""
    from src.music_library import get_grouped_data, get_group_sort_key

    category = ctx.args.by
    grouped = get_grouped_data(ctx.library, category)
    names = sorted(grouped, key=lambda n: get_group_sort_key(n, grouped[n], category))
    rows = [{'name': n, 'tracks': len(grouped[n]),
             'path': n}                       # the pipe key: a name, not a file
            for n in names]
    out.table(f'{category}s', rows, _GROUP_COLS,
              cells=lambda r: [r['name'], str(r['tracks'])])
    return out.OK


def _library_stat(ctx: Ctx) -> int:
    """Counts and totals for the whole library."""
    from src.config import music_dirs
    from src.music_library import CACHE_PATH, get_grouped_data
    from src.utils import ui_utils

    library = ctx.library
    total = sum(float(s.get('duration') or 0) for s in library)
    body = {
        'tracks': len(library),
        'artists': len(get_grouped_data(library, 'artist')),
        'albums': len(get_grouped_data(library, 'album')),
        'genres': len(get_grouped_data(library, 'genre')),
        'duration': ui_utils.format_time(total),
        'duration_seconds': round(total),
        'directories': music_dirs(ctx.config),
        'cache': str(CACHE_PATH),
    }
    # A record, not a list: `stat` has one subject, so it prints as aligned
    # key/value in both modes rather than becoming bare values on a pipe.
    out.record('stat', body)
    return out.OK


def _library_verify(ctx: Ctx) -> int:
    """Report cached tracks whose file is missing or unreadable."""
    bad = []
    for song in ctx.library:
        path = song.get('path', '')
        if not path or not os.path.exists(path):
            bad.append({**_track_row(song), 'problem': 'missing'})
        elif not os.access(path, os.R_OK):
            bad.append({**_track_row(song), 'problem': 'unreadable'})

    if not bad:
        out.record('verify', {'checked': len(ctx.library), 'problems': 0},
                   human=f"  {len(ctx.library)} tracks, all present.")
        return out.OK
    out.table('verify', bad, [
        pc.Column(style='primary', flex=True),
        pc.Column(style='accent'),
    ], cells=lambda r: [r['path'], r['problem']])
    out.note(f"{len(bad)} of {len(ctx.library)} tracks have a problem.")
    return out.FAIL


def _library_dirs(ctx: Ctx) -> int:
    """Show, add or remove the configured music directories."""
    from src.config import music_dirs, normalise_dir, save_config, set_music_dirs

    config = ctx.config
    current = music_dirs(config)
    changed = False

    for raw in (ctx.args.add or []):
        path = normalise_dir(raw)
        if not os.path.isdir(path):
            return out.fail(out.NOT_FOUND, "Not a directory.", directory=path)
        if path in current:
            return out.fail(out.EXISTS, "Already a music directory.", directory=path)
        current.append(path)
        changed = True
    for raw in (ctx.args.remove or []):
        path = normalise_dir(raw)
        if path not in current:
            return out.fail(out.NOT_FOUND, "Not a music directory.", directory=path)
        current.remove(path)
        changed = True

    if changed and not ctx.dry_run():
        set_music_dirs(config, current)
        save_config(config)
    elif changed:
        out.event('plan', action='set-directories', detail=', '.join(current))

    rows = [{'path': p, 'exists': os.path.isdir(p)} for p in current]
    out.table('directories', rows, [
        pc.Column(style='primary', flex=True),
        pc.Column(style='dynamic-dim', align='right', pin=True),
    ], cells=lambda r: [r['path'], '' if r['exists'] else 'missing'])
    return out.OK


# --- track ------------------------------------------------------------------

def _track_list(ctx: Ctx) -> int:
    """List tracks, optionally filtered."""
    from src.music_library import sort_library_logic

    songs = sort_library_logic(_filtered(ctx))
    if ctx.args.limit:
        songs = songs[:ctx.args.limit]
    rows = [_track_row(s) for s in songs]
    out.table('tracks', rows, _TRACK_COLS, cells=_track_cells)
    return out.OK


def _track_show(ctx: Ctx) -> int:
    """Everything the library knows about one or more tracks."""
    paths = ctx.targets()
    if not paths:
        return out.fail(out.USAGE, "No track given.",
                        hint="pass a path, or pipe one in")
    by_path = {s.get('path'): s for s in ctx.library}
    code = out.OK
    for path in paths:
        song = by_path.get(path)
        if song is None:
            if not os.path.exists(path):
                code = out.fail(out.NOT_FOUND, "No such track.", path=path)
                continue
            from src.music_library import get_metadata
            song = get_metadata(path)
        if out.json_mode():
            out.record('track', _track_row(song))
        else:
            row = _track_row(song)
            rows = [{'key': k, 'value': v} for k, v in row.items() if v != '']
            out.table('track', rows, _KV_COLS,
                      cells=lambda r: [str(r['key']), str(r['value'])],
                      pipe_key='value')
    return code


def _search(ctx: Ctx) -> int:
    """Fuzzy search, using the same ranker the live search screen uses."""
    from src import search as searcher
    from src.history import get_recent_paths

    fields = ([ctx.args.scope] if ctx.args.scope != 'all'
              else ['title', 'artist', 'album', 'genre', 'people'])
    results = searcher.search(ctx.library, ctx.args.query, fields=fields,
                              recent=get_recent_paths(), limit=ctx.args.limit)
    if not results:
        out.table('tracks', [], _TRACK_COLS)
        return out.NOT_FOUND
    rows = [_track_row(r.song) for r in results]
    out.table('tracks', rows, _TRACK_COLS, cells=_track_cells)
    return out.OK


# --- history ----------------------------------------------------------------

def _history_list(ctx: Ctx) -> int:
    """Recently played tracks, newest first."""
    from src.history import get_history

    by_path = {s.get('path'): s for s in ctx.library}
    rows = []
    for timestamp, duration, path in get_history(limit=ctx.args.limit):
        song = by_path.get(path, {})
        rows.append({'path': path, 'played': timestamp, 'listened': duration,
                     'title': song.get('title') or os.path.basename(path),
                     'artist': song.get('artist', '')})
    out.table('history', rows, [
        pc.Column(style='primary', flex=True, max_frac=0.4),
        pc.Column(style='normal', flex=True, max_frac=0.3),
        pc.Column(style='dynamic-dim', align='right', pin=True),
    ], cells=lambda r: [r['title'], r['artist'], r['played']])
    return out.OK


def _history_clear(ctx: Ctx) -> int:
    """Delete the listening history log."""
    from src.history import clear_history, get_history

    count = len(get_history(limit=10 ** 6))
    if not count:
        out.note("The history log is already empty.")
        return out.OK
    if ctx.dry_run():
        out.event('plan', action='clear-history',
                  detail=f"would delete {count} entries")
        return out.OK
    if not ctx.confirm(f"Delete all {count} history entries?"):
        out.note("Left alone.")
        return out.OK
    if not clear_history():
        return out.fail(out.FAIL, "Could not delete the history log.")
    out.record('history', {'cleared': count},
               human=f"  Cleared {count} history entries.")
    return out.OK


# --- config -----------------------------------------------------------------

def _config_list(ctx: Ctx) -> int:
    """Every config key and its current value."""
    rows = [{'key': k, 'value': v} for k, v in sorted(ctx.config.items())]
    if out.json_mode():
        out.record('config', ctx.config)
    else:
        out.table('config', rows, _KV_COLS,
                  cells=lambda r: [r['key'], _fmt_value(r['value'])],
                  pipe_key='key')
    return out.OK


_fmt_value = out.human_value


def _config_get(ctx: Ctx) -> int:
    """One config value."""
    from src.config import DEFAULT_CONFIG

    key = ctx.args.key
    if key not in DEFAULT_CONFIG and key not in ctx.config:
        return out.fail(out.NOT_FOUND, "No such config key.", key=key,
                        hint="`backtrack config list` shows them all")
    value = ctx.config.get(key)
    out.record('config', {'key': key, 'value': value},
               human=f"  {_fmt_value(value)}")
    return out.OK


def _config_set(ctx: Ctx) -> int:
    """Change one config value, parsed to the type the existing value has."""
    import json

    from src.config import DEFAULT_CONFIG, save_config

    key, raw = ctx.args.key, ctx.args.value
    if key not in DEFAULT_CONFIG:
        return out.fail(out.NOT_FOUND, "No such config key.", key=key,
                        hint="`backtrack config list` shows them all")
    current = DEFAULT_CONFIG[key]
    try:
        value = _coerce(raw, current)
    except ValueError as exc:
        return out.fail(out.USAGE, str(exc), key=key, given=raw)

    if ctx.dry_run():
        out.event('plan', action='config-set', key=key,
                  detail=f"{key} = {_fmt_value(value)}")
        return out.OK
    config = ctx.config
    config[key] = value
    save_config(config)
    out.record('config', {'key': key, 'value': value},
               human=f"  {key} = {_fmt_value(value)}")
    return out.OK


def _coerce(raw: str, like):
    """Parse `raw` into the type `like` already has, or explain why it can't."""
    import json

    if isinstance(like, bool):
        if raw.lower() in ('true', 'yes', 'on', '1'):
            return True
        if raw.lower() in ('false', 'no', 'off', '0'):
            return False
        raise ValueError("Expected true or false.")
    if isinstance(like, int):
        try:
            return int(raw)
        except ValueError:
            raise ValueError("Expected a whole number.")
    if isinstance(like, float):
        try:
            return float(raw)
        except ValueError:
            raise ValueError("Expected a number.")
    if isinstance(like, list):
        return [p.strip() for p in raw.split(',') if p.strip()]
    if isinstance(like, dict):
        try:
            parsed = json.loads(raw)
        except ValueError:
            raise ValueError("Expected JSON for this key.")
        if not isinstance(parsed, dict):
            raise ValueError("Expected a JSON object for this key.")
        return parsed
    return raw


# --- schema and completion --------------------------------------------------

def _schema(ctx: Ctx) -> int:
    """Print the command tree, flags and output shapes as JSON."""
    import json

    from src.cli import schema_tree
    print(json.dumps(schema_tree(TREE), indent=None if out.json_mode() else 2))
    return out.OK


def _completion(ctx: Ctx) -> int:
    """Print a shell completion script, generated from the command tree."""
    from src.cli import completion
    print(completion(ctx.args.shell, TREE), end='')
    return out.OK


_FILTERS = [
    Flag('--artist', 'Only tracks whose artist contains this', short='-a'),
    Flag('--album', 'Only tracks whose album contains this', short='-A'),
    Flag('--genre', 'Only tracks whose genre contains this', short='-g'),
    Flag('--limit', 'Show at most this many', short='-n', type=int),
]


# --- tags -------------------------------------------------------------------

def _targets_or_filter(ctx: Ctx) -> list[str]:
    """The files a tag or bulk command acts on.

    In order: explicit paths, then the --artist / --album / --genre filters
    against the library, then whatever is piped in. Stdin is consulted **last**
    and only when nothing else said what to act on — reading it whenever the
    positionals were empty hung `backtrack bulk stripdisc --album Rio` on any
    stdin that stays open, which is most of them.

    A command that narrows to nothing returns nothing, and its caller reports a
    usage error: "it did nothing" and "it matched nothing" are different things
    to a script.
    """
    explicit = ctx.targets(allow_stdin=False)
    if explicit:
        return explicit
    if any(getattr(ctx.args, f, None) for f in ('artist', 'album', 'genre')):
        return [s['path'] for s in _filtered(ctx) if s.get('path')]
    return out.read_stdin_paths()


def _tag_rows(path: str) -> list[dict]:
    """Every frame in one file, as the rows both modes are built from."""
    from mutagen.id3 import ID3

    from src.id3.id3_tag_handler import display_tag_id, summarize_tag_value
    from src.id3.tag_registry import get_preferred_tag_name

    audio = ID3(path)
    rows = []
    for key in sorted(audio.keys()):
        tag_id = display_tag_id(key)
        rows.append({
            'path': path,
            'tag': tag_id,
            'name': get_preferred_tag_name(tag_id.split(':')[0]) or '',
            'value': summarize_tag_value(key, audio[key], display=True),
        })
    return rows


_TAG_COLS = [
    pc.Column(style='primary', min_width=8),
    pc.Column(style='dynamic-dim', max_frac=0.25, priority=1),
    pc.Column(style='normal', flex=True),
]


def _tag_read(ctx: Ctx) -> int:
    """List the tags on one or more files."""
    from mutagen.id3 import ID3NoHeaderError  # type: ignore[reportPrivateImportUsage]

    paths = _targets_or_filter(ctx)
    if not paths:
        return out.fail(out.USAGE, "No track given.",
                        hint="pass a path, pipe one in, or use --artist/--album")
    code = out.OK
    for path in paths:
        if not os.path.exists(path):
            code = out.fail(out.NOT_FOUND, "No such track.", path=path)
            continue
        try:
            rows = _tag_rows(path)
        except ID3NoHeaderError:
            rows = []
        except Exception as exc:
            code = out.fail(out.FAIL, str(exc), path=path)
            continue
        if ctx.args.tag:
            wanted = {t.upper() for t in ctx.args.tag}
            rows = [r for r in rows if r['tag'].split(':')[0].upper() in wanted]
        if len(paths) > 1 and not out.json_mode():
            out.note(os.path.basename(path))
        out.table('tags', rows, _TAG_COLS,
                  cells=lambda r: [r['tag'], r['name'], r['value']],
                  pipe_key='value')
    return code


def _tag_apply(ctx: Ctx, operation: str, value=None) -> int:
    """The shared body of `tag write`, `tag rename` and `tag delete`."""
    from src.id3.id3_tag_handler import apply_bulk_operation_to_files

    paths = _targets_or_filter(ctx)
    if not paths:
        return out.fail(out.USAGE, "No track given.",
                        hint="pass a path, pipe one in, or use --artist/--album")
    missing = [p for p in paths if not os.path.exists(p)]
    if missing:
        return out.fail(out.NOT_FOUND, "No such track.", path=missing[0])

    tags = [t.upper() for t in ctx.args.tag]
    verb = {'set': 'Write', 'rename': 'Rename', 'delete': 'Delete'}[operation]
    if ctx.dry_run():
        for path in paths:
            out.event('plan', path=path, operation=operation, tags=tags,
                      detail=f"{verb.lower()} {', '.join(tags)} on {os.path.basename(path)}")
        out.note(f"{len(paths)} files would change.")
        return out.OK
    if operation == 'delete' and not ctx.confirm(
            f"Delete {', '.join(tags)} from {len(paths)} file(s)?"):
        out.note("Left alone.")
        return out.OK

    ok = fail = 0
    for path in paths:
        good, bad = apply_bulk_operation_to_files(
            [path], operation, tags, value, ctx.library)
        ok += good
        fail += bad
        out.event('written' if good and not bad else 'error', path=path,
                  detail=f"{', '.join(tags)} on {os.path.basename(path)}")
    from src.utils import ui_utils
    out.note(f"{verb}d {ui_utils.plural(ok, 'tag')} across "
             f"{ui_utils.plural(len(paths), 'file')}."
             + (f" {ui_utils.plural(fail, 'failure')}." if fail else ""))
    return out.FAIL if fail and not ok else out.OK


def _tag_write(ctx: Ctx) -> int:
    """Set one or more tags to a value."""
    return _tag_apply(ctx, 'set', ctx.args.value)


def _tag_rename(ctx: Ctx) -> int:
    """Change a frame's four-letter id, keeping its value."""
    return _tag_apply(ctx, 'rename', ctx.args.to.upper())


def _tag_delete(ctx: Ctx) -> int:
    """Remove one or more tags."""
    return _tag_apply(ctx, 'delete')


def _tag_copy(ctx: Ctx) -> int:
    """Copy tags from one file onto others."""
    import copy as _copy

    from mutagen.id3 import ID3

    from src.id3.id3_tag_handler import save_id3
    from src.music_library import refresh_library_entry

    source = os.path.abspath(os.path.expanduser(ctx.args.source))
    if not os.path.exists(source):
        return out.fail(out.NOT_FOUND, "No such source track.", path=source)
    targets = [p for p in _targets_or_filter(ctx) if p != source]
    if not targets:
        return out.fail(out.USAGE, "No target tracks given.",
                        hint="pass paths after the source, or pipe them in")

    src_tags = ID3(source)
    wanted = {t.upper() for t in ctx.args.tag} if ctx.args.tag else None
    frames = [src_tags[k] for k in src_tags
              if wanted is None or k.split(':')[0].upper() in wanted]
    if not frames:
        return out.fail(out.NOT_FOUND, "The source has none of those tags.",
                        path=source)

    if ctx.dry_run():
        for path in targets:
            out.event('plan', path=path,
                      detail=f"{len(frames)} frames from {os.path.basename(source)}")
        out.note(f"{len(targets)} files would change.")
        return out.OK

    written = errors = 0
    for path in targets:
        try:
            audio = ID3(path)
            for frame in frames:
                audio.setall(frame.FrameID, [_copy.deepcopy(frame)])
            save_id3(audio, path)
        except Exception as exc:
            errors += 1
            out.event('error', path=path, detail=str(exc))
            continue
        written += 1
        try:
            refresh_library_entry(ctx.library, path)
        except Exception:
            pass
        out.event('written', path=path, detail=os.path.basename(path))
    from src.utils import ui_utils
    out.note(f"Copied {ui_utils.plural(len(frames), 'tag')} onto "
             f"{ui_utils.plural(written, 'file')}."
             + (f" {ui_utils.plural(errors, 'error')}." if errors else ""))
    return out.FAIL if errors and not written else out.OK


# --- bulk operations --------------------------------------------------------

def _run_plan(ctx: Ctx, plan, writer, verb: str, **summary) -> int:
    """Preview, confirm, apply and report one `bulk_ops` plan.

    The whole of Phase 3 for every bulk command in one place: `--dry-run` emits
    the plan as the same `event` shape a real run emits, a terminal gets a
    confirmation before anything is written, and each file reports as it happens
    so `--json` streams rather than buffering.
    """
    from src.id3 import bulk_ops as bo
    from src.utils import ui_utils

    if plan.message:
        out.note(plan.message)
        return out.OK
    changed = plan.changed
    if not changed:
        out.note("Nothing to change.")
        return out.OK

    if ctx.dry_run():
        for change in changed:
            out.event('plan', path=change.path, detail=change.why,
                      fields=change.fields)
        out.note(f"{ui_utils.plural(len(changed), 'file')} would change.")
        return out.OK

    if not ctx.confirm(f"{verb} {ui_utils.plural(len(changed), 'file')}?",
                       default=True):
        out.note("Left alone.")
        return out.OK

    applied = bo.apply_changes(
        plan, ctx.library, writer,
        on_event=lambda kind, change, detail: out.event(
            kind, path=change.path, detail=detail))
    out.note(bo.summarise(applied, verb, **summary))
    return out.FAIL if applied.errors and not applied.written else out.OK


def _bulk_paths(ctx: Ctx) -> tuple[list, int]:
    """The files to operate on, or an empty list with a usage code."""
    paths = _targets_or_filter(ctx)
    if not paths:
        return [], out.fail(out.USAGE, "No tracks given.",
                            hint="pass paths, pipe them in, or use "
                                 "--artist/--album/--genre")
    return paths, out.OK


def _bulk_renumber(ctx: Ctx) -> int:
    """Renumber tracks continuously across the album or per disc."""
    from src.id3 import bulk_ops as bo
    from src.id3 import tag_writer as tw

    paths, code = _bulk_paths(ctx)
    if not paths:
        return code
    ordered, skipped = bo.read_numbering(paths)
    mode = 'per_disc' if ctx.args.mode == 'per-disc' else 'continuous'
    plan = bo.plan_renumber(ordered, skipped, mode)
    return _run_plan(ctx, plan,
                     lambda c: tw.write_fields(c.path, c.fields, {'track'},
                                               overwrite=True),
                     "Renumbered")


def _bulk_reflow(ctx: Ctx) -> int:
    """Re-flow disc numbering onto a dense 1…N and fix the totals."""
    from src.id3 import bulk_ops as bo
    from src.id3 import tag_writer as tw

    paths, code = _bulk_paths(ctx)
    if not paths:
        return code
    ordered, skipped = bo.read_numbering(paths)
    fields = {'disc'} | ({'track'} if ctx.args.track_totals else set())
    plan = bo.plan_reflow(ordered, skipped, renumber=not ctx.args.totals_only,
                          disc_totals=True, track_totals=ctx.args.track_totals)
    return _run_plan(ctx, plan,
                     lambda c: tw.write_fields(c.path, c.fields, fields,
                                               overwrite=True),
                     "Reflowed disc numbering on")


def _bulk_stripdisc(ctx: Ctx) -> int:
    """Remove the disc number from tracks that are disc 1 of 1."""
    from src.id3 import bulk_ops as bo
    from src.id3 import tag_writer as tw

    paths, code = _bulk_paths(ctx)
    if not paths:
        return code
    ordered, skipped = bo.read_numbering(paths)
    plan = bo.plan_strip_single_disc(ordered, skipped)
    return _run_plan(ctx, plan, lambda c: tw.clear_fields(c.path, {'disc'}),
                     "Removed the disc number from")


def _bulk_striplength(ctx: Ctx) -> int:
    """Remove stale TLEN and non-zero TDLY frames."""
    from src.id3 import bulk_ops as bo

    paths, code = _bulk_paths(ctx)
    if not paths:
        return code
    songs, skipped = bo.read_length_tags(paths)
    plan = bo.plan_strip_length_tags(songs, skipped)
    return _run_plan(ctx, plan, bo.strip_length_writer,
                     "Stripped stale length tags from")


def _bulk_pictype(ctx: Ctx) -> int:
    """Retype embedded art without touching the image."""
    from src.id3 import bulk_ops as bo
    from src.id3 import tag_writer as tw

    paths, code = _bulk_paths(ctx)
    if not paths:
        return code
    art, skipped = bo.read_picture_types(paths)
    pic_type = ctx.args.type
    plan = bo.plan_set_picture_type(art, skipped, pic_type)
    return _run_plan(ctx, plan, lambda c: tw.retype_cover(c.path, pic_type),
                     f"Set {bo.picture_type_name(pic_type)} on",
                     skipped_note="without art or not MP3")


def _bulk_derive(ctx: Ctx) -> int:
    """Fill tags from file and folder names."""
    from src.id3 import bulk_ops as bo

    paths, code = _bulk_paths(ctx)
    if not paths:
        return code
    fields = set(ctx.args.field or ['title'])
    if ctx.args.sort:
        fields.add('sort')
    plan, derived = bo.plan_derive(
        paths, fields, overwrite=ctx.args.overwrite,
        template=ctx.args.template, regex=ctx.args.regex)
    return _run_plan(ctx, plan,
                     bo.derive_writer(derived, fields, ctx.args.overwrite),
                     "Derived tags for", skipped_note="non-MP3/MP4 skipped")


def _bulk_sortorders(ctx: Ctx) -> int:
    """Generate sort-order frames from the tags already present.

    The menu asks how each name divides and applies the answer everywhere that
    person appears. With no human to ask, this takes the engine's top candidate
    for every value — the same one the menu offers first.
    """
    from mutagen.id3 import ID3

    from src.id3 import bulk_ops as bo
    from src.id3 import tag_writer as tw

    paths, code = _bulk_paths(ctx)
    if not paths:
        return code

    writable = [p for p in paths if tw.format_kind(p) == 'mp3']
    skipped = len(paths) - len(writable)
    per_path: dict = {}
    changes = []
    for path in writable:
        try:
            audio = ID3(path)
        except Exception:
            continue
        writes = []
        for base, frame in bo.sort_base():
            source = {'artist': 'TPE1', 'album_artist': 'TPE2',
                      'album': 'TALB'}[base]
            existing = audio.get(source)
            if existing is None or not getattr(existing, 'text', None):
                continue
            if not ctx.args.overwrite and audio.get(frame) is not None:
                continue
            value = bo._sort_value(frame, str(existing.text[0]))
            if value:
                writes.append((frame, value))
        if writes:
            per_path[path] = writes
            changes.append(bo.Change(
                path=path, fields=dict(writes),
                why=' · '.join(f"{f}={v}" for f, v in writes)))

    plan = bo.Plan(changes=changes, skipped=skipped)
    if not changes:
        plan.message = "No sort orders to write — every one is already set."
    # apply_frame_writes does its own loop, so the plan here drives the preview,
    # the dry run and the confirmation; the write goes through in one call.
    if plan.message or ctx.dry_run():
        return _run_plan(ctx, plan, lambda c: None, "Wrote sort orders for")
    from src.utils import ui_utils
    if not ctx.confirm(f"Write sort orders on "
                       f"{ui_utils.plural(len(changes), 'file')}?", default=True):
        out.note("Left alone.")
        return out.OK
    applied = bo.apply_frame_writes(
        per_path, ctx.library,
        on_event=lambda kind, change, detail: out.event(
            kind, path=change.path, detail=detail))
    applied.skipped = skipped
    out.note(bo.summarise(applied, "Wrote sort orders for",
                          skipped_note="non-MP3 skipped"))
    return out.FAIL if applied.errors and not applied.written else out.OK


def _bulk_rename(ctx: Ctx) -> int:
    """Rename files from their tags, collision-safely."""
    from src.id3 import bulk_ops as bo
    from src.id3 import file_namer as fnm
    from src.id3 import tag_writer as tw
    from src.utils import ui_utils

    paths, code = _bulk_paths(ctx)
    if not paths:
        return code
    pattern = ctx.args.pattern
    unknown = fnm.unknown_tokens(pattern)
    if unknown:
        return out.fail(out.USAGE, "Unknown token(s) in the pattern.",
                        tokens=', '.join(unknown), pattern=pattern)

    writable = [p for p in paths if tw.is_writable(p)]
    skipped = len(paths) - len(writable)
    # plan_renames returns every supported file, whether or not the name moves.
    todo = [(path, os.path.join(os.path.dirname(path), new))
            for (path, was, new) in fnm.plan_renames(writable, pattern)
            if was != new]
    if not todo:
        out.note("Every file is already named that way.")
        return out.OK

    if ctx.dry_run():
        for old, new in todo:
            out.event('plan', path=old, detail=os.path.basename(new))
        out.note(f"{ui_utils.plural(len(todo), 'file')} would be renamed.")
        return out.OK
    if not ctx.confirm(f"Rename {ui_utils.plural(len(todo), 'file')}?",
                       default=True):
        out.note("Left alone.")
        return out.OK

    applied = bo.rename_files(
        todo, ctx.library,
        on_event=lambda kind, change, detail: out.event(
            kind, path=change.path, detail=detail))
    applied.skipped = skipped
    out.note(bo.summarise(applied, "Renamed"))
    return out.FAIL if applied.errors and not applied.written else out.OK


def _bulk_art(ctx: Ctx) -> int:
    """Embed cover images found beside the tracks."""
    from src.id3 import bulk_ops as bo
    from src.id3 import cover_matcher as cm
    from src.id3 import tag_writer as tw
    from src.utils import ui_utils

    paths, code = _bulk_paths(ctx)
    if not paths:
        return code
    writable = [p for p in paths if tw.is_writable(p)]
    skipped = len(paths) - len(writable)

    strategy = ctx.args.strategy
    if strategy == 'grouped':
        def planner(tracks, images):
            """One cover per disc or per folder group."""
            return cm.plan_grouped(tracks, images, group_by=ctx.args.group_by)
    elif strategy == 'template':
        if not ctx.args.pattern:
            return out.fail(out.USAGE, "--strategy template needs --pattern.",
                            hint='e.g. --pattern "%album% %track%"')

        def planner(tracks, images):
            """Pair by a %token% pattern rendered from each track's tags."""
            return cm.plan_template(tracks, images, ctx.args.pattern)
    else:
        planner = {'auto': cm.plan_auto, 'basename': cm.plan_basename,
                   'positional': cm.plan_positional, 'best': cm.plan_best}[strategy]
    # Covers live beside their tracks, so each directory is planned against its
    # own images — the same per-directory grouping the menu's preview uses.
    by_dir: dict = {}
    for path in writable:
        by_dir.setdefault(os.path.dirname(os.path.abspath(path)), []).append(path)
    matched: dict = {}
    for directory, tracks in by_dir.items():
        images = cm.find_images(directory)
        if not images:
            continue
        for path, image in planner(tracks, images).items():
            if image:
                matched[path] = image
    if not matched:
        out.note("No cover images found beside these tracks.")
        return out.OK

    if ctx.dry_run():
        for path, image in matched.items():
            out.event('plan', path=path, detail=os.path.basename(image))
        out.note(f"{ui_utils.plural(len(matched), 'track')} would get art.")
        return out.OK
    if not ctx.confirm(f"Set album art on "
                       f"{ui_utils.plural(len(matched), 'track')}?", default=True):
        out.note("Left alone.")
        return out.OK

    applied = bo.apply_covers(
        matched, ctx.library, overwrite=ctx.args.overwrite,
        on_event=lambda kind, change, detail: out.event(
            kind, path=change.path, detail=detail))
    applied.skipped = skipped
    out.note(bo.summarise(applied, "Set album art on", noun="track",
                          kept_note="kept existing art (fill blanks only)",
                          unsupported_note="MP4 skipped (cover needs JPEG/PNG)"))
    return out.FAIL if applied.errors and not applied.written else out.OK


def _bulk_assign(ctx: Ctx) -> int:
    """Assign one tag across an ordered selection by range, every-N or schedule."""
    from src import bulk_pattern as bp
    from src.id3 import bulk_ops as bo
    from src.id3 import tag_writer as tw
    from src.utils import ui_utils

    paths, code = _bulk_paths(ctx)
    if not paths:
        return code
    tag_id = ctx.args.tag.upper()
    writable = [p for p in paths if tw.format_kind(p) == 'mp3']
    skipped = len(paths) - len(writable)
    ordered, _skipped = bo.read_numbering(writable)
    if not ordered:
        out.note("No MP3 tracks to assign to.")
        return out.OK

    modes = [bool(ctx.args.every), bool(ctx.args.range), bool(ctx.args.start)]
    if sum(modes) != 1:
        return out.fail(out.USAGE,
                        "Pick exactly one of --every, --range or --start.")

    if ctx.args.every:
        assignments = bp.assign_periodic(ordered, ctx.args.every, ctx.args.value)
    elif ctx.args.range:
        ranges = []
        for spec in ctx.args.range:
            try:
                span, _, value = spec.partition('=')
                first, _, last = span.partition('-')
                ranges.append((int(first), int(last or first), value))
            except ValueError:
                return out.fail(out.USAGE, "Bad --range.", given=spec,
                                expected="FIRST-LAST=VALUE, e.g. 1-6=Series 1")
        assignments = bp.assign_ranges(ordered, ranges)
    else:
        start, _time, why = bp.parse_start(ctx.args.start)
        if why or not start:
            return out.fail(out.USAGE, why or "Bad --start date.",
                            given=ctx.args.start)
        assignments = bp.assign_dates(ordered, start, ctx.args.interval or 7)

    per_path = {p: [(tag_id, v)] for p, v in assignments.items() if v}
    changes = [bo.Change(path=p, why=f"{tag_id}={writes[0][1]}",
                         fields={tag_id: writes[0][1]})
               for p, writes in per_path.items()]
    plan = bo.Plan(changes=changes, skipped=skipped)
    if not changes:
        plan.message = "Nothing to assign."

    if plan.message or ctx.dry_run():
        return _run_plan(ctx, plan, lambda c: None, f"Assigned {tag_id} to")
    if not ctx.confirm(f"Assign {tag_id} on "
                       f"{ui_utils.plural(len(changes), 'file')}?", default=True):
        out.note("Left alone.")
        return out.OK
    applied = bo.apply_frame_writes(
        per_path, ctx.library, overwrite=ctx.args.overwrite,
        on_event=lambda kind, change, detail: out.event(
            kind, path=change.path, detail=detail))
    applied.skipped = skipped
    out.note(bo.summarise(applied, f"Assigned {tag_id} to",
                          skipped_note="non-MP3 skipped",
                          kept_note="kept an existing value"))
    return out.FAIL if applied.errors and not applied.written else out.OK


# --- the tree ---------------------------------------------------------------

TREE = [
    Cmd('library', 'Scan, list and check the library', children=[
        Cmd('scan', 'Rebuild the library cache from disk', run=_library_scan,
            emits='library',
            example='backtrack library scan --library ~/Music'),
        Cmd('list', 'List the library grouped by artist, album or genre',
            run=_library_list, emits='artists|albums|genres',
            flags=[Flag('--by', 'What to group by', short='-b',
                        choices=('artist', 'album', 'genre'), default='artist')],
            example='backtrack library list --by album'),
        Cmd('stat', 'Counts and totals for the whole library', run=_library_stat,
            emits='stat', example='backtrack library stat --json'),
        Cmd('verify', 'Report cached tracks whose file is missing or unreadable',
            run=_library_verify, emits='verify',
            example='backtrack library verify'),
        Cmd('dirs', 'Show, add or remove the configured music directories',
            run=_library_dirs, emits='directories',
            flags=[Flag('--add', 'Add a music directory (repeatable)',
                        metavar='DIR', action='append'),
                   Flag('--remove', 'Remove a music directory (repeatable)',
                        metavar='DIR', action='append')],
            example='backtrack library dirs --add ~/Music/Podcasts'),
    ]),

    Cmd('track', 'List and inspect individual tracks', children=[
        Cmd('list', 'List tracks, optionally filtered', run=_track_list,
            emits='tracks', flags=list(_FILTERS),
            example='backtrack track list --artist "Duran Duran"'),
        Cmd('show', 'Everything the library knows about a track', run=_track_show,
            emits='track', args=[Arg('target', 'Track file (repeatable; '
                                               'read from stdin if omitted)',
                                     nargs='*')],
            example='backtrack track list -a Darude | backtrack track show'),
    ]),


    Cmd('tag', 'Read and change tags on individual files', children=[
        Cmd('read', 'List the tags on a file', run=_tag_read, emits='tags',
            args=[Arg('target', 'Track file (repeatable; read from stdin if '
                                'omitted)', nargs='*')],
            flags=[Flag('--tag', 'Only this frame id (repeatable)', short='-t',
                        metavar='ID', action='append')] + list(_FILTERS[:3]),
            example='backtrack tag read track.mp3 --tag TIT2'),
        Cmd('write', 'Set a tag to a value', run=_tag_write, emits='event',
            args=[Arg('target', 'Track file (repeatable)', nargs='*')],
            flags=[Flag('--tag', 'Frame id to set (repeatable)', short='-t',
                        metavar='ID', action='append'),
                   Flag('--value', 'Value to write', short='-v')]
                  + list(_FILTERS[:3]),
            example='backtrack tag write track.mp3 -t TCON -v "New Wave"'),
        Cmd('rename', "Change a frame's id, keeping its value", run=_tag_rename,
            emits='event',
            args=[Arg('target', 'Track file (repeatable)', nargs='*')],
            flags=[Flag('--tag', 'Frame id to rename (repeatable)', short='-t',
                        metavar='ID', action='append'),
                   Flag('--to', 'New frame id')] + list(_FILTERS[:3]),
            example='backtrack tag rename track.mp3 -t TIT1 --to TSST'),
        Cmd('delete', 'Remove a tag', run=_tag_delete, emits='event',
            args=[Arg('target', 'Track file (repeatable)', nargs='*')],
            flags=[Flag('--tag', 'Frame id to delete (repeatable)', short='-t',
                        metavar='ID', action='append')] + list(_FILTERS[:3]),
            example='backtrack tag delete *.mp3 -t TLEN --yes'),
        Cmd('copy', 'Copy tags from one file onto others', run=_tag_copy,
            emits='event',
            args=[Arg('source', 'File to copy from'),
                  Arg('target', 'Files to copy onto (repeatable)', nargs='*')],
            flags=[Flag('--tag', 'Only this frame id (repeatable)', short='-t',
                        metavar='ID', action='append')] + list(_FILTERS[:3]),
            example='backtrack tag copy disc1/01.mp3 disc1/*.mp3 -t TALB'),
    ]),

    Cmd('bulk', 'Operations across a whole album or selection', children=[
        Cmd('derive', 'Fill tags from file and folder names', run=_bulk_derive,
            emits='event', args=[Arg('target', 'Track files', nargs='*')],
            flags=[Flag('--field', 'Field to fill (repeatable; default title)',
                        short='-f', metavar='FIELD', action='append'),
                   Flag('--overwrite', 'Replace values that are already set',
                        action='store_true'),
                   Flag('--sort', 'Also generate sort-order tags',
                        action='store_true'),
                   Flag('--template', 'A %token% naming template to parse with'),
                   Flag('--regex', 'A named-group regex to parse with')]
                  + list(_FILTERS[:3]),
            example='backtrack bulk derive --album Rio -f title -f track'),
        Cmd('rename', 'Rename files from their tags', run=_bulk_rename,
            emits='event', args=[Arg('target', 'Track files', nargs='*')],
            flags=[Flag('--pattern', 'A %token% name pattern', short='-p',
                        default='%track% - %title%')] + list(_FILTERS[:3]),
            example='backtrack bulk rename --album Rio -p "%track% %title%"'),
        Cmd('art', 'Embed cover images found beside the tracks', run=_bulk_art,
            emits='event', args=[Arg('target', 'Track files', nargs='*')],
            flags=[Flag('--strategy', 'How to pair tracks with images',
                        short='-s', default='auto',
                        choices=('auto', 'basename', 'positional', 'best',
                                 'grouped', 'template')),
                   Flag('--group-by', 'What a grouped pairing groups on',
                        default='auto', choices=('auto', 'disc', 'folder')),
                   Flag('--pattern', 'A %token% pattern, for --strategy template'),
                   Flag('--overwrite', 'Replace art that is already embedded',
                        action='store_true')] + list(_FILTERS[:3]),
            example='backtrack bulk art --album Rio --strategy best'),
        Cmd('pictype', 'Retype embedded art without touching the image',
            run=_bulk_pictype, emits='event',
            args=[Arg('target', 'Track files', nargs='*')],
            flags=[Flag('--type', 'APIC picture type (3 = front cover)',
                        short='-t', type=int, default=3)] + list(_FILTERS[:3]),
            example='backtrack bulk pictype --album Rio --type 3'),
        Cmd('renumber', 'Renumber tracks across the album or per disc',
            run=_bulk_renumber, emits='event',
            args=[Arg('target', 'Track files', nargs='*')],
            flags=[Flag('--mode', 'Numbering to lay down', short='-m',
                        default='continuous',
                        choices=('continuous', 'per-disc'))] + list(_FILTERS[:3]),
            example='backtrack bulk renumber --album Rio --mode per-disc'),
        Cmd('reflow', 'Re-flow disc numbering onto a dense 1…N',
            run=_bulk_reflow, emits='event',
            args=[Arg('target', 'Track files', nargs='*')],
            flags=[Flag('--totals-only', 'Fix the totals, keep the numbers',
                        action='store_true'),
                   Flag('--track-totals', "Also fix each disc's track total",
                        action='store_true')] + list(_FILTERS[:3]),
            example='backtrack bulk reflow --album "The Wall"'),
        Cmd('stripdisc', 'Remove the disc number from tracks that are 1 of 1',
            run=_bulk_stripdisc, emits='event',
            args=[Arg('target', 'Track files', nargs='*')],
            flags=list(_FILTERS[:3]),
            example='backtrack bulk stripdisc --album Rio'),
        Cmd('striplength', 'Remove stale TLEN and non-zero TDLY frames',
            run=_bulk_striplength, emits='event',
            args=[Arg('target', 'Track files', nargs='*')],
            flags=list(_FILTERS[:3]),
            example='backtrack bulk striplength --artist "BBC Radio 4"'),
        Cmd('sortorders', 'Generate sort-order frames from the tags present',
            run=_bulk_sortorders, emits='event',
            args=[Arg('target', 'Track files', nargs='*')],
            flags=[Flag('--overwrite', 'Replace sort tags already set',
                        action='store_true')] + list(_FILTERS[:3]),
            example='backtrack bulk sortorders --artist "DJ Wren"'),
        Cmd('assign', 'Assign one tag by range, every-N or date schedule',
            run=_bulk_assign, emits='event',
            args=[Arg('target', 'Track files', nargs='*')],
            flags=[Flag('--tag', 'Frame id to assign', short='-t',
                        default='TIT1'),
                   Flag('--every', 'Group size for an every-N assignment',
                        type=int),
                   Flag('--value', 'Value template, e.g. "Series {n}"',
                        short='-v', default='{n}'),
                   Flag('--range', 'FIRST-LAST=VALUE (repeatable)',
                        metavar='SPEC', action='append'),
                   Flag('--start', 'Start date for a schedule (YYYY-MM-DD)'),
                   Flag('--interval', 'Days between scheduled dates', type=int),
                   Flag('--overwrite', 'Replace values already set',
                        action='store_true')] + list(_FILTERS[:3]),
            example='backtrack bulk assign -t TIT1 --every 6 -v "Series {n}"'),
    ]),

    Cmd('search', 'Fuzzy-search the library', run=_search, emits='tracks',
        args=[Arg('query', 'What to search for')],
        flags=[Flag('--scope', 'Field to search', short='-s',
                    choices=('all', 'title', 'artist', 'album', 'genre', 'people'),
                    default='all'),
               Flag('--limit', 'Show at most this many', short='-n', type=int,
                    default=20)],
        example='backtrack search "hungry wolf" --limit 5'),

    Cmd('history', 'Listening history', children=[
        Cmd('list', 'Recently played tracks, newest first', run=_history_list,
            emits='history',
            flags=[Flag('--limit', 'Show at most this many', short='-n',
                        type=int, default=30)],
            example='backtrack history list -n 10'),
        Cmd('clear', 'Delete the listening history log', run=_history_clear,
            emits='history', example='backtrack history clear --yes'),
    ]),

    Cmd('config', 'Read and change stored settings', children=[
        Cmd('list', 'Every config key and its current value', run=_config_list,
            emits='config', example='backtrack config list'),
        Cmd('get', 'One config value', run=_config_get, emits='config',
            args=[Arg('key', 'Config key')],
            example='backtrack config get lyric_lead_in'),
        Cmd('set', 'Change one config value', run=_config_set, emits='config',
            args=[Arg('key', 'Config key'), Arg('value', 'New value')],
            example='backtrack config set autoplay_on_select true'),
    ]),

    Cmd('schema', 'Print the command tree, flags and output shapes as JSON',
        run=_schema, emits='schema', example='backtrack schema | jq .commands'),

    Cmd('completion', 'Print a shell completion script', run=_completion,
        args=[Arg('shell', 'Shell to generate for',
                  choices=('bash', 'zsh', 'fish'))],
        example='backtrack completion zsh > ~/.zfunc/_backtrack'),
]
