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

    if not ctx.args.source:
        return out.fail(out.USAGE, "No .lrc file given.",
                        hint='pass --from track.lrc')
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

    applied = bo.apply_changes(plan, ctx.library, writer,
                               on_event=_reporter(ctx, len(changed)))
    _clear_progress(ctx)
    out.note(bo.summarise(applied, verb, **summary))
    return out.FAIL if applied.errors and not applied.written else out.OK


def _reporter(ctx: Ctx, total: int):
    """An `on_event` that reports each file and, on a terminal, draws progress.

    The progress bar is `ui_utils.print_inline_progress` — the same one the
    trimmer's ffmpeg passes use, so a long CLI run looks like a long in-app run.
    It is drawn only when stdout is a terminal, so a pipe and `--json` stay
    clean.
    """
    from src.utils import ui_utils

    interactive = out.is_tty() and not out.json_mode() and not ctx.args.quiet
    done = [0]

    def _report(kind: str, change, detail: str) -> None:
        """One file's outcome, plus a redrawn bar when anyone is watching."""
        done[0] += 1
        if interactive:
            ui_utils.print_inline_progress(
                f"{done[0]}/{total} {os.path.basename(change.path)}",
                done[0] / total if total else 1.0)
        else:
            out.event(kind, path=change.path, detail=detail)

    return _report


def _clear_progress(ctx: Ctx) -> None:
    """Erase the inline progress line, if one was drawn."""
    from src.utils import ui_utils
    if out.is_tty() and not out.json_mode() and not ctx.args.quiet:
        ui_utils.clear_inline_progress()


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


# --- playback ---------------------------------------------------------------
# A CLI process is not the audio host. When a Backtrack session is already
# running these commands drive it over the existing IPC socket and return at
# once; with no session, `play` hosts one itself and blocks until the queue
# ends, the way any other terminal player does.

def _session_link():
    """A connected client for the newest live session, or None."""
    from src.playback import ipc

    for info in ipc.list_sessions():
        link = ipc.SessionClient(info.get('socket', ''))
        if link.connect():
            return link, info
    return None, None


def _snapshot(link, tries: int = 20):
    """The host's first now-playing snapshot, waited for briefly."""
    import time

    for _ in range(tries):
        snap = link.latest()
        if snap is not None:
            return snap
        time.sleep(0.025)
    return None


def _now_playing_body(snap: dict) -> dict:
    """The fields `session status` reports, in both modes."""
    return {
        'playing': not snap.get('paused', False),
        'title': snap.get('title', ''),
        'artist': snap.get('artist', ''),
        'album': snap.get('album', ''),
        'path': snap.get('file_path', ''),
        'elapsed': round(float(snap.get('elapsed') or 0), 1),
        'duration': round(float(snap.get('duration') or 0), 1),
        'volume': snap.get('volume', 0),
        'queue_length': len(snap.get('queue') or []),
    }


def _session_list(ctx: Ctx) -> int:
    """Every Backtrack session currently running on this machine."""
    from src.playback import ipc

    rows = []
    for info in ipc.list_sessions():
        now = info.get('now_playing') or {}
        rows.append({'path': info.get('socket', ''), 'id': info.get('id', ''),
                     'label': info.get('label', ''),
                     'playing': now.get('title', '')})
    out.table('sessions', rows, [
        pc.Column(style='primary', flex=True),
        pc.Column(style='dynamic-dim', flex=True),
    ], cells=lambda r: [r['label'] or r['id'], r['playing'] or '(idle)'])
    return out.OK


def _session_status(ctx: Ctx) -> int:
    """What the running session is playing."""
    link, _info = _session_link()
    if link is None:
        return out.fail(out.NOT_FOUND, "No Backtrack session is running.")
    try:
        snap = _snapshot(link)
        if not snap:
            out.record('session', {'playing': False},
                       human="  Nothing is playing.")
            return out.OK
        out.record('session', _now_playing_body(snap))
        return out.OK
    finally:
        link.close()


def _transport(ctx: Ctx, command: str, args: dict | None = None,
               describe: str = '') -> int:
    """Send one transport command to the running session."""
    link, _info = _session_link()
    if link is None:
        return out.fail(out.NOT_FOUND, "No Backtrack session is running.",
                        hint="`backtrack play <track>` starts one")
    try:
        if ctx.dry_run():
            out.event('plan', action=command, detail=describe or command)
            return out.OK
        if not link.send(command, args or {}):
            return out.fail(out.FAIL, "Could not reach the session.")
        import time
        time.sleep(0.15)                     # let the host apply it before we look
        snap = _snapshot(link, tries=8)
        if snap:
            out.record('session', _now_playing_body(snap))
        else:
            out.note(describe or command)
        return out.OK
    finally:
        link.close()


def _session_pause(ctx: Ctx) -> int:
    """Toggle play/pause on the running session."""
    return _transport(ctx, 'pause', describe='Toggled play/pause.')


def _session_next(ctx: Ctx) -> int:
    """Skip to the next track."""
    return _transport(ctx, 'next', describe='Skipped to the next track.')


def _session_prev(ctx: Ctx) -> int:
    """Go back to the previous track."""
    return _transport(ctx, 'prev', describe='Went back a track.')


def _session_stop(ctx: Ctx) -> int:
    """Stop playback."""
    return _transport(ctx, 'stop', describe='Stopped.')


def _session_seek(ctx: Ctx) -> int:
    """Seek by a number of seconds, forwards or back."""
    return _transport(ctx, 'seek', {'delta': ctx.args.seconds},
                      describe=f"Sought {ctx.args.seconds:+g}s.")


def _session_volume(ctx: Ctx) -> int:
    """Set the volume, 0-100."""
    if not 0 <= ctx.args.level <= 100:
        return out.fail(out.USAGE, "Volume must be between 0 and 100.",
                        given=ctx.args.level)
    return _transport(ctx, 'set_volume', {'vol': ctx.args.level},
                      describe=f"Volume {ctx.args.level}.")


def _resolve_queue(ctx: Ctx) -> list:
    """The tracks a play/queue command was given, in library order."""
    from src.music_library import sort_library_logic

    paths = _targets_or_filter(ctx)
    if paths:
        known = {s['path']: s for s in ctx.library}
        songs = [known.get(p, {'path': p}) for p in paths if os.path.exists(p)]
        return sort_library_logic(songs) if len(songs) > 1 else songs
    return []


def _play(ctx: Ctx) -> int:
    """Play tracks — through a running session if there is one, else here.

    Handing a running session the queue is instant and leaves the audio with the
    process that owns it. With no session, this process becomes the host and
    blocks until the queue ends, because audio stops when its process exits.
    """
    songs = _resolve_queue(ctx)
    if not songs:
        return out.fail(out.USAGE, "Nothing to play.",
                        hint="pass a path, pipe one in, or use --artist/--album")
    paths = [s['path'] for s in songs]
    titles = [s.get('title') or os.path.basename(s['path']) for s in songs]

    if ctx.dry_run():
        for path, title in zip(paths, titles):
            out.event('plan', action='play', path=path, detail=title)
        return out.OK

    link, _info = _session_link()
    if link is not None:
        try:
            link.send('play', {'path': paths[0], 'queue': paths,
                               'titles': titles, 'index': 0,
                               'mode': ctx.args.repeat})
            out.record('session', {'playing': True, 'title': titles[0],
                                   'path': paths[0], 'queue_length': len(paths)},
                       human=f"  ▸ {titles[0]}")
            return out.OK
        finally:
            link.close()

    return _play_here(ctx, paths, titles)


def _play_here(ctx: Ctx, paths: list, titles: list) -> int:
    """Host a session in this process and block until the queue ends."""
    import time

    from src.playback.session import SESSION
    from src.utils import ui_utils

    SESSION.bind_config(ctx.config)
    SESSION.start(paths[0], queue=paths, titles=titles, index=0,
                  mode=ctx.args.repeat)
    current = None
    try:
        while SESSION.is_active():
            SESSION.tick()
            snap = SESSION.now_playing() or {}
            if snap.get('file_path') != current:
                current = snap.get('file_path')
                out.event('playing', path=current or '',
                          detail=snap.get('title') or '')
            if not out.json_mode() and not out.is_tty():
                time.sleep(0.25)
                continue
            if out.is_tty() and not out.json_mode():
                total = float(snap.get('duration') or 0)
                done = float(snap.get('elapsed') or 0)
                ui_utils.print_inline_progress(
                    f"{snap.get('title', '')} — {ui_utils.format_time(done)}"
                    f" / {ui_utils.format_time(total)}",
                    (done / total) if total else 0.0)
            time.sleep(0.25)
    except KeyboardInterrupt:
        pass
    finally:
        if out.is_tty() and not out.json_mode():
            ui_utils.clear_inline_progress()
        SESSION.shutdown()
    return out.OK


def _queue_add(ctx: Ctx) -> int:
    """Add tracks to the end of the running session's queue."""
    return _queue_send(ctx, 'enqueue', "Added to the queue")


def _queue_next(ctx: Ctx) -> int:
    """Put tracks next in the running session's queue."""
    return _queue_send(ctx, 'play_next', "Queued next")


def _queue_send(ctx: Ctx, command: str, verb: str) -> int:
    """The shared body of `queue add` and `queue next`."""
    from src.utils import ui_utils

    songs = _resolve_queue(ctx)
    if not songs:
        return out.fail(out.USAGE, "Nothing to queue.",
                        hint="pass a path, pipe one in, or use --artist/--album")
    if ctx.dry_run():
        for song in songs:
            out.event('plan', action=command, path=song['path'],
                      detail=song.get('title', ''))
        return out.OK

    link, _info = _session_link()
    if link is None:
        return out.fail(out.NOT_FOUND, "No Backtrack session is running.",
                        hint="`backtrack play <track>` starts one")
    try:
        for song in songs:
            title = song.get('title') or os.path.basename(song['path'])
            link.send(command, {'path': song['path'], 'title': title})
            out.event('queued', path=song['path'], detail=title)
        out.note(f"{verb}: {ui_utils.plural(len(songs), 'track')}.")
        return out.OK
    finally:
        link.close()


def _queue_show(ctx: Ctx) -> int:
    """What the running session has queued up."""
    link, _info = _session_link()
    if link is None:
        return out.fail(out.NOT_FOUND, "No Backtrack session is running.")
    try:
        snap = _snapshot(link) or {}
        paths = snap.get('queue') or []
        titles = snap.get('titles') or []
        index = int(snap.get('index', 0) or 0)
        rows = [{'path': p, 'title': titles[i] if i < len(titles) else
                 os.path.basename(p), 'position': i + 1,
                 'current': i == index}
                for i, p in enumerate(paths)]
        out.table('queue', rows, [
            pc.Column(style='dynamic-dim', min_width=3),
            pc.Column(style='primary', flex=True),
        ], cells=lambda r: [('▸' if r['current'] else str(r['position'])),
                            r['title']])
        return out.OK
    finally:
        link.close()


# --- lyrics -----------------------------------------------------------------
# The sync editor's tap and audition modes need a human listening to the audio,
# so they stay in the app. Everything that is a file transformation is here.

def _one_target(ctx: Ctx, what: str = "track"):
    """Exactly one file, or a failure code — for the single-subject commands."""
    paths = _targets_or_filter(ctx)
    if not paths:
        return None, out.fail(out.USAGE, f"No {what} given.",
                              hint="pass a path or pipe one in")
    if not os.path.exists(paths[0]):
        return None, out.fail(out.NOT_FOUND, f"No such {what}.", path=paths[0])
    return paths[0], out.OK


def _lyric_lines(path: str) -> tuple[list, str]:
    """A track's lyrics as `(rows, source)` — timed SYLT first, else USLT."""
    from mutagen.id3 import ID3

    from src.lyrics import lyrics as ly

    audio = ID3(path)
    timed = ly._parse_sylt(audio)
    if timed:
        return ([{'path': path, 'time_ms': ms, 'text': text}
                 for text, ms in timed], 'SYLT')
    plain = ly._parse_uslt(audio)
    return ([{'path': path, 'time_ms': None, 'text': text}
             for text, _ms in plain], 'USLT')


def _ms(value) -> str:
    """A millisecond timestamp as [mm:ss.mmm], or blank when untimed."""
    if value is None:
        return ''
    total = int(value)
    return f"[{total // 60000:02d}:{total // 1000 % 60:02d}.{total % 1000:03d}]"


def _lyrics_show(ctx: Ctx) -> int:
    """Print a track's lyrics."""
    from mutagen.id3 import ID3NoHeaderError  # type: ignore[reportPrivateImportUsage]

    path, code = _one_target(ctx)
    if path is None:
        return code
    try:
        rows, source = _lyric_lines(path)
    except ID3NoHeaderError:
        rows, source = [], ''
    if not rows:
        return out.fail(out.NOT_FOUND, "That track has no lyrics.", path=path)
    if out.json_mode():
        out.record('lyrics', {'path': path, 'source': source,
                              'count': len(rows), 'lines': rows})
        return out.OK
    out.table('lyrics', rows, [
        pc.Column(style='dynamic-dim', min_width=12),
        pc.Column(style='normal', flex=True),
    ], cells=lambda r: [_ms(r['time_ms']), r['text']], pipe_key='text')
    return out.OK


def _lyrics_import(ctx: Ctx) -> int:
    """Import lyrics from an .lrc file into SYLT (timed) or USLT (untimed)."""
    from src.lyrics import lyrics as ly
    from src.music_library import refresh_library_entry

    path, code = _one_target(ctx)
    if path is None:
        return code
    if not ctx.args.source:
        return out.fail(out.USAGE, "No .lrc file given.",
                        hint='pass --from track.lrc')
    source = os.path.abspath(os.path.expanduser(ctx.args.source))
    if not os.path.exists(source):
        return out.fail(out.NOT_FOUND, "No such .lrc file.", path=source)

    entries = ly.parse_lrc_file(source)
    if not entries:
        return out.fail(out.FAIL, "Nothing readable in that .lrc file.",
                        path=source)
    timed = any(ms is not None for _text, ms in entries)

    if ctx.dry_run():
        out.event('plan', path=path,
                  detail=f"{len(entries)} lines into "
                         f"{'SYLT' if timed else 'USLT'}")
        return out.OK

    tag_id, count = ly.import_lrc(path, source)
    if not tag_id:
        return out.fail(out.FAIL, "Nothing readable in that .lrc file.",
                        path=source)
    try:
        refresh_library_entry(ctx.library, path)
    except Exception:
        pass
    out.record('lyrics', {'path': path, 'tag': tag_id, 'lines': count},
               human=f"  Imported {count} lines into {tag_id}.")
    return out.OK


def _lyrics_export(ctx: Ctx) -> int:
    """Write a track's lyrics out as .lrc, .srt or plain text."""
    path, code = _one_target(ctx)
    if path is None:
        return code
    rows, source = _lyric_lines(path)
    if not rows:
        return out.fail(out.NOT_FOUND, "That track has no lyrics.", path=path)

    fmt = ctx.args.format
    if fmt == 'lrc':
        text = "\n".join(f"{_ms(r['time_ms'])}{r['text']}" for r in rows)
    elif fmt == 'srt':
        parts = []
        for i, row in enumerate(rows, 1):
            start = int(row['time_ms'] or 0)
            end = int(rows[i]['time_ms']) if i < len(rows) and rows[i]['time_ms'] \
                else start + 3000
            parts.append(f"{i}\n{_srt(start)} --> {_srt(end)}\n{row['text']}\n")
        text = "\n".join(parts)
    else:
        text = "\n".join(r['text'] for r in rows)

    stem = os.path.splitext(os.path.basename(path))[0]
    directory = (ctx.args.output or ctx.config.get('cli_output_dir')
                 or os.path.dirname(path))
    target = os.path.join(os.path.expanduser(directory), f"{stem}.{fmt}")
    if ctx.dry_run():
        out.event('plan', path=target, detail=f"{len(rows)} lines as {fmt}")
        return out.OK
    if os.path.exists(target) and not ctx.confirm(f"Overwrite {target}?"):
        return out.fail(out.EXISTS, "That file already exists.", path=target)
    with open(target, 'w', encoding='utf-8') as handle:
        handle.write(text + "\n")
    out.record('lyrics', {'path': target, 'format': fmt, 'lines': len(rows),
                          'source': source},
               human=f"  Wrote {len(rows)} lines to {target}.")
    return out.OK


def _srt(ms_value: int) -> str:
    """A millisecond timestamp in SRT's HH:MM:SS,mmm form."""
    return (f"{ms_value // 3600000:02d}:{ms_value // 60000 % 60:02d}:"
            f"{ms_value // 1000 % 60:02d},{ms_value % 1000:03d}")


def _lyrics_verify(ctx: Ctx) -> int:
    """Report whether a track's script and transcript still line up."""
    from src.lyrics import lyrics as ly

    path, code = _one_target(ctx)
    if path is None:
        return code
    md_path, json_path = ly._find_timing_files_for_audio(path)
    body = {'path': path, 'script': md_path or '', 'transcript': json_path or ''}
    if not md_path and not json_path:
        out.record('lyrics', {**body, 'status': 'none'},
                   human="  No script or transcript beside this track.")
        return out.NOT_FOUND
    if not (md_path and json_path):
        out.record('lyrics', {**body, 'status': 'partial'},
                   human=f"  Only the {'script' if md_path else 'transcript'} "
                         "is present — nothing to check it against.")
        return out.FAIL

    from src.lyrics.md_overlay import build_md_overlay
    transcript = ly.load_transcript(json_path) or {}
    segments = transcript.get('segments') or []
    _overlay, quality, _links = build_md_overlay(segments, md_path)
    flags = [key for key, value in (quality or {}).items() if value]
    out.record('lyrics', {**body, 'status': 'checked',
                          'segments': len(segments), 'flagged': len(flags)},
               human=f"  {len(segments)} segments, {len(flags)} flagged.")
    return out.FAIL if flags else out.OK


# --- trim -------------------------------------------------------------------
# Marking a cut by ear is the editor's job. Detecting candidates, performing a
# cut you can already name, and the backup store are all headless.

def _need_ffmpeg() -> int | None:
    """The missing-tool failure, when ffmpeg is not on PATH."""
    from src.trim import trim as t
    if not t.HAS_FFMPEG:
        return out.fail(out.NO_TOOL, "ffmpeg is required for trimming.",
                        hint="brew install ffmpeg, or set trim_ffmpeg_path")
    return None


def _trim_detect(ctx: Ctx) -> int:
    """Suggest cut points from the silence near a track's head and tail."""
    from src.trim import trim as t

    missing = _need_ffmpeg()
    if missing is not None:
        return missing
    path, code = _one_target(ctx)
    if path is None:
        return code

    window = ctx.args.window
    rows = []
    try:
        bounds = [(region, t.window_bounds(path, window, region))
                  for region in ('head', 'tail')]
    except Exception as exc:
        return out.fail(out.FAIL, "Could not read that file's audio.",
                        path=path, reason=str(exc))
    for region, (start, dur) in bounds:
        for begin, end in t.detect_silence(
                path, start, dur,
                noise_db=ctx.config.get('trim_silence_noise_db', -32.0),
                min_s=ctx.config.get('trim_silence_min_s', 0.4)):
            rows.append({'path': path, 'region': region,
                         'start': round(begin, 3), 'end': round(end, 3),
                         'length': round(end - begin, 3)})
    if not rows:
        out.record('trim', {'path': path, 'silences': 0},
                   human="  No silence found near either end.")
        return out.OK
    out.table('silences', rows, [
        pc.Column(style='primary', min_width=6),
        pc.Column(style='normal', min_width=10),
        pc.Column(style='normal', min_width=10),
        pc.Column(style='dynamic-dim', align='right', pin=True),
    ], cells=lambda r: [r['region'], f"{r['start']:.3f}", f"{r['end']:.3f}",
                        f"{r['length']:.3f}s"], pipe_key='start')
    return out.OK


def _trim_cut(ctx: Ctx) -> int:
    """Cut a track losslessly between two points, backing up the original."""
    from src.trim import trim as t

    missing = _need_ffmpeg()
    if missing is not None:
        return missing
    path, code = _one_target(ctx)
    if path is None:
        return code

    # Argument validation before file I/O: bad points are bad points whether or
    # not the file turns out to be readable, and that should read as a usage
    # error rather than whatever mutagen says about the audio.
    if ctx.args.end is not None and ctx.args.end <= ctx.args.start:
        return out.fail(out.USAGE, "The out point must come after the in point.",
                        start=ctx.args.start, end=ctx.args.end)
    if ctx.args.start < 0:
        return out.fail(out.USAGE, "The in point cannot be negative.",
                        start=ctx.args.start)

    try:
        frame = t.probe_frame_duration(path)
        end_raw = ctx.args.end
        if end_raw is None:
            from src.music_library import get_song_duration
            end_raw = get_song_duration(path)
    except Exception as exc:
        return out.fail(out.FAIL, "Could not read that file's audio.",
                        path=path, reason=str(exc))
    start = t.snap_in_point(ctx.args.start, frame)
    end = t.snap_out_point(end_raw, frame)
    if end <= start:
        return out.fail(out.USAGE, "The out point must come after the in point.",
                        start=start, end=end)

    if ctx.dry_run():
        out.event('plan', path=path, action='cut',
                  detail=f"keep {start:.3f}s to {end:.3f}s "
                         f"({end - start:.3f}s of audio)")
        return out.OK
    if not ctx.confirm(f"Cut {os.path.basename(path)} to "
                       f"{start:.3f}-{end:.3f}s?", default=True):
        out.note("Left alone.")
        return out.OK

    # The editor asks about every disturbed chapter; with no human to ask, the
    # --chapters policy decides for all of them.
    result = t.commit_trim(path, start, end, ctx.library,
                           chapters=t.apply_chapter_policy(
                               path, start, end, ctx.args.chapters))
    if not result.ok:
        return out.fail(out.FAIL, result.error or "The cut failed.", path=path)
    out.record('trim', {'path': path, 'start': start, 'end': end,
                        'duration': round(end - start, 3)},
               human=f"  Cut to {end - start:.3f}s. The original is backed up.")
    return out.OK


def _trim_list(ctx: Ctx) -> int:
    """Every backed-up original a trim can be undone from."""
    from src.trim import trim as t

    import datetime

    rows = []
    for entry in t.list_backups():
        stamp = entry.get('timestamp')
        when = (datetime.datetime.fromtimestamp(stamp).strftime('%Y-%m-%d %H:%M')
                if stamp else '')
        rows.append({'path': entry.get('original_path', ''),
                     'id': entry.get('id', ''),
                     'when': when,
                     'kept_from': entry.get('snapped_in_s', 0.0),
                     'kept_to': entry.get('snapped_out_s', 0.0),
                     'original_length': entry.get('original_length_s', 0.0)})
    out.table('backups', rows, [
        pc.Column(style='primary', min_width=12),
        pc.Column(style='normal', flex=True),
        pc.Column(style='dynamic-dim', align='right', pin=True),
    ], cells=lambda r: [r['id'], os.path.basename(r['path']), r['when']],
        pipe_key='id')
    return out.OK


def _trim_restore(ctx: Ctx) -> int:
    """Put a backed-up original back."""
    from src.trim import trim as t

    entry_id = str(ctx.args.id or '')
    known = {entry.get('id') for entry in t.list_backups()}
    if entry_id not in known:
        return out.fail(out.NOT_FOUND, "No such backup.", id=entry_id,
                        hint="`backtrack trim list` shows them")
    if ctx.dry_run():
        out.event('plan', action='restore', detail=entry_id)
        return out.OK
    if not ctx.confirm(f"Restore backup {entry_id}, replacing the trimmed file?",
                       default=True):
        out.note("Left alone.")
        return out.OK
    result = t.restore_backup(entry_id, ctx.library)
    if not result.ok:
        return out.fail(out.FAIL, result.error or "The restore failed.",
                        id=entry_id)
    out.record('trim', {'id': entry_id, 'restored': True},
               human="  Restored.")
    return out.OK


# --- feeds ------------------------------------------------------------------
# Downloaded audio enters the library the way any other new file does: it is
# written into a music directory and handed to refresh_library_entry. There is
# deliberately no second route in.

def _feed_root(ctx: Ctx) -> str | None:
    """Where downloaded episodes should land — --output, else the first music
    directory."""
    from src.config import music_dirs

    chosen = ctx.args.output or ctx.config.get('cli_output_dir') or ''
    if chosen:
        return os.path.abspath(os.path.expanduser(chosen))
    roots = music_dirs(ctx.config)
    return roots[0] if roots else None


def _feed_add(ctx: Ctx) -> int:
    """Subscribe to a feed."""
    from src import feed as fd

    url = ctx.args.url
    feeds = fd.load_feeds()
    try:
        parsed = fd.parse_feed(fd.fetch(url))
    except (OSError, ValueError) as exc:
        return out.fail(out.FAIL, "Could not read that feed.", url=url,
                        reason=str(exc))

    name = fd.slugify(ctx.args.name or parsed.title or url)
    if name in feeds:
        return out.fail(out.EXISTS, "A feed by that name is already added.",
                        name=name, hint="pass --name to choose another")

    entry = {'url': url, 'title': parsed.title,
             'filter_title': ctx.args.filter_title or '',
             'seen': fd.new_seen()}
    matched = fd.matching(parsed.items, entry['filter_title'])
    if ctx.dry_run():
        out.event('plan', action='feed-add', name=name,
                  detail=f"{parsed.title} — {len(matched)} matching episodes")
        return out.OK

    feeds[name] = entry
    fd.save_feeds(feeds)
    out.record('feed', {'name': name, 'url': url, 'title': parsed.title,
                        'filter_title': entry['filter_title'],
                        'episodes': len(matched)},
               human=f"  Added {name} — {parsed.title}, "
                     f"{len(matched)} episodes waiting.")
    return out.OK


def _feed_list(ctx: Ctx) -> int:
    """Every subscribed feed."""
    from src import feed as fd

    feeds = fd.load_feeds()
    rows = [{'path': name, 'name': name, 'title': entry.get('title', ''),
             'url': entry.get('url', ''),
             'filter_title': entry.get('filter_title', ''),
             'downloaded': len(entry.get('seen') or [])}
            for name, entry in sorted(feeds.items())]
    out.table('feeds', rows, [
        pc.Column(style='primary', max_frac=0.3),
        pc.Column(style='normal', flex=True),
        pc.Column(style='dynamic-dim', align='right', pin=True),
    ], cells=lambda r: [r['name'], r['title'],
                        f"{r['downloaded']} downloaded"], pipe_key='name')
    return out.OK


def _feed_remove(ctx: Ctx) -> int:
    """Unsubscribe from a feed. The downloaded files are left alone."""
    from src import feed as fd

    feeds = fd.load_feeds()
    name = ctx.args.name
    if name not in feeds:
        return out.fail(out.NOT_FOUND, "No feed by that name.", name=name,
                        hint="`backtrack feed list` shows them")
    if ctx.dry_run():
        out.event('plan', action='feed-remove', name=name)
        return out.OK
    if not ctx.confirm(f"Stop following {name}? (downloaded files are kept)",
                       default=True):
        out.note("Left alone.")
        return out.OK
    feeds.pop(name)
    fd.save_feeds(feeds)
    out.record('feed', {'name': name, 'removed': True},
               human=f"  Removed {name}. The downloaded files are still there.")
    return out.OK


def _feed_fetch(ctx: Ctx) -> int:
    """Read a feed and print what is in it, storing nothing."""
    from src import feed as fd

    try:
        parsed = fd.parse_feed(fd.fetch(ctx.args.url))
    except (OSError, ValueError) as exc:
        return out.fail(out.FAIL, "Could not read that feed.",
                        url=ctx.args.url, reason=str(exc))

    items = fd.matching(parsed.items, ctx.args.filter_title or '')
    rows = [item.as_dict() for item in items]
    for row in rows:
        row['path'] = row.get('url', '')
    out.table('episodes', rows, [
        pc.Column(style='dynamic-dim', min_width=10),
        pc.Column(style='normal', max_frac=0.25),
        pc.Column(style='primary', flex=True),
    ], cells=lambda r: [r['date'] or '', r['show'] or '',
                        r['title'] or r['raw']], pipe_key='url')
    return out.OK


def _feed_sync(ctx: Ctx) -> int:
    """Download everything new from one feed, or all of them."""
    from src import feed as fd
    from src.utils import ui_utils

    feeds = fd.load_feeds()
    # A named feed that does not exist is not found, whether or not any others
    # do — "there are no feeds" answers a different question from the one asked.
    if ctx.args.name and ctx.args.name not in feeds:
        return out.fail(out.NOT_FOUND, "No feed by that name.",
                        name=ctx.args.name,
                        hint="`backtrack feed list` shows them")
    if not feeds:
        out.note("No feeds added. `backtrack feed add <url>` starts one.")
        return out.OK
    wanted = [ctx.args.name] if ctx.args.name else sorted(feeds)

    root = _feed_root(ctx)
    if not root:
        return out.fail(out.USAGE, "Nowhere to put the downloads.",
                        hint="pass --output DIR, or set a music directory")

    downloaded = skipped = errors = planned = 0
    for name in wanted:
        entry = feeds[name]
        try:
            parsed = fd.parse_feed(fd.fetch(entry['url']))
        except (OSError, ValueError) as exc:
            errors += 1
            out.event('error', name=name, detail=str(exc))
            continue

        seen = set(entry.get('seen') or [])
        items = fd.matching(parsed.items, entry.get('filter_title', ''))
        fresh = [i for i in items if i.key and i.key not in seen and i.url]
        skipped += len(items) - len(fresh)

        for item in fresh:
            target = fd.target_path(item, root, parsed.title)
            if os.path.exists(target):
                # Already on disk under the name we would give it: record it as
                # seen so the next sync stops reconsidering it.
                seen.add(item.key)
                skipped += 1
                out.event('skipped', path=target, detail=item.parsed.title)
                continue
            if ctx.dry_run():
                planned += 1
                out.event('plan', action='download', path=target,
                          detail=item.parsed.title or item.parsed.raw,
                          url=item.url)
                continue
            try:
                _download_one(ctx, item, target, parsed.title)
            except OSError as exc:
                errors += 1
                out.event('error', path=target, detail=str(exc))
                continue
            seen.add(item.key)
            downloaded += 1
            out.event('written', path=target,
                      detail=item.parsed.title or item.parsed.raw)

        if not ctx.dry_run():
            entry['seen'] = sorted(seen)
            entry['title'] = parsed.title or entry.get('title', '')
            feeds[name] = entry

    if ctx.dry_run():
        out.note(f"{ui_utils.plural(planned, 'episode')} would be downloaded, "
                 f"{skipped} already had.")
        return out.OK
    fd.save_feeds(feeds)
    out.note(f"{ui_utils.plural(downloaded, 'new episode')}, "
             f"{skipped} already had"
             + (f", {ui_utils.plural(errors, 'error')}" if errors else "") + ".")
    return out.FAIL if errors and not downloaded else out.OK


def _download_one(ctx: Ctx, item, target: str, feed_title: str) -> None:
    """Fetch one episode, tag it, and let the library know it exists."""
    from src import feed as fd
    from src.id3 import tag_writer as tw
    from src.music_library import refresh_library_entry
    from src.utils import ui_utils

    interactive = out.is_tty() and not out.json_mode() and not ctx.args.quiet
    label = item.parsed.title or item.parsed.raw

    def _progress(done: int, total: int) -> None:
        """Draw the app's own inline progress bar while a download runs."""
        ui_utils.print_inline_progress(label, (done / total) if total else 0.0)

    fd.download(item.url, target, on_progress=_progress if interactive else None)
    if interactive:
        ui_utils.clear_inline_progress()

    # The file was downloaded a moment ago and nothing else has touched it, so
    # these writes overwrite: the TDRC the frame write carries is the full
    # broadcast date, and it has to win over the year the field write left.
    fields, frames = fd.tag_plan(item, feed_title)
    tw.write_fields(target, fields, set(fields), overwrite=True)
    if frames and tw.format_kind(target) == 'mp3':
        from src.id3 import bulk_ops as bo
        bo.apply_frame_writes({target: list(frames.items())}, [], overwrite=True)
    try:
        refresh_library_entry(ctx.library, target)
    except Exception:
        pass


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
                        default='%track% - %title%',
                        config_key='cli_rename_pattern')] + list(_FILTERS[:3]),
            example='backtrack bulk rename --album Rio -p "%track% %title%"'),
        Cmd('art', 'Embed cover images found beside the tracks', run=_bulk_art,
            emits='event', args=[Arg('target', 'Track files', nargs='*')],
            flags=[Flag('--strategy', 'How to pair tracks with images',
                        short='-s', default='auto', config_key='cli_art_strategy',
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

    Cmd('play', 'Play tracks — through a running session, or here', run=_play,
        emits='session', args=[Arg('target', 'Track files', nargs='*')],
        flags=[Flag('--repeat', 'Repeat mode', short='-r', default='linear',
                    choices=('linear', 'one', 'all'))] + list(_FILTERS[:3]),
        example='backtrack play --album Rio --repeat all'),

    Cmd('queue', "The running session's queue", children=[
        Cmd('show', 'What is queued up', run=_queue_show, emits='queue',
            example='backtrack queue show'),
        Cmd('add', 'Add tracks to the end of the queue', run=_queue_add,
            emits='event', args=[Arg('target', 'Track files', nargs='*')],
            flags=list(_FILTERS[:3]),
            example='backtrack queue add --artist Darude'),
        Cmd('next', 'Put tracks next in the queue', run=_queue_next,
            emits='event', args=[Arg('target', 'Track files', nargs='*')],
            flags=list(_FILTERS[:3]),
            example='backtrack queue next track.mp3'),
    ]),

    Cmd('session', 'Control a running Backtrack session', children=[
        Cmd('list', 'Every session running on this machine', run=_session_list,
            emits='sessions', example='backtrack session list'),
        Cmd('status', 'What the running session is playing',
            run=_session_status, emits='session',
            example='backtrack session status --json'),
        Cmd('pause', 'Toggle play/pause', run=_session_pause, emits='session',
            example='backtrack session pause'),
        Cmd('next', 'Skip to the next track', run=_session_next,
            emits='session', example='backtrack session next'),
        Cmd('prev', 'Go back to the previous track', run=_session_prev,
            emits='session', example='backtrack session prev'),
        Cmd('stop', 'Stop playback', run=_session_stop, emits='session',
            example='backtrack session stop'),
        Cmd('seek', 'Seek forwards or back', run=_session_seek, emits='session',
            args=[Arg('seconds', 'Seconds to move, negative to go back',
                      type=float)],
            example='backtrack session seek -- -30'),
        Cmd('volume', 'Set the volume, 0-100', run=_session_volume,
            emits='session',
            args=[Arg('level', 'Volume level', type=int)],
            example='backtrack session volume 70'),
    ]),

    Cmd('lyrics', 'Read, import and export lyrics', children=[
        Cmd('show', "Print a track's lyrics", run=_lyrics_show, emits='lyrics',
            args=[Arg('target', 'Track file', nargs='*')],
            example='backtrack lyrics show track.mp3'),
        Cmd('import', 'Import lyrics from an .lrc file', run=_lyrics_import,
            emits='lyrics', args=[Arg('target', 'Track file', nargs='*')],
            flags=[Flag('--from', 'The .lrc file to read', short='-f',
                        metavar='FILE', store_as='source')],
            example='backtrack lyrics import track.mp3 --from track.lrc'),
        Cmd('export', 'Write lyrics out as .lrc, .srt or plain text',
            run=_lyrics_export, emits='lyrics',
            args=[Arg('target', 'Track file', nargs='*')],
            flags=[Flag('--format', 'What to write', short='-f', default='lrc',
                        choices=('lrc', 'srt', 'txt'))],
            example='backtrack lyrics export track.mp3 --format srt'),
        Cmd('verify', "Check a track's script against its transcript",
            run=_lyrics_verify, emits='lyrics',
            args=[Arg('target', 'Track file', nargs='*')],
            example='backtrack lyrics verify episode.mp3'),
    ]),

    Cmd('trim', 'Cut tracks losslessly, and undo it', children=[
        Cmd('detect', 'Suggest cut points from the silence near each end',
            run=_trim_detect, emits='silences',
            args=[Arg('target', 'Track file', nargs='*')],
            flags=[Flag('--window', 'Seconds to scan at each end', short='-w',
                        type=float, default=30.0,
                        config_key='trim_scan_window_s')],
            example='backtrack trim detect episode.mp3 --window 60'),
        Cmd('cut', 'Cut a track between two points', run=_trim_cut,
            emits='trim', args=[Arg('target', 'Track file', nargs='*')],
            flags=[Flag('--start', 'In point, in seconds', short='-s',
                        type=float, default=0.0),
                   Flag('--end', 'Out point, in seconds (default: the end)',
                        short='-e', type=float),
                   Flag('--chapters', 'What to do with disturbed chapters',
                        default='clamp', choices=('clamp', 'drop', 'keep'))],
            example='backtrack trim cut episode.mp3 --start 12.5 --end 1800'),
        Cmd('list', 'Every backed-up original a trim can be undone from',
            run=_trim_list, emits='backups', example='backtrack trim list'),
        Cmd('restore', 'Put a backed-up original back', run=_trim_restore,
            emits='trim', args=[Arg('id', 'Backup id from `trim list`')],
            example='backtrack trim restore a1b2c3d4e5f6'),
    ]),

    Cmd('feed', 'Follow podcast feeds and import their episodes', children=[
        Cmd('add', 'Subscribe to a feed', run=_feed_add, emits='feed',
            args=[Arg('url', 'Feed URL')],
            flags=[Flag('--name', 'Short name to file it under', short='-n',
                        metavar='SLUG'),
                   Flag('--filter-title', 'Only episodes whose title contains '
                                          'this', metavar='TEXT')],
            example='backtrack feed add https://example.com/rss '
                    '--name comedy --filter-title "News Quiz"'),
        Cmd('list', 'Every feed being followed', run=_feed_list, emits='feeds',
            example='backtrack feed list'),
        Cmd('remove', 'Stop following a feed (downloads are kept)',
            run=_feed_remove, emits='feed',
            args=[Arg('name', 'Feed name from `feed list`')],
            example='backtrack feed remove comedy'),
        Cmd('sync', 'Download everything new', run=_feed_sync, emits='event',
            flags=[Flag('--name', 'Just this feed', short='-n',
                        metavar='SLUG')],
            example='backtrack feed sync --name comedy --output ~/Music/Podcasts'),
        Cmd('fetch', 'Read a feed and print it, storing nothing',
            run=_feed_fetch, emits='episodes', args=[Arg('url', 'Feed URL')],
            flags=[Flag('--filter-title', 'Only episodes whose title contains '
                                          'this', metavar='TEXT')],
            example='backtrack feed fetch https://example.com/rss --json'),
    ]),

    Cmd('search', 'Fuzzy-search the library', run=_search, emits='tracks',
        args=[Arg('query', 'What to search for')],
        flags=[Flag('--scope', 'Field to search', short='-s',
                    choices=('all', 'title', 'artist', 'album', 'genre', 'people'),
                    default='all'),
               Flag('--limit', 'Show at most this many', short='-n', type=int,
                    default=20, config_key='cli_search_limit')],
        example='backtrack search "hungry wolf" --limit 5'),

    Cmd('history', 'Listening history', children=[
        Cmd('list', 'Recently played tracks, newest first', run=_history_list,
            emits='history',
            flags=[Flag('--limit', 'Show at most this many', short='-n',
                        type=int, default=30, config_key='cli_history_limit')],
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
