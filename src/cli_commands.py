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


# --- the tree ---------------------------------------------------------------

_FILTERS = [
    Flag('--artist', 'Only tracks whose artist contains this', short='-a'),
    Flag('--album', 'Only tracks whose album contains this', short='-A'),
    Flag('--genre', 'Only tracks whose genre contains this', short='-g'),
    Flag('--limit', 'Show at most this many', short='-n', type=int),
]

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
