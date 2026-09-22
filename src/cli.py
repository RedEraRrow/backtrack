"""The command-line interface: one definition, three readers.

`backtrack` with no arguments is the app it has always been. With arguments it
is an ordinary command-line tool, noun then verb, and everything the menus can
do is reachable from here.

The command tree below is **data**, not argparse calls. `build_parser` turns it
into argparse, `schema` prints it as JSON, and `completion` generates the bash /
zsh / fish scripts from it. Three consumers, one source, so a flag cannot exist
in the parser and be missing from the schema or the completions — which is the
usual way a hand-written completion script rots.

Handlers are thin: they resolve arguments, call the same internal functions the
menus call, and hand the result to `utils.output`. Nothing here decides what a
bulk operation *does* (that is `id3.bulk_ops`) or what output looks like (that
is `utils.output`).
"""
from __future__ import annotations

import argparse
import os
import sys
from dataclasses import dataclass, field
from typing import Callable

from src.utils import output as out


@dataclass
class Flag:
    """One option, in the form all three readers need it."""
    name: str                       # '--overwrite'
    help: str
    short: str = ''                 # '-o'
    metavar: str = ''
    action: str = ''                # 'store_true' | 'append' | ''
    default: object = None
    choices: tuple = ()
    type: Callable | None = None
    store_as: str = ''              # override, for a name that is a keyword
    config_key: str = ''            # config key this falls back to when unset

    @property
    def dest(self) -> str:
        """The attribute argparse stores this flag under.

        `store_as` exists for `--from`, whose natural destination is a Python
        keyword and so cannot be read back off the namespace.
        """
        return self.store_as or self.name.lstrip('-').replace('-', '_')


@dataclass
class Arg:
    """One positional argument."""
    name: str
    help: str
    nargs: str | int | None = None
    choices: tuple = ()
    type: Callable | None = None


@dataclass
class Cmd:
    """One command or command group.

    A group has `children` and no `run`; a leaf has `run` and no children.
    `example` is mandatory on a leaf — every command's `--help` shows at least
    one worked invocation, because a flag list alone never answers "but how do I
    actually use it".
    """
    name: str
    help: str
    example: str = ''
    args: list = field(default_factory=list)
    flags: list = field(default_factory=list)
    children: list = field(default_factory=list)
    run: Callable | None = None
    emits: str = ''                 # the output `kind` this command produces

    @property
    def is_group(self) -> bool:
        """Whether this node dispatches to children rather than doing work."""
        return bool(self.children)


# --- global flags -----------------------------------------------------------
# Accepted before or after the subcommand, so `backtrack --json library list`
# and `backtrack library list --json` both work. argparse gets them via a
# parent parser shared by every subparser.

GLOBAL_FLAGS = [
    Flag('--json', 'Emit machine-readable JSON (NDJSON for long operations)',
         action='store_true'),
    Flag('--yes', 'Answer yes to every confirmation; never prompt', short='-y',
         action='store_true'),
    Flag('--dry-run', 'Print the plan and change nothing', action='store_true'),
    Flag('--library', 'Music directory to work in (repeatable)', short='-L',
         metavar='DIR', action='append'),
    Flag('--output', 'Where files this command writes should go', short='-o',
         metavar='DIR'),
    Flag('--quiet', 'Suppress human output; exit code still reports',
         short='-q', action='store_true'),
    Flag('--no-colour', 'Never colour the output', action='store_true'),
]


class Ctx:
    """What a handler is given: the parsed arguments and lazy access to the app.

    The library is loaded on first use, so `backtrack schema` and
    `backtrack config get` stay instant on a large collection.
    """

    def __init__(self, args: argparse.Namespace) -> None:
        """Hold the arguments; everything else is resolved on demand."""
        self.args = args
        self._config: dict | None = None
        self._library: list | None = None

    @property
    def config(self) -> dict:
        """The user's config, with `--library` overriding the stored directories."""
        if self._config is None:
            from src.config import load_config, set_music_dirs
            self._config = load_config()
            if self.args.library:
                set_music_dirs(self._config, list(self.args.library))
        return self._config

    @property
    def library(self) -> list:
        """The cached library, built from disk if there is no cache yet."""
        if self._library is None:
            from src.music_library import (build_library, load_library_cache,
                                           save_library_cache)
            from src.config import music_dirs
            self._library = load_library_cache()
            if not self._library:
                roots = music_dirs(self.config)
                if roots:
                    self._library = build_library(
                        roots,
                        ignore_hidden=self.config.get('ignore_hidden_files', False))
                    save_library_cache(self._library, _async=False)
        return self._library or []

    def targets(self, positional: str = 'target', *,
                allow_stdin: bool = True) -> list[str]:
        """The files this run acts on: the positional arguments, else stdin.

        Lets `backtrack track list --artist Darude | backtrack tag read` work
        without either command knowing about the other. `allow_stdin=False` is
        for a caller with another source to try first — reading stdin is a
        blocking call, so it must be the last thing asked, not the second.
        """
        given = getattr(self.args, positional, None) or []
        if isinstance(given, str):
            given = [given]
        paths = [os.path.abspath(os.path.expanduser(p)) for p in given]
        if paths or not allow_stdin:
            return paths
        return out.read_stdin_paths()

    def confirm(self, question: str, *, default: bool = False) -> bool:
        """Ask before something destructive — unless we were told not to.

        `--yes` accepts, `--dry-run` accepts (nothing is written anyway), and a
        non-terminal stdin takes `default` rather than blocking: an agent with no
        human attached must never wait on a read that will never come.
        """
        if self.args.yes or self.args.dry_run:
            return True
        if not sys.stdin.isatty() or not out.is_tty():
            return default
        from src.utils import prompt
        return bool(prompt.confirm(question, default=default))

    def dry_run(self) -> bool:
        """Whether this run prints its plan instead of performing it."""
        return bool(self.args.dry_run)


# --- argparse construction --------------------------------------------------

def _add_flag(parser: argparse.ArgumentParser, flag: Flag,
              suppress_default: bool = False) -> None:
    """Add one `Flag` to a parser, translating its fields to argparse's.

    `suppress_default` is for the global flags. They are declared on both the
    top-level parser and every subparser so they can be written either side of
    the verb, and with ordinary defaults the subparser's copy overwrites what
    the top level parsed — `backtrack --json library list` came out human.
    Suppressing the default means the attribute only exists when the flag was
    actually given, wherever it was given, and `_apply_global_defaults` fills in
    the rest afterwards.
    """
    names = [flag.name] + ([flag.short] if flag.short else [])
    kwargs: dict = {'help': _escape_help(flag.help), 'dest': flag.dest}
    if flag.action:
        kwargs['action'] = flag.action
    if flag.action != 'store_true':
        # A flag that takes a value needs a metavar whether or not it appends.
        kwargs['metavar'] = flag.metavar or flag.dest.upper()
    if flag.choices:
        kwargs['choices'] = list(flag.choices)
    if flag.type is not None:
        kwargs['type'] = flag.type
    if suppress_default or flag.config_key:
        # A flag with a config default must be able to tell "not given" from
        # "given the built-in default", so it is filled in afterwards rather
        # than by argparse. See `_resolve_defaults`.
        kwargs['default'] = argparse.SUPPRESS
    elif flag.action != 'store_true':
        kwargs['default'] = flag.default
    parser.add_argument(*names, **kwargs)


def _escape_help(text: str) -> str:
    """Escape a help string for argparse, which %-formats them.

    A help line naming a `%token%` pattern — which several of these do — made
    argparse raise on `--help` trying to read `%t` as a format specifier. Done
    here, at the boundary, so `schema` still prints the readable text and no
    author has to remember.
    """
    return (text or '').replace('%', '%%')


def _globals_parser() -> argparse.ArgumentParser:
    """A parent parser holding the global flags, shared by every subparser."""
    parent = argparse.ArgumentParser(add_help=False)
    for flag in GLOBAL_FLAGS:
        _add_flag(parent, flag, suppress_default=True)
    return parent


def _apply_global_defaults(args: argparse.Namespace) -> argparse.Namespace:
    """Fill in the global flags nobody passed (see `_add_flag`)."""
    for flag in GLOBAL_FLAGS:
        if not hasattr(args, flag.dest):
            setattr(args, flag.dest,
                    False if flag.action == 'store_true' else flag.default)
    return args


def _resolve_defaults(args: argparse.Namespace, cmd: Cmd | None,
                      config: dict) -> argparse.Namespace:
    """Fill a command's unset flags from the config, then from the built-in.

    The precedence the CLI promises, in one place: a flag on the command line
    beats the config file, and the config file beats the value compiled in. A
    flag only takes part when it names a `config_key`.
    """
    for flag in getattr(cmd, 'flags', ()) or ():
        if hasattr(args, flag.dest):
            continue                          # given on the command line
        value = config.get(flag.config_key) if flag.config_key else None
        # An empty string, a zero or a missing key all mean "no preference" —
        # these keys ship falsy precisely so an untouched config changes nothing,
        # and a limit of 0 would otherwise silently show nothing.
        if not value:
            value = flag.default
        elif flag.type is not None:
            try:
                value = flag.type(value)
            except (TypeError, ValueError):
                value = flag.default          # a bad config value is not fatal
        setattr(args, flag.dest, value)
    return args


def _epilog(cmd: Cmd) -> str:
    """The worked example shown under a command's help."""
    return f"example:\n  {cmd.example}" if cmd.example else ''


def _build(cmd: Cmd, parser: argparse.ArgumentParser, parent) -> None:
    """Attach one command's arguments — and its children — to `parser`."""
    for flag in cmd.flags:
        _add_flag(parser, flag)
    for arg in cmd.args:
        kwargs: dict = {'help': _escape_help(arg.help)}
        if arg.nargs is not None:
            kwargs['nargs'] = arg.nargs
        if arg.choices:
            kwargs['choices'] = list(arg.choices)
        if arg.type is not None:
            kwargs['type'] = arg.type
        parser.add_argument(arg.name, **kwargs)
    if cmd.is_group:
        subs = parser.add_subparsers(dest=f'_{cmd.name}_cmd', metavar='<command>')
        for child in cmd.children:
            sub = subs.add_parser(
                child.name, help=child.help, description=child.help,
                epilog=_epilog(child), parents=[parent],
                formatter_class=argparse.RawDescriptionHelpFormatter)
            sub.set_defaults(_run=child.run, _cmd=child)
            _build(child, sub, parent)
    elif cmd.run is not None:
        parser.set_defaults(_run=cmd.run, _cmd=cmd)


def build_parser(tree: list) -> argparse.ArgumentParser:
    """The whole CLI, built from the command tree."""
    parent = _globals_parser()
    parser = argparse.ArgumentParser(
        prog='backtrack',
        description='Terminal music player and tag editor.\n'
                    'Run with no arguments to open the app.',
        epilog='Run `backtrack <command> --help` for a command, or\n'
               '`backtrack schema` for the whole tree as JSON.',
        parents=[parent],
        formatter_class=argparse.RawDescriptionHelpFormatter)
    subs = parser.add_subparsers(dest='_cmd_name', metavar='<command>')
    for cmd in tree:
        sub = subs.add_parser(
            cmd.name, help=cmd.help, description=cmd.help, epilog=_epilog(cmd),
            parents=[parent],
            formatter_class=argparse.RawDescriptionHelpFormatter)
        sub.set_defaults(_run=cmd.run, _cmd=cmd)
        _build(cmd, sub, parent)
    return parser


# --- schema and completion, from the same tree ------------------------------

def schema_tree(tree: list) -> dict:
    """The command tree as JSON-able data — what `backtrack schema` prints.

    Generated rather than written down, so it cannot describe a CLI that no
    longer exists.
    """
    def _flag(f: Flag) -> dict:
        """One flag's description."""
        d: dict = {'name': f.name, 'help': f.help,
                   'takes_value': f.action != 'store_true'}
        if f.short:
            d['short'] = f.short
        if f.choices:
            d['choices'] = list(f.choices)
        if f.default is not None:
            d['default'] = f.default
        return d

    def _cmd(c: Cmd) -> dict:
        """One command's description, recursing into its children."""
        d: dict = {'name': c.name, 'help': c.help}
        if c.example:
            d['example'] = c.example
        if c.args:
            d['arguments'] = [
                {'name': a.name, 'help': a.help,
                 'repeatable': a.nargs in ('*', '+'),
                 **({'choices': list(a.choices)} if a.choices else {})}
                for a in c.args]
        if c.flags:
            d['flags'] = [_flag(f) for f in c.flags]
        if c.emits:
            d['emits'] = c.emits
        if c.children:
            d['commands'] = [_cmd(x) for x in c.children]
        return d

    return {
        'schema': out.SCHEMA_VERSION,
        'kind': 'schema',
        'program': 'backtrack',
        'global_flags': [_flag(f) for f in GLOBAL_FLAGS],
        'exit_codes': {out.code_name(c): c for c in
                       (out.OK, out.FAIL, out.USAGE, out.NOT_FOUND,
                        out.EXISTS, out.NO_TOOL)},
        'commands': [_cmd(c) for c in tree],
    }


def _paths(tree: list) -> list[tuple[str, list]]:
    """Every reachable command path with the flags valid there.

    `[('library scan', [...]), ('library list', [...]), …]` — flat, which is the
    shape all three shells want.
    """
    out_paths: list[tuple[str, list]] = []

    def walk(cmds: list, prefix: str) -> None:
        """Collect one level, then recurse."""
        for c in cmds:
            path = f"{prefix} {c.name}".strip()
            out_paths.append((path, list(c.flags) + GLOBAL_FLAGS))
            walk(c.children, path)

    walk(tree, '')
    return out_paths


def completion(shell: str, tree: list) -> str:
    """A completion script for bash, zsh or fish, generated from the tree."""
    paths = _paths(tree)
    tops = [c.name for c in tree]

    if shell == 'fish':
        lines = ["# backtrack completions — generated by `backtrack completion fish`",
                 "complete -c backtrack -f"]
        for cmd in tree:
            lines.append(f"complete -c backtrack -n '__fish_use_subcommand' "
                         f"-a {cmd.name} -d {_q(cmd.help)}")
            for child in cmd.children:
                lines.append(f"complete -c backtrack -n '__fish_seen_subcommand_from "
                             f"{cmd.name}' -a {child.name} -d {_q(child.help)}")
        for flag in GLOBAL_FLAGS:
            short = f" -s {flag.short.lstrip('-')}" if flag.short else ""
            lines.append(f"complete -c backtrack -l {flag.name.lstrip('-')}"
                         f"{short} -d {_q(flag.help)}")
        return "\n".join(lines) + "\n"

    if shell == 'zsh':
        lines = ["#compdef backtrack",
                 "# generated by `backtrack completion zsh`",
                 "_backtrack() {",
                 "  local -a cmds",
                 "  cmds=("]
        for cmd in tree:
            lines.append(f"    {cmd.name}:{_zq(cmd.help)}")
        lines += ["  )", "  local -a subs", "  case ${words[2]} in"]
        for cmd in tree:
            if cmd.children:
                inner = ' '.join(f"{c.name}:{_zq(c.help)}" for c in cmd.children)
                lines.append(f"    {cmd.name}) subs=({inner});;")
        flag_list = ' '.join(f.name for f in GLOBAL_FLAGS)
        lines += ["  esac",
                  "  if (( CURRENT == 2 )); then",
                  "    _describe 'command' cmds",
                  "  elif (( ${#subs} )); then",
                  "    _describe 'subcommand' subs",
                  "  fi",
                  f"  _values 'option' {flag_list}",
                  "}",
                  "compdef _backtrack backtrack"]
        return "\n".join(lines) + "\n"

    # bash
    lines = ["# backtrack completions — generated by `backtrack completion bash`",
             "_backtrack() {",
             "  local cur prev",
             '  cur="${COMP_WORDS[COMP_CWORD]}"',
             '  prev="${COMP_WORDS[COMP_CWORD-1]}"',
             f'  local tops="{" ".join(tops)}"',
             f'  local globals="{" ".join(f.name for f in GLOBAL_FLAGS)}"']
    lines.append('  case "$prev" in')
    for cmd in tree:
        if cmd.children:
            kids = ' '.join(c.name for c in cmd.children)
            lines.append(f'    {cmd.name}) COMPREPLY=($(compgen -W "{kids}" -- "$cur")); return;;')
    lines += ['  esac',
              '  if [[ "$cur" == -* ]]; then',
              '    COMPREPLY=($(compgen -W "$globals" -- "$cur")); return',
              '  fi',
              '  if (( COMP_CWORD == 1 )); then',
              '    COMPREPLY=($(compgen -W "$tops" -- "$cur")); return',
              '  fi',
              '  COMPREPLY=($(compgen -f -- "$cur"))',
              '}',
              'complete -F _backtrack backtrack']
    return "\n".join(lines) + "\n"


def _q(text: str) -> str:
    """Single-quote a string for fish."""
    return "'" + text.replace("'", r"\'") + "'"


def _zq(text: str) -> str:
    """Escape a description for a zsh `_describe` array entry."""
    return text.replace(':', ' -').replace("'", '')


# --- entry point ------------------------------------------------------------

def main(argv: list) -> int:
    """Run one CLI invocation. Returns the process exit code."""
    from src.cli_commands import TREE

    parser = build_parser(TREE)
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:                 # argparse's own --help / usage
        return int(exc.code or 0)
    except Exception as exc:                  # a malformed definition or input
        return out.fail(out.USAGE, f"Could not read those arguments: {exc}")
    args = _apply_global_defaults(args)

    out.configure(json_mode=args.json, quiet=args.quiet,
                  colour=False if args.no_colour else None)

    ctx = Ctx(args)
    _resolve_defaults(args, getattr(args, '_cmd', None), ctx.config
                      if getattr(args, '_cmd', None) else {})

    run = getattr(args, '_run', None)
    if run is None:
        # A group with no verb: show that group's help rather than a bare error.
        # argparse exits from inside --help, which must not escape as a traceback.
        try:
            parser.parse_args(argv + ['--help'])
        except SystemExit:
            pass
        return out.USAGE

    try:
        code = int(run(ctx) or out.OK)
        if args.dry_run and code == out.OK:
            # Said once, centrally, so no handler has to remember to — and so a
            # planned-state table can't read as a done deal. Only on success:
            # after a usage error nothing was planned either.
            out.note("Dry run — nothing was written.")
        return code
    except KeyboardInterrupt:
        return out.fail(out.FAIL, "Interrupted.")
    except BrokenPipeError:
        # `backtrack track list | head` — the reader left, which is not an error.
        try:
            sys.stdout.close()
        except Exception:
            pass
        return out.OK
    except Exception as exc:
        return out.fail(out.FAIL, str(exc) or exc.__class__.__name__,
                        type=exc.__class__.__name__)
