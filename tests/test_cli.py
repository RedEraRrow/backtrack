"""Headless tests for the command-line interface.

Every command is exercised in both output modes, every exit code is produced by
a real invocation rather than asserted about in the abstract, and the two rules
that are easy to regress — dry-run writes nothing, colour never reaches a pipe —
get their own cases.

`cli.main` is called in process with stdout/stderr captured, so a test sees
exactly the bytes a shell would.

**Isolation.** Several modules bind their paths at import — `config.CONFIG_FILE`,
`music_library.CACHE_PATH` and, the one that bites, `history.HISTORY_FILE`,
which captures `config.CONFIG_DIR` the moment it is imported and so ignores a
later patch of it. Every such constant is redirected in `setUp`, and
`_assert_isolated` refuses to run a test whose paths still point anywhere but
the temp directory: these tests invoke destructive commands, and a patch that
silently missed one deleted a real history log exactly once.
"""
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from mutagen.id3 import ID3, TALB, TCON, TIT2, TPE1, TPOS, TRCK  # type: ignore[reportPrivateImportUsage]

from src import cli
from src.utils import output as out


def _mp3(path: str, title: str, artist: str, album: str, track: str,
         genre: str = "Pop", disc: str | None = None) -> str:
    """A tag-only MP3 the library scanner will pick up."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    open(path, "wb").close()
    audio = ID3()
    audio.add(TIT2(encoding=3, text=[title]))
    audio.add(TPE1(encoding=3, text=[artist]))
    audio.add(TALB(encoding=3, text=[album]))
    audio.add(TRCK(encoding=3, text=[track]))
    audio.add(TCON(encoding=3, text=[genre]))
    if disc:
        audio.add(TPOS(encoding=3, text=[disc]))
    audio.save(path, v2_version=3)
    return path


class CliTest(unittest.TestCase):
    """A CLI test with its own config, cache and music directory."""

    def setUp(self):
        from src import config as cfg
        from src import history as hist
        from src import music_library as ml

        self.tmp = tempfile.mkdtemp()
        self.music = os.path.join(self.tmp, "music")
        self.stdin = sys.stdin
        self._saved = (cfg.CONFIG_DIR, cfg.CONFIG_FILE, ml.CACHE_DIR,
                       ml.CACHE_PATH, hist.HISTORY_FILE)
        cfg.CONFIG_DIR = Path(self.tmp) / "config"
        cfg.CONFIG_FILE = cfg.CONFIG_DIR / "config.json"
        ml.CACHE_DIR = Path(self.tmp) / "cache"
        ml.CACHE_PATH = ml.CACHE_DIR / "library_cache.json"
        hist.HISTORY_FILE = cfg.CONFIG_DIR / "history.log"
        cfg.CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        ml.CACHE_DIR.mkdir(parents=True, exist_ok=True)
        self._assert_isolated()

        self.tracks = [
            _mp3(f"{self.music}/Duran Duran/Rio/01 Rio.mp3",
                 "Rio", "Duran Duran", "Rio", "1/2", "New Wave", "1/1"),
            _mp3(f"{self.music}/Duran Duran/Rio/02 Hungry Like the Wolf.mp3",
                 "Hungry Like the Wolf", "Duran Duran", "Rio", "2/2", "New Wave", "1/1"),
            _mp3(f"{self.music}/Darude/Before the Storm/01 Sandstorm.mp3",
                 "Sandstorm", "Darude", "Before the Storm", "1/1", "Trance"),
        ]

    def _assert_isolated(self):
        """Refuse to run unless every writable path is inside the temp directory.

        A missed patch means a destructive command runs against the real config
        directory. Failing loudly here costs one assertion; not failing here cost
        a real listening-history log.
        """
        from src import config as cfg
        from src import history as hist
        from src import music_library as ml

        for name, path in (('config.CONFIG_DIR', cfg.CONFIG_DIR),
                           ('config.CONFIG_FILE', cfg.CONFIG_FILE),
                           ('music_library.CACHE_DIR', ml.CACHE_DIR),
                           ('music_library.CACHE_PATH', ml.CACHE_PATH),
                           ('history.HISTORY_FILE', hist.HISTORY_FILE)):
            self.assertTrue(
                str(path).startswith(self.tmp),
                f"{name} is {path}, outside the test directory — refusing to run")

    def tearDown(self):
        from src import config as cfg
        from src import history as hist
        from src import music_library as ml
        (cfg.CONFIG_DIR, cfg.CONFIG_FILE, ml.CACHE_DIR, ml.CACHE_PATH,
         hist.HISTORY_FILE) = self._saved
        sys.stdin = self.stdin
        out.configure()
        shutil.rmtree(self.tmp, ignore_errors=True)

    # -- running ------------------------------------------------------------

    def run_cli(self, *argv, stdin: str | None = None):
        """Run one invocation; returns `(code, stdout, stderr)`."""
        if stdin is not None:
            sys.stdin = io.StringIO(stdin)
            sys.stdin.isatty = lambda: False       # type: ignore[method-assign]
        stdout, stderr = io.StringIO(), io.StringIO()
        # Nothing here is a terminal, which is the mode an agent runs in.
        stdout.isatty = lambda: False              # type: ignore[method-assign]
        stderr.isatty = lambda: False              # type: ignore[method-assign]
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = cli.main(list(argv))
        return code, stdout.getvalue(), stderr.getvalue()

    def scan(self):
        """Populate the library — most tests need one."""
        return self.run_cli('library', 'dirs', '--add', self.music)[0], \
            self.run_cli('library', 'scan')[0]

    def json_of(self, *argv):
        """Run with --json and parse the single object it prints."""
        code, stdout, stderr = self.run_cli(*argv, '--json')
        payload = stdout.strip() or stderr.strip()
        return code, json.loads(payload.splitlines()[-1])


class LibraryCommandsTest(CliTest):
    def test_scan_reports_what_it_found(self):
        self.run_cli('library', 'dirs', '--add', self.music)
        code, stdout, _err = self.run_cli('library', 'scan')
        self.assertEqual(code, out.OK)
        self.assertIn("3 tracks", stdout)

    def test_scan_json_carries_the_schema_version(self):
        self.run_cli('library', 'dirs', '--add', self.music)
        code, body = self.json_of('library', 'scan')
        self.assertEqual(code, out.OK)
        self.assertEqual(body['schema'], out.SCHEMA_VERSION)
        self.assertEqual(body['tracks'], 3)

    def test_scan_without_a_directory_is_a_usage_error(self):
        code, _out, err = self.run_cli('library', 'scan')
        self.assertEqual(code, out.USAGE)
        self.assertIn("No music directory", err)

    def test_scan_of_a_missing_directory_is_not_found(self):
        code, _out, _err = self.run_cli('library', 'scan',
                                        '--library', os.path.join(self.tmp, 'nope'))
        self.assertEqual(code, out.NOT_FOUND)

    def test_list_groups_by_artist_by_default(self):
        self.scan()
        code, stdout, _err = self.run_cli('library', 'list')
        self.assertEqual(code, out.OK)
        self.assertEqual(stdout.split(), ['Darude', 'Duran', 'Duran'])

    def test_list_can_group_by_album(self):
        self.scan()
        _code, body = self.json_of('library', 'list', '--by', 'album')
        self.assertEqual({i['name'] for i in body['items']},
                         {'Rio', 'Before the Storm'})

    def test_stat_counts_every_axis(self):
        self.scan()
        _code, body = self.json_of('library', 'stat')
        self.assertEqual((body['tracks'], body['artists'], body['albums'],
                          body['genres']), (3, 2, 2, 2))

    def test_verify_is_quiet_when_every_file_is_present(self):
        self.scan()
        code, stdout, _err = self.run_cli('library', 'verify')
        self.assertEqual(code, out.OK)
        self.assertIn("all present", stdout)

    def test_verify_fails_and_names_a_missing_file(self):
        self.scan()
        os.remove(self.tracks[0])
        code, stdout, _err = self.run_cli('library', 'verify')
        self.assertEqual(code, out.FAIL)
        self.assertIn(self.tracks[0], stdout)

    def test_dirs_add_then_remove(self):
        self.assertEqual(self.run_cli('library', 'dirs', '--add', self.music)[0],
                         out.OK)
        self.assertEqual(self.run_cli('library', 'dirs', '--remove', self.music)[0],
                         out.OK)
        _code, body = self.json_of('library', 'dirs')
        self.assertEqual(body['items'], [])

    def test_adding_the_same_directory_twice_reports_exists(self):
        self.run_cli('library', 'dirs', '--add', self.music)
        code, _out, _err = self.run_cli('library', 'dirs', '--add', self.music)
        self.assertEqual(code, out.EXISTS)

    def test_removing_a_directory_that_is_not_there_is_not_found(self):
        code, _out, _err = self.run_cli('library', 'dirs', '--remove', self.music)
        self.assertEqual(code, out.NOT_FOUND)


class TrackCommandsTest(CliTest):
    def test_list_prints_one_path_per_line_when_piped(self):
        self.scan()
        _code, stdout, _err = self.run_cli('track', 'list')
        self.assertEqual(sorted(stdout.splitlines()), sorted(self.tracks))

    def test_list_filters_by_artist(self):
        self.scan()
        _code, body = self.json_of('track', 'list', '--artist', 'darude')
        self.assertEqual([i['title'] for i in body['items']], ['Sandstorm'])

    def test_list_filters_by_album_and_genre(self):
        self.scan()
        _code, body = self.json_of('track', 'list', '--album', 'rio')
        self.assertEqual(len(body['items']), 2)
        _code, body = self.json_of('track', 'list', '--genre', 'trance')
        self.assertEqual(len(body['items']), 1)

    def test_limit_caps_the_list(self):
        self.scan()
        _code, body = self.json_of('track', 'list', '--limit', '2')
        self.assertEqual(len(body['items']), 2)

    def test_show_reads_a_path_from_stdin(self):
        self.scan()
        code, stdout, _err = self.run_cli('track', 'show', '--json',
                                          stdin=self.tracks[2] + "\n")
        self.assertEqual(code, out.OK)
        self.assertEqual(json.loads(stdout.strip())['title'], 'Sandstorm')

    def test_show_of_a_missing_file_is_not_found(self):
        self.scan()
        code, _out, _err = self.run_cli('track', 'show',
                                        os.path.join(self.tmp, 'ghost.mp3'))
        self.assertEqual(code, out.NOT_FOUND)

    def test_show_with_nothing_to_act_on_is_a_usage_error(self):
        self.scan()
        code, _out, _err = self.run_cli('track', 'show', stdin="")
        self.assertEqual(code, out.USAGE)


class SearchTest(CliTest):
    def test_finds_a_track(self):
        self.scan()
        _code, body = self.json_of('search', 'wolf')
        self.assertEqual([i['title'] for i in body['items']],
                         ['Hungry Like the Wolf'])

    def test_a_miss_is_not_found(self):
        self.scan()
        code, _out, _err = self.run_cli('search', 'zzzznothing')
        self.assertEqual(code, out.NOT_FOUND)

    def test_scope_narrows_the_fields_searched(self):
        self.scan()
        code, _out, _err = self.run_cli('search', 'darude', '--scope', 'title')
        self.assertEqual(code, out.NOT_FOUND)
        code, _out, _err = self.run_cli('search', 'darude', '--scope', 'artist')
        self.assertEqual(code, out.OK)


class ConfigTest(CliTest):
    def test_get_and_set_a_boolean(self):
        self.assertEqual(self.run_cli('config', 'set', 'autoplay_on_select',
                                      'true')[0], out.OK)
        _code, body = self.json_of('config', 'get', 'autoplay_on_select')
        self.assertIs(body['value'], True)

    def test_set_coerces_to_the_declared_type(self):
        self.run_cli('config', 'set', 'lyric_lead_in', '3.5')
        _code, body = self.json_of('config', 'get', 'lyric_lead_in')
        self.assertEqual(body['value'], 3.5)

    def test_a_value_of_the_wrong_type_is_a_usage_error(self):
        code, _out, err = self.run_cli('config', 'set', 'lyric_lead_in', 'soon')
        self.assertEqual(code, out.USAGE)
        self.assertIn("number", err)

    def test_an_unknown_key_is_not_found_for_both_get_and_set(self):
        self.assertEqual(self.run_cli('config', 'get', 'nope')[0], out.NOT_FOUND)
        self.assertEqual(self.run_cli('config', 'set', 'nope', '1')[0],
                         out.NOT_FOUND)

    def test_list_shows_every_key(self):
        from src.config import DEFAULT_CONFIG
        _code, body = self.json_of('config', 'list')
        for key in DEFAULT_CONFIG:
            self.assertIn(key, body)


class HistoryTest(CliTest):
    def test_clearing_an_empty_log_is_fine(self):
        code, stdout, _err = self.run_cli('history', 'clear', '--yes')
        self.assertEqual(code, out.OK)
        self.assertIn("already empty", stdout)

    def test_list_is_empty_to_begin_with(self):
        _code, body = self.json_of('history', 'list')
        self.assertEqual(body['items'], [])

    def test_clear_removes_logged_plays(self):
        from src import history
        history.log_listening_history(self.tracks[0], 0.0, 30.0)
        _code, body = self.json_of('history', 'list')
        self.assertEqual(len(body['items']), 1)
        self.assertEqual(self.run_cli('history', 'clear', '--yes')[0], out.OK)
        _code, body = self.json_of('history', 'list')
        self.assertEqual(body['items'], [])

    def test_clear_does_not_prompt_when_stdin_is_not_a_terminal(self):
        from src import history
        history.log_listening_history(self.tracks[0], 0.0, 30.0)
        # No --yes, nothing on stdin: it must decline rather than block.
        code, stdout, _err = self.run_cli('history', 'clear', stdin="")
        self.assertEqual(code, out.OK)
        self.assertIn("Left alone", stdout)
        self.assertEqual(len(self.json_of('history', 'list')[1]['items']), 1)


class DryRunTest(CliTest):
    def test_dry_run_does_not_change_the_config(self):
        code, _out, _err = self.run_cli('library', 'dirs', '--add', self.music,
                                        '--dry-run')
        self.assertEqual(code, out.OK)
        _code, body = self.json_of('config', 'get', 'music_directories')
        self.assertEqual(body['value'], [])

    def test_dry_run_does_not_write_the_cache(self):
        from src import music_library as ml
        self.run_cli('library', 'dirs', '--add', self.music)
        self.run_cli('library', 'scan', '--dry-run')
        self.assertFalse(ml.CACHE_PATH.exists())

    def test_dry_run_does_not_clear_the_history(self):
        from src import history
        history.log_listening_history(self.tracks[0], 0.0, 30.0)
        self.run_cli('history', 'clear', '--dry-run')
        self.assertEqual(len(self.json_of('history', 'list')[1]['items']), 1)

    def test_dry_run_says_so_in_human_mode(self):
        _code, stdout, _err = self.run_cli('library', 'dirs', '--add',
                                           self.music, '--dry-run')
        self.assertIn("nothing was written", stdout)

    def test_dry_run_emits_a_plan_event_under_json(self):
        self.run_cli('library', 'dirs', '--add', self.music)
        _code, body = self.json_of('library', 'scan', '--dry-run')
        self.assertEqual(body['kind'], 'event')
        self.assertEqual(body['event'], 'plan')


class OutputModeTest(CliTest):
    def test_no_escape_codes_reach_a_pipe(self):
        self.scan()
        for argv in (('track', 'list'), ('library', 'list'), ('library', 'stat'),
                     ('config', 'list'), ('history', 'list')):
            with self.subTest(argv=argv):
                _code, stdout, _err = self.run_cli(*argv)
                self.assertNotIn('\033', stdout)

    def test_no_escape_codes_in_a_piped_error(self):
        _code, _out, err = self.run_cli('config', 'get', 'nope')
        self.assertNotIn('\033', err)

    def test_no_colour_flag_strips_styling_on_a_terminal_too(self):
        from src.utils import ui_utils
        out.configure(colour=True)
        self.assertNotEqual(ui_utils.Colors.DIM, '')
        self.run_cli('config', 'list', '--no-colour')
        self.assertEqual(ui_utils.Colors.DIM, '')

    def test_no_color_environment_variable_is_honoured(self):
        from src.utils import ui_utils
        saved = os.environ.get('NO_COLOR')
        os.environ['NO_COLOR'] = '1'
        try:
            self.assertFalse(ui_utils.colour_enabled())
        finally:
            if saved is None:
                os.environ.pop('NO_COLOR', None)
            else:
                os.environ['NO_COLOR'] = saved

    def test_cursor_control_survives_colour_being_switched_off(self):
        from src.utils import ui_utils
        ui_utils.set_colour(False)
        self.assertEqual(ui_utils.Colors.DIM, '')
        self.assertNotEqual(ui_utils.Colors.HIDE, '')
        ui_utils.set_colour(True)

    def test_quiet_prints_nothing_but_still_reports(self):
        self.scan()
        code, stdout, _err = self.run_cli('track', 'list', '--quiet')
        self.assertEqual(code, out.OK)
        self.assertEqual(stdout, '')

    def test_every_json_object_carries_a_schema_version(self):
        self.scan()
        for argv in (('track', 'list'), ('library', 'stat'), ('library', 'list'),
                     ('history', 'list'), ('config', 'list'),
                     ('config', 'get', 'debug')):
            with self.subTest(argv=argv):
                _code, body = self.json_of(*argv)
                self.assertEqual(body['schema'], out.SCHEMA_VERSION)

    def test_a_json_error_has_the_documented_shape(self):
        _code, body = self.json_of('config', 'get', 'nope')
        self.assertEqual(set(body['error']), {'code', 'message', 'context'})
        self.assertEqual(body['error']['code'], 'not_found')

    def test_global_flags_work_on_either_side_of_the_verb(self):
        self.scan()
        before = self.run_cli('--json', 'library', 'stat')[1]
        after = self.run_cli('library', 'stat', '--json')[1]
        self.assertEqual(before, after)
        self.assertEqual(json.loads(before)['kind'], 'stat')


class UsageTest(CliTest):
    def test_an_unknown_command_is_a_usage_error(self):
        code, _out, _err = self.run_cli('nosuchthing')
        self.assertEqual(code, out.USAGE)

    def test_an_invalid_choice_is_a_usage_error(self):
        code, _out, _err = self.run_cli('library', 'list', '--by', 'nope')
        self.assertEqual(code, out.USAGE)

    def test_a_group_with_no_verb_shows_its_help(self):
        code, stdout, _err = self.run_cli('library')
        self.assertIn('scan', stdout)
        self.assertNotEqual(code, out.OK)

    def test_help_exits_zero(self):
        code, stdout, _err = self.run_cli('--help')
        self.assertEqual(code, out.OK)
        self.assertIn('library', stdout)

    def test_every_leaf_command_has_a_worked_example(self):
        from src.cli_commands import TREE

        def leaves(cmds, prefix=''):
            """Every runnable command and the path that reaches it."""
            for cmd in cmds:
                path = f"{prefix} {cmd.name}".strip()
                if cmd.children:
                    yield from leaves(cmd.children, path)
                else:
                    yield path, cmd

        for path, cmd in leaves(TREE):
            with self.subTest(command=path):
                self.assertTrue(cmd.example, f"{path} has no example")
                self.assertIn('backtrack', cmd.example)

    def test_every_command_offers_help(self):
        from src.cli_commands import TREE
        for cmd in TREE:
            with self.subTest(command=cmd.name):
                code, stdout, _err = self.run_cli(cmd.name, '--help')
                self.assertEqual(code, out.OK)
                self.assertIn(cmd.help.split()[0].lower(), stdout.lower())


class SchemaAndCompletionTest(CliTest):
    def test_schema_describes_the_real_tree(self):
        from src.cli_commands import TREE
        _code, stdout, _err = self.run_cli('schema', '--json')
        body = json.loads(stdout)
        self.assertEqual([c['name'] for c in body['commands']],
                         [c.name for c in TREE])

    def test_schema_lists_every_exit_code(self):
        _code, stdout, _err = self.run_cli('schema', '--json')
        body = json.loads(stdout)
        self.assertEqual(body['exit_codes'],
                         {'ok': 0, 'failed': 1, 'usage': 2, 'not_found': 3,
                          'exists': 4, 'missing_tool': 5})

    def test_schema_carries_the_global_flags(self):
        _code, stdout, _err = self.run_cli('schema', '--json')
        body = json.loads(stdout)
        names = {f['name'] for f in body['global_flags']}
        self.assertIn('--json', names)
        self.assertIn('--dry-run', names)

    def test_schema_is_pretty_printed_for_a_human(self):
        _code, stdout, _err = self.run_cli('schema')
        self.assertIn('\n  ', stdout)
        json.loads(stdout)                      # still valid either way

    def test_completions_mention_every_top_level_command(self):
        from src.cli_commands import TREE
        for shell in ('bash', 'zsh', 'fish'):
            with self.subTest(shell=shell):
                code, stdout, _err = self.run_cli('completion', shell)
                self.assertEqual(code, out.OK)
                for cmd in TREE:
                    self.assertIn(cmd.name, stdout)

    def test_completions_mention_the_subcommands(self):
        code, stdout, _err = self.run_cli('completion', 'bash')
        self.assertEqual(code, out.OK)
        for verb in ('scan', 'stat', 'verify', 'dirs'):
            self.assertIn(verb, stdout)

    def test_an_unknown_shell_is_a_usage_error(self):
        code, _out, _err = self.run_cli('completion', 'nushell')
        self.assertEqual(code, out.USAGE)


if __name__ == "__main__":
    unittest.main()
