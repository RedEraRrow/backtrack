"""setting(): the one place a setting's default comes from."""
import unittest

from src.config import DEFAULT_CONFIG, setting


class SettingTest(unittest.TestCase):
    def test_value_else_default(self):
        self.assertIs(setting({'debug': True}, 'debug'), True)
        self.assertIs(setting({}, 'debug'), DEFAULT_CONFIG['debug'])

    def test_a_list_default_is_a_copy(self):
        words = setting({}, 'sort_ignore_words')
        words.append('Le')
        self.assertNotIn('Le', DEFAULT_CONFIG['sort_ignore_words'])

    def test_unknown_key_is_a_bug_not_a_silent_none(self):
        with self.assertRaises(KeyError):
            setting({}, 'no_such_setting')


if __name__ == "__main__":
    unittest.main()
