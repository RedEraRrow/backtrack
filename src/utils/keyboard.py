"""Which keyboard the typist is using — only as far as search needs it.

`search._lev` scores a spelling mistake by whether the wrong letter sits next to
the right one, so the only thing that matters here is where the *letters* are.
That collapses the world's keyboards into a handful of families: British, US,
Canadian, Irish, Australian and ABC differ solely in punctuation and dead keys,
and are all one QWERTY as far as a typo is concerned.

Detection is best-effort by design. Every branch is wrapped, and anything
unreadable, unrecognised or unknown falls back to QWERTY — the assumption the
matcher made before this module existed. `BACKTRACK_KEYBOARD` overrides it
outright, which is the only thing that can be right over SSH: the layout lives
on the machine in front of the typist, not the one running the process.
"""
from __future__ import annotations

import os
import sys

# Row-by-row key positions. Punctuation is kept in place even though nothing
# looks it up: it holds the columns open, so dropping the leading "'," from
# Dvorak's top row would slide every letter on it one key to the left.
LAYOUTS: dict[str, tuple[str, ...]] = {
    'qwerty':  ("1234567890-=", "qwertyuiop[]", "asdfghjkl;'", "zxcvbnm,./"),
    'qwertz':  ("1234567890ß'", "qwertzuiopü+", "asdfghjklöä", "yxcvbnm,.-"),
    'azerty':  ("1234567890°+", "azertyuiop^$", "qsdfghjklmù", "wxcvbn,;:!"),
    'dvorak':  ("1234567890[]", "',.pyfgcrl/=", "aoeuidhtns-", ";qjkxbmwvz"),
    'colemak': ("1234567890-=", "qwfpgjluy;[]", "arstdhneio'", "zxcvbkm,./"),
}

DEFAULT = 'qwerty'

# Layout names (macOS), XKB codes (Linux) and language ids (Windows) that move
# letters around. Matched as substrings against a lowercased name, so "Swiss
# German" and "German — Standard" both land on QWERTZ.
_NAME_FAMILIES: tuple[tuple[str, str], ...] = (
    ('dvorak', 'dvorak'), ('colemak', 'colemak'),
    ('azerty', 'azerty'), ('french', 'azerty'), ('belgian', 'azerty'),
    ('qwertz', 'qwertz'), ('german', 'qwertz'), ('swiss', 'qwertz'),
    ('austrian', 'qwertz'), ('czech', 'qwertz'), ('slovak', 'qwertz'),
    ('hungarian', 'qwertz'), ('croatian', 'qwertz'), ('serbian', 'qwertz'),
    ('slovenian', 'qwertz'), ('bosnian', 'qwertz'),
)

_XKB_FAMILIES: dict[str, str] = {
    'fr': 'azerty', 'be': 'azerty',
    'de': 'qwertz', 'ch': 'qwertz', 'at': 'qwertz', 'cz': 'qwertz',
    'sk': 'qwertz', 'hu': 'qwertz', 'hr': 'qwertz', 'rs': 'qwertz',
    'si': 'qwertz', 'ba': 'qwertz',
}

# Windows primary language ids (the low byte of the low word of the HKL).
_WIN_FAMILIES: dict[int, str] = {
    0x0c: 'azerty',                                     # French (incl. Belgian)
    0x13: 'azerty',                                     # Dutch — Belgian is AZERTY
    0x07: 'qwertz', 0x05: 'qwertz', 0x0e: 'qwertz',     # German, Czech, Hungarian
    0x1b: 'qwertz', 0x24: 'qwertz', 0x1a: 'qwertz',     # Slovak, Slovenian, Croatian
}


def _family_from_name(name: str) -> str | None:
    """Map a human layout name ("ABC — AZERTY", "Swiss German") to a family."""
    low = name.lower()
    for needle, family in _NAME_FAMILIES:
        if needle in low:
            return family
    return None


def _detect_macos() -> str | None:
    """The selected input source, straight out of the HIToolbox preferences.

    Read with plistlib rather than shelling out to `defaults`: no subprocess on
    startup, and no dependency on pyobjc for the Carbon TIS API.
    """
    import plistlib
    path = os.path.expanduser("~/Library/Preferences/com.apple.HIToolbox.plist")
    with open(path, 'rb') as fh:
        prefs = plistlib.load(fh)
    for source in prefs.get('AppleSelectedInputSources', []):
        if source.get('InputSourceKind') != 'Keyboard Layout':
            continue
        # A selected source can be one with no letter arrangement of its own —
        # "Unicode Hex Input" is US letters with hex entry bolted on. Unnamed
        # families fall through to QWERTY rather than guessing from whatever
        # else the user happens to have enabled.
        return _family_from_name(str(source.get('KeyboardLayout Name', '')))
    return None


def _detect_linux() -> str | None:
    """XKB layout: the X server first, then systemd, then the Debian default."""
    import subprocess

    for cmd, key in ((['setxkbmap', '-query'], 'layout:'),
                     (['localectl', 'status'], 'x11 layout:')):
        try:
            out = subprocess.run(cmd, capture_output=True, text=True, timeout=2).stdout
        except (OSError, subprocess.SubprocessError):
            continue
        for line in out.splitlines():
            low = line.strip().lower()
            if low.startswith(key):
                # "layout: gb,fr" — the first is the active one.
                code = low[len(key):].strip().split(',')[0].strip()
                if code:
                    return _XKB_FAMILIES.get(code, DEFAULT)

    try:
        with open('/etc/default/keyboard') as fh:
            for line in fh:
                if line.startswith('XKBLAYOUT='):
                    code = line.split('=', 1)[1].strip().strip('"\'').split(',')[0]
                    if code:
                        return _XKB_FAMILIES.get(code, DEFAULT)
    except OSError:
        pass
    return None


def _detect_windows() -> str | None:
    """Active keyboard layout of the foreground thread, via user32.

    The HKL's low word is a language id, which is as far as this gets: it names
    French but cannot tell US Dvorak from US QWERTY (that lives in the registry,
    keyed by device). Dvorak and Colemak users on Windows want the env var.
    """
    import ctypes
    hkl = ctypes.windll.user32.GetKeyboardLayout(0)          # type: ignore[attr-defined]
    return _WIN_FAMILIES.get(hkl & 0xff, DEFAULT)


def detect() -> str:
    """Name the keyboard family in front of the user. Never raises."""
    override = (os.environ.get('BACKTRACK_KEYBOARD') or '').strip().lower()
    if override in LAYOUTS:
        return override

    detector = ({'darwin': _detect_macos, 'win32': _detect_windows}
                .get(sys.platform, _detect_linux))
    try:
        return detector() or DEFAULT
    except Exception:
        return DEFAULT


def rows(name: str | None = None) -> tuple[str, ...]:
    """Key rows for a layout family, or for the detected one."""
    return LAYOUTS.get(name or detect(), LAYOUTS[DEFAULT])
