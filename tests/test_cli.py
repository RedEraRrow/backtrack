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


class TagCommandsTest(CliTest):
    def setUp(self):
        super().setUp()
        self.scan()
        self.track = self.tracks[2]                # Sandstorm, on its own

    def test_read_lists_the_frames(self):
        _code, body = self.json_of('tag', 'read', self.track)
        self.assertEqual({i['tag'] for i in body['items']},
                         {'TIT2', 'TPE1', 'TALB', 'TRCK', 'TCON'})

    def test_read_can_narrow_to_one_frame(self):
        _code, body = self.json_of('tag', 'read', self.track, '--tag', 'TIT2')
        self.assertEqual([i['value'] for i in body['items']], ['Sandstorm'])

    def test_read_carries_the_friendly_name(self):
        _code, body = self.json_of('tag', 'read', self.track, '--tag', 'TALB')
        self.assertEqual(body['items'][0]['name'], 'Album')

    def test_read_of_a_missing_file_is_not_found(self):
        code, _out, _err = self.run_cli('tag', 'read',
                                        os.path.join(self.tmp, 'ghost.mp3'))
        self.assertEqual(code, out.NOT_FOUND)

    def test_read_with_no_target_is_a_usage_error(self):
        code, _out, _err = self.run_cli('tag', 'read', stdin="")
        self.assertEqual(code, out.USAGE)

    def test_write_sets_a_value(self):
        self.assertEqual(self.run_cli('tag', 'write', self.track,
                                      '-t', 'TCON', '-v', 'Hardcore')[0], out.OK)
        self.assertEqual(ID3(self.track)['TCON'].text[0], 'Hardcore')

    def test_write_reaches_every_file_a_filter_matches(self):
        self.run_cli('tag', 'write', '--album', 'rio', '-t', 'TCON',
                     '-v', 'Synthpop')
        for path in self.tracks[:2]:
            self.assertEqual(ID3(path)['TCON'].text[0], 'Synthpop')

    def test_delete_removes_a_frame(self):
        self.assertEqual(self.run_cli('tag', 'delete', self.track,
                                      '-t', 'TCON', '--yes')[0], out.OK)
        self.assertNotIn('TCON', ID3(self.track))

    def test_delete_does_not_prompt_or_act_without_yes_on_a_pipe(self):
        code, stdout, _err = self.run_cli('tag', 'delete', self.track,
                                          '-t', 'TCON', stdin="")
        self.assertEqual(code, out.OK)
        self.assertIn("Left alone", stdout)
        self.assertIn('TCON', ID3(self.track))

    def test_rename_moves_the_value_to_a_new_frame(self):
        self.run_cli('tag', 'write', self.track, '-t', 'TIT1', '-v', 'Grouping')
        self.assertEqual(self.run_cli('tag', 'rename', self.track, '-t', 'TIT1',
                                      '--to', 'TSST')[0], out.OK)
        self.assertEqual(ID3(self.track)['TSST'].text[0], 'Grouping')
        self.assertNotIn('TIT1', ID3(self.track))

    def test_copy_moves_chosen_frames_between_files(self):
        code, _out, _err = self.run_cli('tag', 'copy', self.track,
                                        self.tracks[0], '-t', 'TCON')
        self.assertEqual(code, out.OK)
        self.assertEqual(ID3(self.tracks[0])['TCON'].text[0], 'Trance')

    def test_copy_from_a_missing_source_is_not_found(self):
        code, _out, _err = self.run_cli('tag', 'copy',
                                        os.path.join(self.tmp, 'ghost.mp3'),
                                        self.tracks[0])
        self.assertEqual(code, out.NOT_FOUND)

    def test_copy_of_a_tag_the_source_lacks_is_not_found(self):
        code, _out, _err = self.run_cli('tag', 'copy', self.track,
                                        self.tracks[0], '-t', 'TBPM')
        self.assertEqual(code, out.NOT_FOUND)

    def test_dry_run_writes_no_tag(self):
        self.run_cli('tag', 'write', self.track, '-t', 'TCON', '-v', 'Nope',
                     '--dry-run')
        self.assertEqual(ID3(self.track)['TCON'].text[0], 'Trance')


class BulkCommandsTest(CliTest):
    def setUp(self):
        super().setUp()
        self.scan()

    def test_stripdisc_removes_one_of_one(self):
        from src.id3 import tag_writer as tw
        code, _out, _err = self.run_cli('bulk', 'stripdisc', '--album', 'rio',
                                        '--yes')
        self.assertEqual(code, out.OK)
        self.assertEqual(tw.read_number_pairs(self.tracks[0])['disc'], '')

    def test_stripdisc_dry_run_changes_nothing(self):
        from src.id3 import tag_writer as tw
        self.run_cli('bulk', 'stripdisc', '--album', 'rio', '--dry-run')
        self.assertEqual(tw.read_number_pairs(self.tracks[0])['disc'], '1')

    def test_dry_run_and_a_real_run_emit_the_same_event_shape(self):
        planned = [json.loads(line) for line in
                   self.run_cli('bulk', 'stripdisc', '--album', 'rio',
                                '--dry-run', '--json')[1].splitlines()]
        real = [json.loads(line) for line in
                self.run_cli('bulk', 'stripdisc', '--album', 'rio',
                             '--yes', '--json')[1].splitlines()]
        self.assertEqual(len(planned), len(real))
        for a, b in zip(planned, real):
            self.assertEqual(a['kind'], b['kind'])
            self.assertEqual(set(a) - {'fields'}, set(b))
            self.assertEqual(a['detail'], b['detail'])

    def test_events_stream_one_line_per_file(self):
        _code, stdout, _err = self.run_cli('bulk', 'stripdisc', '--album', 'rio',
                                           '--yes', '--json')
        lines = [json.loads(line) for line in stdout.splitlines()]
        self.assertEqual(len(lines), 2)
        self.assertTrue(all(line['kind'] == 'event' for line in lines))
        self.assertTrue(all(line['schema'] == out.SCHEMA_VERSION for line in lines))

    def test_renumber_lays_down_a_continuous_run(self):
        from src.id3 import tag_writer as tw
        self.run_cli('bulk', 'renumber', '--album', 'rio', '--yes')
        numbers = [tw.read_number_pairs(p)['track'] for p in self.tracks[:2]]
        self.assertEqual(numbers, ['1', '2'])

    def test_striplength_removes_a_stale_tlen(self):
        from mutagen.id3 import TLEN  # type: ignore[reportPrivateImportUsage]
        from src.id3 import tag_writer as tw
        audio = ID3(self.tracks[2])
        audio.add(TLEN(encoding=3, text=["123456"]))
        audio.save(self.tracks[2], v2_version=3)
        self.run_cli('bulk', 'striplength', self.tracks[2], '--yes')
        self.assertEqual(tw.stale_length_tags(self.tracks[2]), (False, False))

    def test_sortorders_moves_a_leading_article_to_the_tail(self):
        self.run_cli('tag', 'write', self.tracks[2], '-t', 'TPE1',
                     '-v', 'The Shamen')
        self.run_cli('bulk', 'sortorders', self.tracks[2], '--yes')
        self.assertEqual(ID3(self.tracks[2])['TSOP'].text[0], 'Shamen, The')

    def test_assign_every_n_numbers_the_groups(self):
        self.run_cli('bulk', 'assign', '--album', 'rio', '-t', 'TIT1',
                     '--every', '1', '-v', 'Series {n}', '--yes')
        values = [str(ID3(p)['TIT1'].text[0]) for p in self.tracks[:2]]
        self.assertEqual(values, ['Series 1', 'Series 2'])

    def test_assign_by_range(self):
        self.run_cli('bulk', 'assign', '--album', 'rio', '-t', 'TSST',
                     '--range', '1-2=Part One', '--yes')
        self.assertEqual(str(ID3(self.tracks[0])['TSST'].text[0]), 'Part One')

    def test_assign_needs_exactly_one_mode(self):
        code, _out, _err = self.run_cli('bulk', 'assign', '--album', 'rio',
                                        '-t', 'TIT1', '--yes')
        self.assertEqual(code, out.USAGE)
        code, _out, _err = self.run_cli('bulk', 'assign', '--album', 'rio',
                                        '-t', 'TIT1', '--every', '2',
                                        '--range', '1-2=x', '--yes')
        self.assertEqual(code, out.USAGE)

    def test_assign_rejects_a_malformed_range(self):
        code, _out, _err = self.run_cli('bulk', 'assign', '--album', 'rio',
                                        '-t', 'TIT1', '--range', 'nonsense',
                                        '--yes')
        self.assertEqual(code, out.USAGE)

    def test_rename_renames_from_the_tags(self):
        code, _out, _err = self.run_cli('bulk', 'rename', '--album', 'rio',
                                        '-p', '%track% - %title%', '--yes')
        self.assertEqual(code, out.OK)
        names = sorted(os.listdir(os.path.dirname(self.tracks[0])))
        self.assertEqual(names, ['01 - Rio.mp3', '02 - Hungry Like the Wolf.mp3'])

    def test_rename_rejects_an_unknown_token(self):
        code, _out, err = self.run_cli('bulk', 'rename', '--album', 'rio',
                                       '-p', '%nosuchtoken%', '--yes')
        self.assertEqual(code, out.USAGE)
        self.assertIn('nosuchtoken', err)

    def test_rename_dry_run_leaves_the_names_alone(self):
        before = sorted(os.listdir(os.path.dirname(self.tracks[0])))
        self.run_cli('bulk', 'rename', '--album', 'rio', '-p', '%title%',
                     '--dry-run')
        self.assertEqual(sorted(os.listdir(os.path.dirname(self.tracks[0]))),
                         before)

    def test_derive_fills_a_title_from_the_file_name(self):
        self.run_cli('bulk', 'derive', self.tracks[2], '-f', 'title',
                     '--overwrite', '--yes')
        self.assertEqual(str(ID3(self.tracks[2])['TIT2'].text[0]), 'Sandstorm')

    def test_pictype_reports_when_there_is_no_art(self):
        code, stdout, _err = self.run_cli('bulk', 'pictype', '--album', 'rio',
                                          '--yes')
        self.assertEqual(code, out.OK)
        self.assertIn('No MP3s with embedded art', stdout)

    def test_art_embeds_a_shared_cover(self):
        from src.id3 import tag_writer as tw
        cover = os.path.join(os.path.dirname(self.tracks[0]), 'cover.jpg')
        with open(cover, 'wb') as f:
            f.write(b"\xff\xd8\xff\xe0" + b"\x00" * 32 + b"\xff\xd9")
        code, _out, _err = self.run_cli('bulk', 'art', '--album', 'rio',
                                        '--strategy', 'best', '--yes')
        self.assertEqual(code, out.OK)
        self.assertTrue(tw.has_cover(self.tracks[0]))

    def test_art_template_without_a_pattern_is_a_usage_error(self):
        code, _out, _err = self.run_cli('bulk', 'art', '--album', 'rio',
                                        '--strategy', 'template', '--yes')
        self.assertEqual(code, out.USAGE)

    def test_a_bulk_command_with_no_target_is_a_usage_error(self):
        for verb in ('stripdisc', 'striplength', 'renumber', 'reflow',
                     'sortorders', 'derive'):
            with self.subTest(verb=verb):
                code, _out, _err = self.run_cli('bulk', verb, '--yes', stdin="")
                self.assertEqual(code, out.USAGE)

    def test_a_named_filter_never_reads_stdin(self):
        # Regression: reading stdin whenever the positionals were empty hung any
        # `--album`-scoped command against a stdin that stays open.
        sys.stdin = io.StringIO("")
        sys.stdin.isatty = lambda: False        # type: ignore[method-assign]
        code, _out, _err = self.run_cli('bulk', 'stripdisc', '--album', 'rio',
                                        '--dry-run')
        self.assertEqual(code, out.OK)


class SessionCommandsTest(CliTest):
    """With no session running, every transport command reports that rather
    than hanging or pretending it worked."""

    def setUp(self):
        super().setUp()
        self.scan()

    def test_list_is_empty_and_succeeds(self):
        _code, body = self.json_of('session', 'list')
        self.assertEqual(body['kind'], 'sessions')

    def test_status_with_no_session_is_not_found(self):
        code, _out, _err = self.run_cli('session', 'status')
        self.assertEqual(code, out.NOT_FOUND)

    def test_every_transport_command_reports_no_session(self):
        for verb, extra in (('pause', ()), ('next', ()), ('prev', ()),
                            ('stop', ()), ('seek', ('30',)),
                            ('volume', ('50',))):
            with self.subTest(verb=verb):
                code, _out, _err = self.run_cli('session', verb, *extra)
                self.assertEqual(code, out.NOT_FOUND)

    def test_volume_out_of_range_is_a_usage_error(self):
        code, _out, _err = self.run_cli('session', 'volume', '200')
        self.assertEqual(code, out.USAGE)

    def test_queue_commands_report_no_session(self):
        self.assertEqual(self.run_cli('queue', 'show')[0], out.NOT_FOUND)
        self.assertEqual(self.run_cli('queue', 'add', self.tracks[0])[0],
                         out.NOT_FOUND)

    def test_play_with_nothing_to_play_is_a_usage_error(self):
        code, _out, _err = self.run_cli('play', stdin="")
        self.assertEqual(code, out.USAGE)

    def test_play_dry_run_starts_no_audio(self):
        code, stdout, _err = self.run_cli('play', self.tracks[0], '--dry-run',
                                          '--json')
        self.assertEqual(code, out.OK)
        event = json.loads(stdout.splitlines()[0])
        self.assertEqual(event['event'], 'plan')
        self.assertEqual(event['action'], 'play')


class LyricsCommandsTest(CliTest):
    def setUp(self):
        super().setUp()
        self.scan()
        self.track = self.tracks[2]
        self.lrc = os.path.join(self.tmp, 'lines.lrc')
        with open(self.lrc, 'w', encoding='utf-8') as handle:
            handle.write("[00:12.000]First line\n"
                         "[00:15.500]Second line\n"
                         "[00:19.250]Third line\n")

    def test_show_before_any_import_is_not_found(self):
        code, _out, _err = self.run_cli('lyrics', 'show', self.track)
        self.assertEqual(code, out.NOT_FOUND)

    def test_import_writes_timed_lines_to_sylt(self):
        code, _out, _err = self.run_cli('lyrics', 'import', self.track,
                                        '--from', self.lrc)
        self.assertEqual(code, out.OK)
        _code, body = self.json_of('lyrics', 'show', self.track)
        self.assertEqual(body['source'], 'SYLT')
        self.assertEqual([l['time_ms'] for l in body['lines']],
                         [12000, 15500, 19250])

    def test_an_untimed_lrc_becomes_uslt(self):
        plain = os.path.join(self.tmp, 'plain.lrc')
        with open(plain, 'w', encoding='utf-8') as handle:
            handle.write("Just words\nAnd more words\n")
        self.run_cli('lyrics', 'import', self.track, '--from', plain)
        _code, body = self.json_of('lyrics', 'show', self.track)
        self.assertEqual(body['source'], 'USLT')

    def test_import_without_a_source_is_a_usage_error(self):
        code, _out, _err = self.run_cli('lyrics', 'import', self.track)
        self.assertEqual(code, out.USAGE)

    def test_import_of_a_missing_lrc_is_not_found(self):
        code, _out, _err = self.run_cli('lyrics', 'import', self.track,
                                        '--from', os.path.join(self.tmp, 'no.lrc'))
        self.assertEqual(code, out.NOT_FOUND)

    def test_import_dry_run_writes_nothing(self):
        self.run_cli('lyrics', 'import', self.track, '--from', self.lrc,
                     '--dry-run')
        self.assertEqual(self.run_cli('lyrics', 'show', self.track)[0],
                         out.NOT_FOUND)

    def test_export_round_trips_through_lrc(self):
        self.run_cli('lyrics', 'import', self.track, '--from', self.lrc)
        code, _out, _err = self.run_cli('lyrics', 'export', self.track,
                                        '--format', 'lrc', '--output', self.tmp)
        self.assertEqual(code, out.OK)
        written = os.path.join(self.tmp,
                               os.path.splitext(os.path.basename(self.track))[0]
                               + '.lrc')
        self.assertIn('[00:12.000]First line', open(written).read())

    def test_export_as_srt_numbers_and_ranges_the_cues(self):
        self.run_cli('lyrics', 'import', self.track, '--from', self.lrc)
        self.run_cli('lyrics', 'export', self.track, '--format', 'srt',
                     '--output', self.tmp)
        written = os.path.join(self.tmp,
                               os.path.splitext(os.path.basename(self.track))[0]
                               + '.srt')
        text = open(written).read()
        self.assertIn('00:00:12,000 --> 00:00:15,500', text)
        self.assertTrue(text.startswith('1\n'))

    def test_exporting_over_an_existing_file_reports_exists(self):
        self.run_cli('lyrics', 'import', self.track, '--from', self.lrc)
        self.run_cli('lyrics', 'export', self.track, '--output', self.tmp)
        code, _out, _err = self.run_cli('lyrics', 'export', self.track,
                                        '--output', self.tmp)
        self.assertEqual(code, out.EXISTS)

    def test_verify_with_no_script_or_transcript_is_not_found(self):
        code, _out, _err = self.run_cli('lyrics', 'verify', self.track)
        self.assertEqual(code, out.NOT_FOUND)


class TrimCommandsTest(CliTest):
    def setUp(self):
        super().setUp()
        self.scan()

    def test_list_succeeds_with_no_backups(self):
        code, _out, _err = self.run_cli('trim', 'list')
        self.assertEqual(code, out.OK)

    def test_restoring_an_unknown_backup_is_not_found(self):
        code, _out, _err = self.run_cli('trim', 'restore', 'nosuchbackup')
        self.assertEqual(code, out.NOT_FOUND)

    def test_detect_on_a_file_with_no_audio_fails_cleanly(self):
        code, _out, err = self.run_cli('trim', 'detect', self.tracks[0])
        self.assertIn(code, (out.FAIL, out.NO_TOOL))
        if code == out.FAIL:
            self.assertIn("Could not read", err)

    def test_an_out_point_before_the_in_point_is_a_usage_error(self):
        from src.trim import trim as t
        if not t.HAS_FFMPEG:
            self.skipTest("ffmpeg is not installed")
        code, _out, _err = self.run_cli('trim', 'cut', self.tracks[0],
                                        '--start', '10', '--end', '5', '--yes')
        self.assertEqual(code, out.USAGE)


class ChapterPolicyTest(unittest.TestCase):
    """The rule-driven chapter resolution the CLI uses in place of the editor's
    per-chapter questions."""

    def setUp(self):
        from src.trim import trim as t
        self.t = t

    def test_clamp_pulls_a_straddling_chapter_to_the_boundary(self):
        # (id, start_ms, end_ms, title), cut keeping 5s-15s.
        chapter = ('ch1', 3000, 9000, 'Intro')
        clamped = self.t.clamp_chapter(chapter, 5000, 15000)
        self.assertEqual(clamped[1], 0)
        self.assertEqual(clamped[2], 4000)

    def test_a_chapter_wholly_inside_the_cut_is_kept(self):
        self.assertEqual(
            self.t.classify_chapter(6000, 8000, 5000, 15000), 'kept')

    def test_a_chapter_wholly_outside_is_destroyed(self):
        self.assertEqual(
            self.t.classify_chapter(1000, 2000, 5000, 15000), 'destroyed')

    def test_rebase_moves_a_kept_chapter_back_by_the_cut(self):
        self.assertEqual(self.t.rebase_chapter(('ch', 7000, 9000, 'X'), 5000),
                         ('ch', 2000, 4000, 'X'))

    def test_a_file_with_no_chapters_resolves_to_none(self):
        import tempfile
        with tempfile.NamedTemporaryFile(suffix='.mp3') as handle:
            self.assertIsNone(
                self.t.apply_chapter_policy(handle.name, 0.0, 1.0, 'clamp'))


class FeedCommandsTest(CliTest):
    """Feeds are served from local files over file:// URLs — no network."""

    def setUp(self):
        super().setUp()
        from src import feed as fd
        self._saved_feeds = fd._state_path
        self.enclosures = os.path.join(self.tmp, 'enc')
        os.makedirs(self.enclosures, exist_ok=True)
        for name in ('a.mp3', 'b.mp3'):
            _mp3(os.path.join(self.enclosures, name), 'Enclosure', 'X', 'Y', '1')
        self.feed_path = os.path.join(self.tmp, 'feed.rss')
        self._write_feed()
        self.url = 'file://' + self.feed_path
        self.podcasts = os.path.join(self.tmp, 'podcasts')

    def _write_feed(self, items: str | None = None):
        """Write the test feed, with a default two-item body."""
        base = 'file://' + self.enclosures
        items = items if items is not None else f"""
<item><title>Test Show: Ep 1.	First one</title>
<pubDate>Fri, 06 Jun 2025 18:00:00 +0000</pubDate>
<guid>urn:test:1</guid>
<enclosure url="{base}/a.mp3" length="1" type="audio/mpeg"/></item>
<item><title>Test Show - 14th March</title>
<pubDate>Fri, 14 Mar 2025 18:00:00 +0000</pubDate>
<guid>urn:test:2</guid>
<enclosure url="{base}/b.mp3" length="1" type="audio/mpeg"/></item>"""
        with open(self.feed_path, 'w', encoding='utf-8') as handle:
            handle.write("<?xml version='1.0' encoding='UTF-8'?>"
                         "<rss version='2.0'><channel>"
                         "<title>Local Test Feed</title>"
                         "<link>http://example.invalid</link>"
                         "<description>d</description>"
                         f"{items}</channel></rss>")

    def _mp3s(self):
        """Every audio file under the download directory."""
        found = []
        for root, _dirs, names in os.walk(self.podcasts):
            found += [os.path.join(root, n) for n in names if n.endswith('.mp3')]
        return sorted(found)

    # -- fetch ---------------------------------------------------------------

    def test_fetch_reads_a_feed_without_storing_it(self):
        _code, body = self.json_of('feed', 'fetch', self.url)
        self.assertEqual(body['count'], 2)
        _code, listed = self.json_of('feed', 'list')
        self.assertEqual(listed['items'], [])

    def test_fetch_carries_the_parse_and_the_raw_title(self):
        _code, body = self.json_of('feed', 'fetch', self.url)
        first = body['items'][0]
        self.assertEqual(first['show'], 'Test Show')
        self.assertEqual(first['episode'], 1)
        self.assertEqual(first['title'], 'First one')
        self.assertIn('\t', first['raw'])

    def test_fetch_can_filter_by_title(self):
        _code, body = self.json_of('feed', 'fetch', self.url,
                                   '--filter-title', '14th March')
        self.assertEqual(body['count'], 1)

    def test_fetch_of_something_that_is_not_a_feed_fails(self):
        bad = os.path.join(self.tmp, 'bad.xml')
        with open(bad, 'w') as handle:
            handle.write('<html>404</html>')
        code, _out, _err = self.run_cli('feed', 'fetch', 'file://' + bad)
        self.assertEqual(code, out.FAIL)

    # -- add / list / remove -------------------------------------------------

    def test_add_then_list(self):
        self.assertEqual(self.run_cli('feed', 'add', self.url,
                                      '--name', 'local')[0], out.OK)
        _code, body = self.json_of('feed', 'list')
        self.assertEqual([i['name'] for i in body['items']], ['local'])

    def test_a_name_is_derived_from_the_feed_title_when_not_given(self):
        self.run_cli('feed', 'add', self.url)
        _code, body = self.json_of('feed', 'list')
        self.assertEqual(body['items'][0]['name'], 'local-test-feed')

    def test_adding_the_same_name_twice_reports_exists(self):
        self.run_cli('feed', 'add', self.url, '--name', 'local')
        code, _out, _err = self.run_cli('feed', 'add', self.url,
                                        '--name', 'local')
        self.assertEqual(code, out.EXISTS)

    def test_add_dry_run_stores_nothing(self):
        self.run_cli('feed', 'add', self.url, '--name', 'local', '--dry-run')
        self.assertEqual(self.json_of('feed', 'list')[1]['items'], [])

    def test_remove_forgets_the_feed(self):
        self.run_cli('feed', 'add', self.url, '--name', 'local')
        self.assertEqual(self.run_cli('feed', 'remove', 'local', '--yes')[0],
                         out.OK)
        self.assertEqual(self.json_of('feed', 'list')[1]['items'], [])

    def test_removing_an_unknown_feed_is_not_found(self):
        code, _out, _err = self.run_cli('feed', 'remove', 'nope', '--yes')
        self.assertEqual(code, out.NOT_FOUND)

    # -- sync ----------------------------------------------------------------

    def test_sync_downloads_the_episodes(self):
        self.run_cli('feed', 'add', self.url, '--name', 'local')
        code, _out, _err = self.run_cli('feed', 'sync', '--name', 'local',
                                        '--output', self.podcasts)
        self.assertEqual(code, out.OK)
        self.assertEqual(len(self._mp3s()), 2)

    def test_syncing_twice_downloads_nothing_the_second_time(self):
        self.run_cli('feed', 'add', self.url, '--name', 'local')
        self.run_cli('feed', 'sync', '--name', 'local', '--output', self.podcasts)
        first = {p: os.path.getmtime(p) for p in self._mp3s()}
        _code, stdout, _err = self.run_cli('feed', 'sync', '--name', 'local',
                                           '--output', self.podcasts)
        second = {p: os.path.getmtime(p) for p in self._mp3s()}
        self.assertEqual(first, second)           # same files, untouched
        self.assertIn('0 new episodes', stdout)

    def test_syncing_twice_leaves_one_copy_of_each_episode(self):
        self.run_cli('feed', 'add', self.url, '--name', 'local')
        for _ in range(3):
            self.run_cli('feed', 'sync', '--name', 'local',
                         '--output', self.podcasts)
        self.assertEqual(len(self._mp3s()), 2)

    def test_a_new_episode_on_a_later_sync_is_picked_up(self):
        self.run_cli('feed', 'add', self.url, '--name', 'local')
        self.run_cli('feed', 'sync', '--name', 'local', '--output', self.podcasts)
        base = 'file://' + self.enclosures
        self._write_feed(f"""
<item><title>Test Show: Ep 3. Third one</title>
<pubDate>Fri, 20 Jun 2025 18:00:00 +0000</pubDate>
<guid>urn:test:3</guid>
<enclosure url="{base}/a.mp3" length="1" type="audio/mpeg"/></item>""")
        self.run_cli('feed', 'sync', '--name', 'local', '--output', self.podcasts)
        self.assertEqual(len(self._mp3s()), 3)

    def test_dedupe_falls_back_to_the_enclosure_url_with_no_guid(self):
        base = 'file://' + self.enclosures
        self._write_feed(f"""
<item><title>Test Show: Ep 9. No guid</title>
<pubDate>Fri, 06 Jun 2025 18:00:00 +0000</pubDate>
<enclosure url="{base}/a.mp3" length="1" type="audio/mpeg"/></item>""")
        self.run_cli('feed', 'add', self.url, '--name', 'local')
        self.run_cli('feed', 'sync', '--name', 'local', '--output', self.podcasts)
        self.run_cli('feed', 'sync', '--name', 'local', '--output', self.podcasts)
        self.assertEqual(len(self._mp3s()), 1)

    def test_the_title_filter_is_honoured_on_sync(self):
        self.run_cli('feed', 'add', self.url, '--name', 'local',
                     '--filter-title', '14th March')
        self.run_cli('feed', 'sync', '--name', 'local', '--output', self.podcasts)
        self.assertEqual(len(self._mp3s()), 1)

    def test_sync_dry_run_downloads_nothing(self):
        self.run_cli('feed', 'add', self.url, '--name', 'local')
        code, stdout, _err = self.run_cli('feed', 'sync', '--name', 'local',
                                          '--output', self.podcasts, '--dry-run')
        self.assertEqual(code, out.OK)
        self.assertEqual(self._mp3s(), [])
        self.assertIn('would be downloaded', stdout)

    def test_sync_emits_one_event_per_episode_under_json(self):
        self.run_cli('feed', 'add', self.url, '--name', 'local')
        _code, stdout, _err = self.run_cli('feed', 'sync', '--name', 'local',
                                           '--output', self.podcasts, '--json')
        events = [json.loads(line) for line in stdout.splitlines()]
        self.assertEqual(len(events), 2)
        self.assertTrue(all(e['event'] == 'written' for e in events))
        self.assertTrue(all(e['schema'] == out.SCHEMA_VERSION for e in events))

    def test_the_download_is_tagged_from_the_parse(self):
        self.run_cli('feed', 'add', self.url, '--name', 'local')
        self.run_cli('feed', 'sync', '--name', 'local', '--output', self.podcasts)
        first = [p for p in self._mp3s() if 'First one' in p][0]
        tags = ID3(first)
        self.assertEqual(str(tags['TIT2'].text[0]), 'First one')
        self.assertEqual(str(tags['TALB'].text[0]), 'Test Show')
        self.assertEqual(str(tags['TRCK'].text[0]), '1')
        self.assertEqual(str(tags['TCON'].text[0]), 'Podcast')

    def test_the_raw_title_survives_in_a_comment(self):
        self.run_cli('feed', 'add', self.url, '--name', 'local')
        self.run_cli('feed', 'sync', '--name', 'local', '--output', self.podcasts)
        first = [p for p in self._mp3s() if 'First one' in p][0]
        comments = [str(f.text[0]) for k, f in ID3(first).items()
                    if k.startswith('COMM')]
        self.assertTrue(any('\t' in c for c in comments), comments)

    def test_a_dated_episode_gets_its_broadcast_date_not_the_feed_date(self):
        self.run_cli('feed', 'add', self.url, '--name', 'local')
        self.run_cli('feed', 'sync', '--name', 'local', '--output', self.podcasts)
        dated = [p for p in self._mp3s() if '14th March' in p][0]
        self.assertEqual(str(ID3(dated)['TDRC'].text[0]), '2025-03-14')

    def test_the_download_reaches_the_library(self):
        self.run_cli('library', 'dirs', '--add', self.podcasts)
        self.run_cli('feed', 'add', self.url, '--name', 'local')
        self.run_cli('feed', 'sync', '--name', 'local', '--output', self.podcasts)
        _code, body = self.json_of('track', 'list', '--album', 'Test Show')
        self.assertEqual(len(body['items']), 2)

    def test_syncing_an_unknown_feed_is_not_found(self):
        code, _out, _err = self.run_cli('feed', 'sync', '--name', 'nope',
                                        '--output', self.podcasts)
        self.assertEqual(code, out.NOT_FOUND)

    def test_sync_with_no_feeds_says_so_and_succeeds(self):
        code, stdout, _err = self.run_cli('feed', 'sync',
                                          '--output', self.podcasts)
        self.assertEqual(code, out.OK)
        self.assertIn('No feeds added', stdout)

    def test_sync_with_nowhere_to_put_them_is_a_usage_error(self):
        self.run_cli('feed', 'add', self.url, '--name', 'local')
        code, _out, _err = self.run_cli('feed', 'sync', '--name', 'local')
        self.assertEqual(code, out.USAGE)

    def test_a_part_file_is_never_left_behind(self):
        self.run_cli('feed', 'add', self.url, '--name', 'local')
        self.run_cli('feed', 'sync', '--name', 'local', '--output', self.podcasts)
        for root, _dirs, names in os.walk(self.podcasts):
            self.assertEqual([n for n in names if n.endswith('.part')], [])


if __name__ == "__main__":
    unittest.main()
