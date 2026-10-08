"""Per-file ID3 tag browser and editor UI, plus the sort-order name engine
(name splitting and sort suggestions) that bulk sort uses."""
from __future__ import annotations

from io import BytesIO
from backtrack.id3.tag_formats import is_mp3, kind as tag_kind
import os
import re
import sys
import tempfile
import pyperclip
import subprocess

from backbone import prompt
from backtrack.lyrics.editor import lyrics_editor
from backtrack.lyrics.sync_doc import find_lyrics
from backtrack.lyrics import formats as _lyrics
from backtrack.trim import engine as _trim
from backtrack.trim.editor import trim_editor
from mutagen.id3 import ID3
import mutagen.id3
from mutagen.id3._frames import APIC

from backbone import ui
from backbone.prompt.core import _visible_rows
from backbone.ui import Colors as C, get_terminal_width
from backtrack.playback.player_art import fill_art, image_cells
from backtrack.album_art import decode_image, picture_file
from backtrack.music_library import (drop_moved, format_label, library_entry, refresh_library_entry,
                                    track_title, first_text)

from backtrack.id3.tag_handler import (
    get_tag_info, get_tag_category, display_tag_id, summarize_tag_value, prompt_for_value,
    create_frame, rename_frame, rename_would_replace, people_to_text, people_from_text,
    save_id3, load_id3, create_apic_frame, pick_nearby_cover, _EXT_TO_MIME,
    _prompt_for_image_metadata, parse_composite_tag_id,
)
from backtrack.id3 import tag_registry as _reg
from backtrack.config import setting
from backbone.log import quietly
from backtrack import tuning as tune
from backtrack.id3.tag_handler import picture_type_name, tag_id_input, tag_key_input

# Structured columns for the tag list. Column 1 holds the tag id AND the friendly
# name as two styled segments (TAG bright + friendly dim) in a single column.
_TAG_COLUMNS = [
    prompt.Column(style='primary'),                       # TAG (friendly)
    prompt.Column(style='dynamic-dim', priority=1),       # type / category, drops first when narrow
    prompt.Column(style='normal', flex=True),             # value (takes the rest, kept)
]


# sort frame → the text frame it orders, and the sort frames where full
# name-splitting applies (artists/composers); the rest just strip articles.
# Both come from the canonical table in tag_registry.
_SORT_SOURCES: dict[str, str] = dict(_reg.SORT_SOURCE_OF)
_NAME_SORT_TAGS = _reg.NAME_SORT_FRAMES

# Articles in English + major European languages.
# Longer strings first so prefix matching is unambiguous.
_ARTICLE_MAP: dict[str, str] = {
    # English
    'the': 'The', 'an': 'An', 'a': 'A',
    # French
    'les': 'Les', 'le': 'Le', 'la': 'La', "l'": "L'",
    # Spanish / Portuguese
    'los': 'Los', 'las': 'Las', 'el': 'El', 'os': 'Os', 'as': 'As',
    # German
    'die': 'Die', 'der': 'Der', 'das': 'Das', 'den': 'Den', 'dem': 'Dem',
    # Italian
    'gli': 'Gli', 'lo': 'Lo', 'il': 'Il',
    # Dutch
    'het': 'Het', 'de': 'De',
    # Swedish / Norwegian / Danish
    'den': 'Den', 'det': 'Det',
}

# Spacing surname prefixes: the prefix + following word(s) form the compound surname.
# "van Beethoven" → surname is "van Beethoven".
_SPACING_PREFIXES: frozenset[str] = frozenset({
    'von', 'van', 'de', 'del', 'della', 'degli', 'dei', 'di', 'da', 'du',
    'le', 'la', 'les', 'bin', 'ibn', 'al', 'af', 'av', 'zu', 'ter', 'ten',
    'uit', 'den', 'het',
})

# Joining prefixes: merged (no space) with the following word to form one surname token.
# "O' Connor" → "O'Connor",  "Mc Gregor" → "McGregor".
_JOINING_PREFIXES: frozenset[str] = frozenset({"o'", "ó'", 'mc', 'mac', "m'"})

# Leading honorifics/titles moved to the end of the sort name ("Dr. Dre" → "Dre,
# Dr.", "MC Solaar" → "Solaar, MC"). A small fixed pattern set, no data corpus.
_HONORIFICS: frozenset[str] = frozenset({
    'dr', 'prof', 'sir', 'dame', 'mr', 'mrs', 'ms', 'miss', 'mx', 'rev', 'fr',
    'st', 'maestro', 'dj', 'mc', 'lady', 'lord', 'master', 'madam', 'madame',
    'herr', 'frau', 'signor', 'signora', 'don', 'doña', 'sri', 'sheikh', 'imam',
    'rabbi', 'capt', 'col', 'gen', 'sgt', 'lt', 'hon',
})

# Trailing generational/ordinal suffixes kept with the surname ("James Brown Jr."
# → "Brown, James Jr."). Also a fixed pattern set. NB the roman numerals here also
# usefully leave a two-token stage name like "Sammy V" un-inverted: stripping the
# suffix leaves one word, so the engine returns the name unchanged.
_NAME_SUFFIXES: frozenset[str] = frozenset({
    'jr', 'sr', 'jnr', 'snr', 'ii', 'iii', 'iv', 'v', 'vi', 'vii',
    'phd', 'md', 'esq',
})

# An ensemble is a body, not a person: it sorts under its own first word, with
# only a leading article moved. "Orchestra, Berlin Philharmonic" files a real
# orchestra under a word that is not its name. None of these double as parts of
# a personal name, so a generous list costs nothing.
_ENSEMBLE_WORDS: frozenset[str] = frozenset({
    'orchestra', 'orchestras', 'orchestre', 'orquesta', 'philharmonic',
    'philharmonia', 'symphony', 'symphonietta', 'sinfonia', 'sinfonietta',
    'band', 'bigband', 'quartet', 'quartette', 'quintet', 'sextet', 'septet',
    'octet', 'trio', 'duo', 'ensemble', 'choir', 'chorus', 'chorale', 'choral',
    'singers', 'players', 'consort', 'collective', 'camerata', 'academy',
    'capella', 'cappella', 'soloists', 'allstars', 'all-stars', 'society',
    'club', 'brass', 'quire', 'orchestrina',
})

# Single-letter initial pattern: "J." or "J" alone.
_INITIAL_RE = re.compile(r'^[A-Za-zÀ-ÖØ-öø-ÿ]\.$')

# Delimiters that separate multiple artists in a single tag value.
# NOTE: "and his/her/their" is NOT a delimiter: a *possessive* names a backing
# ensemble belonging to the lead artist ("Paul Tremaine And His Aristocrats").
# "& The …" IS split, though: "The Mildred Snitzer Orchestra" is a named act in
# its own right, so "Jeff Goldblum & The Mildred Snitzer Orchestra" sorts as
# "Goldblum, Jeff/Mildred Snitzer Orchestra, The" rather than being inverted
# whole into "Orchestra, Jeff Goldblum & The Mildred Snitzer".
# The ampersand is kept apart from the rest because it is the one delimiter that
# is often part of a name rather than between two: see _DUO_RE.
_AMP_ALTS = (
    r'(?<!\w)&(?!\s*(?:his|her|their)\b)'          # & but not "& His/Her/Their"
    r'|\band(?=\s+\w)(?!\s+(?:his|her|their)\b)'  # "and X" but not "and his/her/their"
)
_OTHER_ALTS = (
    r'\|'
    r'|[/\\]'
    r'|\bfeaturing\b|\bfeat\.?\b|\bft\.?\b'
    r'|\bwith(?=\s+\w)(?!\s+(?:his|her|their)\b)'
    r'|\bvs\.?\b'
    r'|\+'
)
_LIST_SPLIT_RE  = re.compile(rf'\s*(?:{_AMP_ALTS}|{_OTHER_ALTS})\s*', re.IGNORECASE)
_AMP_SPLIT_RE   = re.compile(rf'\s*(?:{_AMP_ALTS})\s*', re.IGNORECASE)
_OTHER_SPLIT_RE = re.compile(rf'\s*(?:{_OTHER_ALTS})\s*', re.IGNORECASE)

# "Blank & Jones", "Hall & Oates", "Above & Beyond": an ampersand between two
# single words is the name of one act, and it sorts under its own first word.
# Two artists collaborating are credited by their full names either side
# ("Ariana Grande & Justin Bieber"), which is what tells them apart. Two mononyms
# who really are collaborating are read as an act and so get no sort tag at all:
# the safe way to be wrong, since nothing incorrect is written.

_DUO_RE = re.compile(r'^[^\s&/]+\s*(?:&|\band\b)\s*[^\s&/]+$', re.IGNORECASE)


# Integers (1-99) that benefit from zero-padding when used as ordinals.
_ORDINAL_RE = re.compile(r'(?<!\d)([1-9]\d?)(?!\d)')


def _pad_ordinals(s: str) -> str:
    """'Series 1' → 'Series 01'; leaves two-digit numbers unchanged."""
    def _pad(m: re.Match) -> str:
        """Zero-pad a single-digit match; leave two-digit numbers as-is."""
        n = int(m.group(1))
        return str(n).zfill(2) if n < 10 else m.group(1)
    return _ORDINAL_RE.sub(_pad, s)


def _merge_initials(words: list[str]) -> list[str]:
    """Merge space-separated single-letter initials: ['J.', 'S.', 'Bach'] → ['J.S.', 'Bach']."""
    result: list[str] = []
    i = 0
    while i < len(words):
        if _INITIAL_RE.match(words[i]):
            group = [words[i]]
            j = i + 1
            while j < len(words) and _INITIAL_RE.match(words[j]):
                group.append(words[j])
                j += 1
            result.append(''.join(group))
            i = j
        else:
            result.append(words[i])
            i += 1
    return result


def _merge_joining_prefixes(words: list[str]) -> list[str]:
    """Merge Celtic/Irish prefix tokens with the following word without a space.
    ['Sinéad', "O'", 'Connor'] → ['Sinéad', "O'Connor"]
    ['Ewan', 'Mc', 'Gregor'] → ['Ewan', 'McGregor']

    Does NOT merge all-uppercase 'MC' (hip-hop prefix): that is an honorific,
    handled separately by the honorific-stripping step.
    """
    result: list[str] = []
    i = 0
    while i < len(words):
        w = words[i]
        wl = w.lower()
        # Only merge if it looks like a Celtic prefix (mixed/lower case), not
        # an all-caps abbreviation like "MC" or "MAC" used as a stage prefix.
        is_celtic = wl in _JOINING_PREFIXES and not w.isupper()
        if is_celtic and i + 1 < len(words):
            result.append(w + words[i + 1])
            i += 2
        else:
            result.append(w)
            i += 1
    return result


# A comma does two jobs in a name tag, and which one it is depends on the
# company it keeps: it separates people ("Emerson, Lake & Palmer") and it marks
# a name already written in sort order ("Ahlert, Fred E.").
_COMMA_SPLIT_RE = re.compile(r'\s*,\s*')
_STRAY_LEAD_RE  = re.compile(r'^[^\w\s]+')
# "E." / "J.S.": a forename side made only of these is a strong sign the comma
# before it inverted a name rather than separated two people.
_INITIALS_RE = re.compile(r'^[A-Za-zÀ-ÖØ-öø-ÿ](?:\.[A-Za-zÀ-ÖØ-öø-ÿ])*\.?$')


def _clean_part(part: str) -> str:
    """Trim the stray punctuation delimiter tokenisation leaves behind (the "." of "Feat.").

    A trailing "." straight after a letter is kept: that is an initial or an
    abbreviation ("Ahlert, Fred E.", "J.S.", "Jr."), not debris.
    """
    part = _STRAY_LEAD_RE.sub('', part).strip()
    while part and not part[-1].isalnum() and not (
            part[-1] == '.' and len(part) > 1 and part[-2].isalpha()):
        part = part[:-1].rstrip()
    return part


# "Al Jackson, Jr.": this comma is punctuation inside one name, neither a list
# separator nor an inversion, so it goes before either question is asked.
_SUFFIX_COMMA_RE = re.compile(
    r',\s*(?=(?:' + '|'.join(sorted(_NAME_SUFFIXES, key=len, reverse=True)) + r')\b\.?)',
    re.IGNORECASE,
)


def _forename_is_initials(part: str) -> bool:
    """Whether the side after the comma is nothing but initials ("Bach, J.S.")."""
    right = part.partition(',')[2].strip()
    return bool(right) and all(_INITIALS_RE.match(w) for w in right.split())


def _looks_inverted(part: str) -> bool:
    """Whether one part reads as a single name already in "Surname, Forename" order.

    One comma, and a forename side short enough to *be* a forename: "Ahlert,
    Fred E." inverts, but "Miles Davis, John Coltrane" (two full names either
    side) is two people who happen to be separated by a comma.
    """
    left, sep, right = part.partition(',')
    if not sep or ',' in right:
        return False
    left, right = left.strip(), right.strip()
    if not left or not right:
        return False
    return not (len(left.split()) >= 2 and len(right.split()) >= 2)


def _name_readings(raw: str) -> list[list[str]]:
    """The plausible ways to read a value as a list of people, best first.

    Splitting on "&"/"and"/"feat." is unambiguous; the comma is not, so it is
    read from context:

    * "Somebody, Somebody Else & A Third Person": one part has a comma and
      another does not, which is how English writes a list. Three people.
    * "Bach, Johann Sebastian & Mozart, Wolfgang Amadeus": *every* part carries
      one comma and each reads as inverted. Two people, already sorted.
    * "Lennon, John" on its own: one inverted name, left exactly as it is.
    * "Miles Davis, John Coltrane": a full name either side of the comma, so a
      list of two despite there being no other delimiter.

    Where a value reads both ways ("Bach, J.S. & Handel" is a list of three under
    the first rule and a pair under the second), both readings come back and the
    editor offers each; a part left holding its comma is already in sort order
    and is passed through untouched rather than inverted again.
    """
    raw = _SUFFIX_COMMA_RE.sub(' ', raw)
    # Everything but the ampersand splits first; each piece is then broken on the
    # ampersand *unless* it is a duo's name, so "Blank & Jones feat. Bernard
    # Sumner" is the act and its guest rather than three artists.
    split: list = []
    for piece in _OTHER_SPLIT_RE.split(raw):
        if _DUO_RE.match(piece.strip()):
            split.append(piece)
        else:
            split.extend(_AMP_SPLIT_RE.split(piece))
    if len(split) == 1 and ',' not in raw:
        # Nothing was tokenised, so there is no delimiter debris to tidy, and a
        # value that is all punctuation ("!!!") keeps every bit of it.
        return [[raw.strip()]] if raw.strip() else []
    parts = [p for p in (_clean_part(x) for x in split) if p]
    if not parts:
        return []
    if len(parts) == 1 and ',' not in raw:
        # Tidying dropped everything but one person, so the delimiter was part of
        # the name after all ("...And You Will Know Us by the Trail of Dead"):
        # hand back the value whole rather than the tidied fragment.
        return [[raw.strip()]]

    with_comma = [p for p in parts if ',' in p]
    if not with_comma:
        return [parts]

    def _by_comma(items: list[str]) -> list[str]:
        """Break every part on its commas too."""
        return [c for p in items
                for c in (_clean_part(x) for x in _COMMA_SPLIT_RE.split(p)) if c]

    all_inverted = len(with_comma) == len(parts) and all(map(_looks_inverted, parts))
    if len(parts) > 1:
        if all_inverted:
            return [parts]                       # a list of already-sorted names
        # A comma beside a comma-less part is a list: that is what the mix
        # means, and second-guessing it flags half a library as ambiguous.
        # Initials are the one thing that outweighs it: "J.S." is a forename and
        # never a person in a list, so "Bach, J.S. & Handel" really is a pair,
        # and only there is the other reading worth offering as well.
        if all(map(_looks_inverted, with_comma)) and any(map(_forename_is_initials, with_comma)):
            return [parts, _by_comma(parts)]
        return [_by_comma(parts)]

    return [parts] if all_inverted else [_by_comma(parts)]


def _is_ensemble(words: list[str]) -> bool:
    """Whether a name reads as a group rather than a person."""
    return any(w.strip('.,()').lower() in _ENSEMBLE_WORDS for w in words)


def split_options(raw: str) -> list[list[str]]:
    """Every plausible way to read one tag value as a list of names, best first.

    The engine's own reading leads; then the value whole, as a single name; then
    the maximal split, every delimiter honoured (duos, backing bands and commas
    included). A heuristic that guessed wrong is then overruled by picking one of
    these rather than by retyping the names, which is what the split-verification
    step in bulk cycles through.
    """
    out: list[list[str]] = []

    def _add(people: list) -> None:
        """Append a splitting if it is new and non-empty."""
        cleaned = [p for p in (_clean_part(x) for x in people) if p]
        if cleaned and cleaned not in out:
            out.append(cleaned)

    for reading in _name_readings(raw):
        _add(reading)
    _add([raw])
    flat = _SUFFIX_COMMA_RE.sub(' ', raw)
    _add([c for part in _LIST_SPLIT_RE.split(flat) for c in _COMMA_SPLIT_RE.split(part)])
    return out or [[raw]]


def _sort_single_name(name: str) -> list[str]:
    """
    Return sort-order candidates for a single name string, ordered by confidence.

    Pure heuristics, no name corpus. A simple name (one/two words, or a leading
    article) resolves to a single candidate; an ambiguous multi-word name yields
    several (the positional "last word = surname" split first) that the user
    picks from, or overrides with "type custom".

    Pipeline:
    1. Merge space-separated initials ('J. S.' → 'J.S.')
    2. Merge Celtic joining prefixes ("O' Connor" → "O'Connor")
    3. Leading article → move to end ('The Beatles' → 'Beatles, The')
    4. Strip a leading honorific ('Dr. Dre' → 'Dre, Dr.')
    5. Strip a trailing ordinal suffix ('James Brown Jr.' → suffix kept on surname)
    6. Spacing surname prefixes (von/van/de/…) start the surname
    7. All right-splits, last-word-as-surname first (the positional default)
    """
    words = _merge_joining_prefixes(_merge_initials(name.split()))
    if len(words) <= 1:
        return [name]

    # ── Article ────────────────────────────────────────────────────────────
    if words[0].lower() in _ARTICLE_MAP:
        art  = _ARTICLE_MAP[words[0].lower()]
        rest = ' '.join(words[1:])
        return [f"{rest}, {art}"]

    # ── Ensemble / duo ─────────────────────────────────────────────────────
    # A group sorts as itself: no surname to bring to the front. (Any leading
    # article was already moved above, so "The E Street Band" got there first.)
    if _is_ensemble(words) or _DUO_RE.match(name.strip()):
        return [name]

    candidates: list[str] = []
    seen: set[str] = set()

    def _add(firstname: str, surname: str) -> None:
        """Append a "Surname, Firstname" candidate (with honorific/suffix folded in) if new."""
        fn = ' '.join(filter(None, [honorific, firstname]))
        sn = ' '.join(filter(None, [surname, suffix]))
        c  = f"{sn}, {fn}" if fn else sn
        if c and c not in seen:
            seen.add(c)
            candidates.append(c)

    # ── Honorific ──────────────────────────────────────────────────────────
    honorific = ''
    if words[0].rstrip('.').lower() in _HONORIFICS:
        honorific = words[0]
        words = words[1:]
        if len(words) == 1:
            # e.g. "Dr. Dre" → "Dre, Dr." / "MC Solaar" → "Solaar, MC"
            return [f"{words[0]}, {honorific}"]
        if not words:
            return [name]

    # ── Trailing suffix ────────────────────────────────────────────────────
    suffix = ''
    if words[-1].rstrip('.').lower() in _NAME_SUFFIXES:
        suffix = words[-1]
        words = words[:-1]
        if len(words) <= 1:
            return [name]

    # ── Spacing surname prefixes (von/van/de/…) ─────────────────────────────
    # A prefix mid-name starts the surname ("Ludwig van Beethoven" →
    # "van Beethoven, Ludwig"), offered before the plain right-splits.
    for i, w in enumerate(words):
        if i == 0:
            continue
        if w.lower() in _SPACING_PREFIXES and i < len(words) - 1:
            _add(' '.join(words[:i]), ' '.join(words[i:]))

    # ── All right-splits (last word as surname first = the positional default) ─
    for n in range(1, len(words)):
        _add(' '.join(words[:len(words) - n]), ' '.join(words[len(words) - n:]))

    return candidates if candidates else [name]


def _sort_candidates(base_id: str, raw: str) -> list[str]:
    """
    Generate sort-order candidates for a raw tag value.
    Returns candidates ordered by confidence, excluding the raw value itself.
    """
    candidates: list[str] = []
    seen: set[str] = {raw}

    def _add(c: str) -> None:
        """Append a candidate if non-empty and not already seen."""
        if c and c not in seen:
            seen.add(c)
            candidates.append(c)

    if base_id in _NAME_SORT_TAGS:
        readings = _name_readings(raw)

        if len(readings) == 1 and len(readings[0]) == 1:
            # A single person: offer the full ranked list of splits to pick from.
            # One already in sort order ("Ahlert, Fred E.") sorts as itself, so it
            # yields only the raw value and is dropped as redundant below.
            only = readings[0][0]
            for c in ([only] if _looks_inverted(only) else _sort_single_name(only)):
                _add(c)
        else:
            # One candidate per reading: sort each person, join with the
            # configured delimiter. A person still holding a comma is already
            # inverted and passes through as-is.
            from backtrack.config import load_config
            delim = setting(load_config(), 'sort_list_delimiter')
            for people in readings:
                _add(delim.join(p if _looks_inverted(p)
                                else (_sort_single_name(p) or [p])[0]
                                for p in people))
    else:
        # Title / album: strip leading article only.
        words = raw.split()
        if words and words[0].lower() in _ARTICLE_MAP:
            art  = _ARTICLE_MAP[words[0].lower()]
            rest = ' '.join(words[1:])
            _add(f"{rest}, {art}")

    # Ordinal-padded variants of every candidate plus the raw value.
    for base in list(candidates) + [raw]:
        padded = _pad_ordinals(base)
        _add(padded)

    return candidates


def _prompt_sort_order(base_id: str, audio: ID3) -> str | None:
    """
    If base_id is a sort tag and its source tag has a value, show sort-order
    suggestions and return the chosen prefill (or None to skip / type custom).
    Returns None immediately for non-sort tags.
    """
    source_id = _SORT_SOURCES.get(base_id)
    if not source_id:
        return None
    frame = audio.get(source_id)
    if not frame or not getattr(frame, 'text', None):
        return None
    # A source frame can hold several values (TCOM takes one composer each) while
    # the sort frame is single: join them on the configured delimiter, which is
    # also what the engine splits them back on.
    from backtrack.config import load_config
    delim = setting(load_config(), 'sort_list_delimiter')
    raw = delim.join(v for v in (str(t).strip() for t in frame.text) if v)
    if not raw:
        return None

    cands = _sort_candidates(base_id, raw)
    if not cands:
        return raw  # nothing to suggest: just prefill with the raw value

    if len(cands) == 1:
        return cands[0]

    # Multiple suggestions: let the user pick, or type their own.
    _CUSTOM = "Type custom…"
    picked = prompt.select(
        f'Sort order for “{raw}”: pick a suggestion:',
        choices=cands + [prompt.separator(), _CUSTOM],
    )
    if picked is None or picked == _CUSTOM:
        return None
    return picked


def _get_image_from_apic(apic_frame: APIC) -> tuple:
    """Decode an APIC frame's embedded image data; returns (image, mime, raw_bytes)."""
    img_data = getattr(apic_frame, 'data', b"")
    mime_type = getattr(apic_frame, 'mime', "image/jpeg")
    if not img_data:
        return None, mime_type, b""
    return decode_image(img_data), mime_type, img_data


def _open_apic_preview(apic_frame: APIC) -> bool:
    """Write the APIC image to a temp file and open it in the OS's default viewer."""
    image, mime_type, img_bytes = _get_image_from_apic(apic_frame)

    if not img_bytes or (hasattr(img_bytes, 'size') and img_bytes.size == 0) or not len(img_bytes):
        return False

    try:
        ext = {
            'image/jpeg': '.jpg', 'image/jpg': '.jpg',
            'image/png': '.png', 'image/gif': '.gif',
        }.get(mime_type, '.jpg')

        with tempfile.NamedTemporaryFile(suffix=ext, delete=False) as tmp:
            data_to_write = img_bytes.tobytes() if hasattr(img_bytes, 'tobytes') else img_bytes
            tmp.write(data_to_write)
            tmp_path = tmp.name

        if sys.platform == 'darwin':
            subprocess.run(['open', tmp_path], check=True)
        elif sys.platform == 'win32':
            os.startfile(tmp_path)
        elif sys.platform.startswith('linux'):
            subprocess.run(['xdg-open', tmp_path], check=True)
        else:
            raise OSError(f"Unsupported OS: {sys.platform}")

        return True
    except (OSError, subprocess.CalledProcessError) as e:
        ui.show_error(f"couldn't open the preview: {e}")
        return False



def _import_from_lrc(file_path: str, tag_id: str) -> None:
    """Ask for an LRC file and import it; the writing itself is `lyrics.import_lrc`."""
    default_lrc = os.path.splitext(file_path)[0] + ".lrc"
    # prompt.path, not prompt.text: this is a filesystem location, so it gets
    # Tab-completion against the directory listing like every other path field.
    lrc_path = prompt.path("LRC file path:", default=default_lrc)
    if not lrc_path or not os.path.exists(lrc_path):
        ui.show_status("File not found." if lrc_path else "Cancelled.")
        return

    written, count = _lyrics.import_lrc(file_path, lrc_path)
    if not written:
        ui.show_status("No usable lines in LRC file.")
    elif tag_id.startswith('SYLT') and written == 'USLT':
        ui.show_status("This LRC has no timestamps to import.")
    else:
        ui.show_status(f"Imported {count} lines to {written}.")


# Cover-art actions: (value, label, dim note). Plain sentence-case labels, "…"
# when the row opens a further prompt.
_APIC_ACTIONS = [
    ('open',    "Open in image viewer",  "the system default app"),
    ('replace', "Replace image…",        "pick a nearby file or type a path"),
    ('meta',    "Type and description…", None),          # note = current type
    ('export',  "Save a copy…",          None),          # note = file size
    ('remove',  "Remove image",          "deletes this frame"),
]

_APIC_ACTION_COLUMNS = [
    prompt.Column(style='primary', flex=True),                    # action
    prompt.Column(style='dynamic-dim', align='right', pin=True),  # what it does
]


_ART_BREATHING_ROWS = 0          # rows left free below the art box
# Below this the picture is mush; the facts line says more than it would.
_MIN_ART_ROWS = 6
# The facts beside the art need this many columns, or the art has the box.
_FACTS_MIN_W = 18


def _art_rows_available(reserved_rows: int) -> int:
    """Rows the art box may occupy: what's left after the chrome."""
    return _visible_rows() - reserved_rows - 2 - _ART_BREATHING_ROWS   # -2 box edges


def _art_lines_boxed(apic_frame: APIC, reserved_rows: int = 0, title: str = "") -> list[str]:
    """The picture in a box as wide as the screen's other boxes, `title` in its
    top border: a square filled edge to edge (cropped about its centre), an
    image where the terminal shows them (a prompt.Pane carrying it), else text
    art, with what the image is beside it."""
    avail_rows = _art_rows_available(reserved_rows)
    path = picture_file(getattr(apic_frame, 'data', b"") or b"")
    if avail_rows < _MIN_ART_ROWS or not path:
        # Not enough height left for art worth looking at (or nothing that
        # decodes): the facts line in the header carries the detail instead.
        why = "window too short" if path else "no picture to show"
        return [f"{' ' * ui.MARGIN_H}{C.DIM}(art hidden: {why}){C.RESET}"]

    inner = max(12, get_terminal_width() - 2 * ui.MARGIN_H - 4)
    aspect = ui.cell_aspect()
    rows = avail_rows
    cols = round(rows * aspect)
    room = inner - _FACTS_MIN_W - 3                                 # " │ " before the facts
    if cols > max(room, inner * 2 // 3 if room < 12 else room):
        cols = room if room >= 12 else inner
        rows = max(1, int(cols / aspect))
        cols = round(rows * aspect)
    image = image_cells(path, cols, rows)
    art = image[0] if image else fill_art(path, cols, rows)
    facts_w = inner - cols - 3
    facts = [f"{C.DIM}{k:<7}{C.RESET}{ui.truncate_text(v, max(1, facts_w - 7))}"
             for k, v in _apic_fact_rows(apic_frame)] if facts_w >= _FACTS_MIN_W else []
    side = [f" {C.DIM}│{C.RESET} " + (facts[i] if i < len(facts) else "") for i in range(rows)] if facts else []
    pad = " " * ui.MARGIN_H
    name = f" {ui.truncate_text(title, max(1, inner - 2))} " if title else ""
    lines = ([f"{pad}{C.DIM}╭─{C.RESET}{C.PRIMARY}{C.BOLD}{name}{C.RESET}"
              f"{C.DIM}{'─' * (inner + 1 - ui.visual_len(name))}╮{C.RESET}"]
             + [f"{pad}{C.DIM}│{C.RESET} {row}{' ' * (inner - ui.visual_len(row))} {C.DIM}│{C.RESET}"
                for row in (line + (side[i] if side else "") for i, line in enumerate(art))]
             + [f"{pad}{C.DIM}╰{'─' * (inner + 2)}╯{C.RESET}"]
             + [""] * _ART_BREATHING_ROWS)
    # The image over the art's cells: under the top border, inside "│ ".
    pictures = [(1, len(pad) + 2, rows, (path, cols), image[1], cols)] if image else []
    return prompt.Pane(lines, pictures)


def _apic_fact_rows(apic_frame: APIC) -> list[tuple[str, str]]:
    """What the image is, as (label, value) rows, most useful first."""
    from PIL import Image
    _image, mime, img_data = _get_image_from_apic(apic_frame)
    try:
        with Image.open(BytesIO(img_data)) as img:
            size, mode = img.size, img.mode
    except (OSError, ValueError, Image.DecompressionBombError):
        size = mode = None
    rows = [("Size", f"{size[0]}×{size[1]} px")] if size else []
    rows += [("Format", (mime or "").removeprefix("image/").upper() or "?"),
             ("Weight", f"{len(img_data) / 1024:.0f} KB")]
    if mode:
        rows.append(("Colour", {"L": "greyscale", "LA": "greyscale", "RGB": "RGB", "RGBA": "RGBA",
                                "CMYK": "CMYK", "P": "palette"}.get(mode, mode)))
    return rows


def _apic_facts(apic_frame: APIC, budget: int = 999) -> str:
    """What the image *is*, on one line: size, format, weight, colour mode.

    Least useful facts drop first (colour mode, then format, then weight) so the
    line fits `budget` columns instead of wrapping.
    """
    bits = [v for _k, v in _apic_fact_rows(apic_frame)]
    while bits and len(" · ".join(bits)) > budget:
        bits.pop()
    return " · ".join(bits)


def _edit_apic_tag(audio_obj: ID3, tag_name: str, apic_frame: APIC,
                   file_path: str = "") -> bool:
    """View and edit one APIC (cover art) frame. Returns True if anything changed.

    One screen: the art with a facts line above it and the actions below. Saving
    is left to the caller, which also refreshes the library entry.
    """
    changed = False
    # mutagen keys an APIC frame by its description ("APIC:Back sleeve"), so the
    # key moves whenever the description changes. Track it, or a second edit
    # deletes by a stale key and leaves the earlier frame behind.
    key = tag_name

    # Rows this screen spends on anything but the art: the boxed header (3 + a
    # blank), the prompt message, the two above/below indicators, the actions and
    # the hint bar. The art gets what's left.
    _CHROME_ROWS = 4 + 1 + 2 + len(_APIC_ACTIONS) + 2

    def _apic_header() -> list[str]:
        """One boxed line of everything the image is, then the centred art."""
        pic_type = picture_type_name(getattr(apic_frame, 'type', 3))
        desc = (getattr(apic_frame, 'desc', '') or "").strip()

        # The base frame id only: mutagen keys APIC frames by description, so
        # display_tag_id(tag_name) would print the description a second time.
        tag_txt = parse_composite_tag_id(key)[0] or "APIC"
        detail = f" · {pic_type}" + (f" · “{desc}”" if desc else "")
        left_plain = tag_txt + detail

        inner = max(12, get_terminal_width() - 2 * ui.MARGIN_H - 4)
        toggle_w = prompt.help_toggle_width() + 2           # the room the header keeps for the toggle
        art = _art_lines_boxed(apic_frame, _CHROME_ROWS) if _visible_rows() >= 14 else None
        shown = isinstance(art, prompt.Pane)                 # the facts are beside it
        lines = prompt.rounded_header(
            tag_txt, detail,
            "" if shown else _apic_facts(apic_frame, max(0, inner - len(left_plain) - 2 - toggle_w)))
        if art:
            pictures = [(len(lines) + p[0], *p[1:]) for p in getattr(art, 'pictures', ())]
            return prompt.Pane(lines + list(art), pictures)
        return lines

    def _replace_frame(data: bytes, mime: str, pic_type: int, desc: str) -> bool:
        """Swap this frame for a rebuilt one, re-keying it (mutagen keys APIC
        frames by description, so editing `.desc` in place left a stale key)."""
        nonlocal apic_frame, key
        new_frame = create_apic_frame(data, mime, pic_type, desc)
        if new_frame is None:
            ui.show_error("couldn't build the image frame")
            return False
        audio_obj.delall(key)
        audio_obj.add(new_frame)
        apic_frame = new_frame
        key = getattr(new_frame, 'HashKey', key)     # follow the frame's new key
        return True

    while True:
        kb = len(getattr(apic_frame, 'data', b"") or b"") / 1024
        notes = {'meta': picture_type_name(getattr(apic_frame, 'type', 3)),
                 'export': f"{kb:.0f} KB"}
        choices = [prompt.Choice(title=label, value=value,
                                 cells=[label, note or notes.get(value, "")])
                   for value, label, note in _APIC_ACTIONS]

        action = prompt.select("Cover art:", choices=choices,
                               columns=_APIC_ACTION_COLUMNS, header=_apic_header)

        if not action:
            return changed

        if action == 'open':
            if _open_apic_preview(apic_frame):
                ui.show_status("Opening…")
            else:
                ui.show_error("couldn't open the image")

        elif action == 'replace':
            # The same ranked picker the tag editor's image field uses: nearby
            # covers first, with a "type a path" escape.
            img_path = pick_nearby_cover(file_path) if file_path else prompt.path("Image file:")
            if not isinstance(img_path, str) or not img_path:
                continue
            if not os.path.isfile(img_path):
                ui.show_status("File not found.")
                continue
            try:
                with open(img_path, 'rb') as f:
                    new_data = f.read()
            except OSError as e:
                ui.show_error(f"couldn't read the image: {e}")
                continue
            mime = _EXT_TO_MIME.get(os.path.splitext(img_path)[1].lower(), 'image/jpeg')
            # Keep the type and description; only the picture changes.
            if _replace_frame(new_data, mime,
                              getattr(apic_frame, 'type', 3),
                              getattr(apic_frame, 'desc', '') or ''):
                changed = True
                ui.show_status("Image replaced.")

        elif action == 'meta':
            meta = _prompt_for_image_metadata(
                initial_type=getattr(apic_frame, 'type', 3),
                initial_desc=getattr(apic_frame, 'desc', '') or '',
                header=_apic_header)
            if meta is None:
                continue
            pic_type, desc = meta
            if _replace_frame(getattr(apic_frame, 'data', b""),
                              getattr(apic_frame, 'mime', 'image/jpeg'),
                              pic_type, desc):
                changed = True
                ui.show_status(f"{picture_type_name(pic_type)} · "
                                     f"{desc if desc else 'no description'}")

        elif action == 'export':
            data = getattr(apic_frame, 'data', b"") or b""
            mime = getattr(apic_frame, 'mime', 'image/jpeg')
            ext = next((e for e, m in _EXT_TO_MIME.items() if m == mime), '.jpg')
            default = os.path.join(os.path.dirname(file_path) if file_path else os.getcwd(),
                                   f"cover{ext}")
            dest = prompt.path("Save the image to:", default=default)
            if not dest:
                continue
            dest = os.path.abspath(os.path.expanduser(dest))
            if os.path.exists(dest) and not prompt.confirm(
                    f"Overwrite {os.path.basename(dest)}?"):
                continue
            try:
                with open(dest, 'wb') as f:
                    f.write(data)
                ui.show_status(f"Saved to {os.path.basename(dest)}.")
            except OSError as e:
                ui.show_error(f"couldn't save the image: {e}")

        elif action == 'remove':
            if prompt.confirm(f"Remove {display_tag_id(key)}? Cannot be undone."):
                audio_obj.delall(key)
                ui.show_status("Image removed.")
                return True


def _file_facts(file_path: str, library_metadata: dict | None) -> tuple[float, str]:
    """The file's length and its audio's format label, from the library (else
    the file): read once per editor visit, not per render (the header is
    redrawn on every keypress/resize)."""
    entry = library_metadata if (library_metadata or {}).get("format") else library_entry(file_path)
    try:
        dur = float(entry.get("duration") or 0.0)
    except (TypeError, ValueError):
        dur = 0.0
    return dur, format_label(entry)


def _file_header(file_path: str, title: str, artist: str, duration: float, fmt: str = "") -> list[str]:
    """The boxed title/artist/format/duration/size header of a tag editor screen."""
    ext = fmt or os.path.splitext(file_path)[1].upper().lstrip('.')
    try:
        size_str = f"  {os.path.getsize(file_path) / (1024*1024):.1f} MB"
    except OSError:
        size_str = ""
    dur_str = f"  {ui.format_time(int(duration))}" if duration else ""
    return prompt.rounded_header(title or track_title(file_path), f" · {artist}" if artist else "",
                                 f"{ext}{dur_str}{size_str}")


def _refresh_entry(file_path: str, library: list | None, library_metadata: dict | None) -> None:
    """Bring the in-memory library entry up to date after a tag save."""
    if library is None:
        return
    try:
        fresh = refresh_library_entry(library, file_path)
        if library_metadata is not None:
            library_metadata.update(fresh)
    except (OSError, KeyError) as e:
        ui.show_error(f"couldn't update the library cache: {e}")


def _filepath_row():
    """The read-only "File path" row heading a tag editor's list."""
    return prompt.Choice(title="File path", value="__filepath__",
                         cells=[[("File path", 'primary'), (" (filesystem)", 'dynamic-dim')], "", ""])


def _show_filepath(file_path: str) -> None:
    """The file path row's screen: where the file is, and copying it."""
    header = lambda: prompt.path_box(file_path, "File path")   # noqa: E731 (re-wrapped at each width)
    if prompt.select("", choices=["Copy path to clipboard"], header=header) == "Copy path to clipboard":
        pyperclip.copy(file_path)
        ui.show_status("File path copied.")


def inspect_tag_loop(
    file_path: str,
    library_metadata: dict | None = None,
    library: list | None = None
) -> None:
    """Interactive top-level loop for browsing and editing all ID3 tags on an MP3 file."""
    # Tag editing is ID3/MP3-only. Refuse other containers up front (including an
    # mp4/m4a that happens to carry a stray ID3 header, which would otherwise slip
    # past the per-load check and crash downstream): one clean, early message.
    kind = tag_kind(file_path)
    if kind == 'unsupported':
        ui.show_status("This file type has nowhere to keep tags.", duration=tune.STATUS_WARNING_S)
        return
    if not drop_moved([file_path]):
        return
    if kind != 'id3':
        _inspect_kv_loop(file_path, library_metadata, library)
        return

    _cached_dur, _fmt = _file_facts(file_path, library_metadata)

    from backtrack.config import load_config
    _cfg = load_config()
    _has_lyrics = setting(_cfg, "show_lyrics_editor") and bool(find_lyrics(file_path))
    _has_trim = _trim.HAS_FFMPEG and is_mp3(file_path)        # a frame-exact MPEG cut

    def _save(audio_obj):
        """Persist tags and refresh the in-memory library cache entry for this file."""
        save_id3(audio_obj, file_path)      # explicit path: works for a fresh ID3 too
        _refresh_entry(file_path, library, library_metadata)

    def _main_header() -> list[str]:
        """Build the boxed title/artist/format/duration/size header for the tag list screen."""
        # Prefer real tags over the (possibly cryptic) filename. Read live from
        # the loaded ID3 object, falling back to the cached library metadata,
        # and only to the filename when there is no title at all.
        meta = library_metadata or {}
        _PLACEHOLDERS = {"", "Unknown Artist", "Unknown Album", "Unknown Genre", "Unknown Year"}

        def _from_tags(frame: str, meta_key: str) -> str:
            """Read a live tag value, falling back to cached library metadata (placeholders excluded)."""
            with quietly():
                fr = audio.get(frame)
                val = first_text(fr)
                if val:
                    return val
            val = str(meta.get(meta_key, "")).strip()
            return "" if val in _PLACEHOLDERS else val

        title = _from_tags("TIT2", "title")
        artist = _from_tags("TPE1", "artist") or _from_tags("TPE2", "album_artist")
        return _file_header(file_path, title, artist, _cached_dur, _fmt)

    while True:
        try:
            audio = load_id3(file_path)   # untagged: a fresh tag (WAV/AIFF: in their ID3 chunk)
        except (OSError, ValueError, mutagen.MutagenError) as e:
            ui.show_error(f"couldn't open the file: {e}")
            break
        tags = sorted(audio.keys())

        def _tag_cells(tag_id: str) -> list:
            """Build the [id+name, category, value] row cells for one tag in the list."""
            # Column 1 = TAG (bright) + friendly name (dim) as two segments.
            info = get_tag_info(tag_id)
            friendly = f" ({info.name[0]})" if info else ""
            category = get_tag_category(tag_id)
            val = summarize_tag_value(tag_id, audio[tag_id], display=True)
            return [[(display_tag_id(tag_id), 'primary'), (friendly, 'dynamic-dim')], category, val]

        # Read-only filesystem path row: "File path" white (bold when active),
        # "(filesystem)" dimmed, both in column 1.
        non_id3_rows = [_filepath_row()]
        if _has_lyrics:
            # Same shape as the file-path row: a non-ID3 thing that lives with
            # this one track, not a tag: lyrics/transcript sync has its own
            # editor (lyrics_editor), reached here rather than as a separate
            # top-level track action.
            non_id3_rows.append(prompt.Choice(
                title="Lyrics", value="__lyrics__",
                cells=[[("Lyrics", 'primary'), (" (sync editor)", 'dynamic-dim')], "", ""],
            ))
        tag_choices = non_id3_rows + [prompt.separator()] + [
            prompt.Choice(title=t, value=t, cells=_tag_cells(t)) for t in tags
        ]
        has_id3 = tag_kind(file_path) == 'id3'
        _shortcuts = {'tags.add': 'Add Tag'} if has_id3 else {}
        _extra_hints = {'tags.add': 'add tag'} if has_id3 else {}
        if _has_trim:
            # A keyboard shortcut rather than a row: "Trim" as a row here would
            # read like a tag, not an action, so the footer hint spells it out.
            _shortcuts['tags.trim'] = '__trim__'
            _extra_hints['tags.trim'] = 'trim audio'

        choice = prompt.select(
            "Select tag to manage:",
            choices=tag_choices,
            header=_main_header,
            shortcuts=_shortcuts or None,
            extra_hints=_extra_hints or None,
            columns=_TAG_COLUMNS,
        )

        if choice == "__lyrics__":
            ui.clear_screen()
            lyrics_editor(file_path)
            ui.clear_screen()
            continue

        if choice == "__trim__":
            ui.clear_screen()
            trim_editor(file_path, library)
            ui.clear_screen()
            # A commit changes the file's duration: refresh the cached value
            # this screen's header reads, same as `_save` does after a tag write.
            if library is not None:
                with quietly():
                    fresh = refresh_library_entry(library, file_path)
                    if library_metadata is not None:
                        library_metadata.update(fresh)
                    _cached_dur = float(fresh.get("duration") or 0.0)
            continue

        if choice == "__filepath__":
            _show_filepath(file_path)
            continue

        if choice == "Add Tag":
            tag_id = tag_id_input("Tag ID:", audio)
            if not tag_id:
                continue

            # Resolve the base frame of a composite id
            base_id, _, _ = parse_composite_tag_id(tag_id)
            info = get_tag_info(base_id)

            if info:
                value = prompt_for_value(base_id, current_value=_prompt_sort_order(base_id, audio),
                                         file_path=file_path)
            else:
                value = prompt.text(f"Value for {tag_id}:")

            if value is not None:
                new_frame = create_frame(tag_id, value)
                if new_frame:
                    audio.add(new_frame)
                    _save(audio)
                    ui.show_status(f"Added {tag_id}.")
                else:
                    ui.show_error(f"couldn't create {tag_id}")
            continue

        if not choice:
            break

        while True:
            audio = load_id3(file_path)
            if choice not in audio:
                break

            raw_val = audio[choice]
            category = get_tag_category(choice)

            info = get_tag_info(choice) if choice else None
            tag_title = f"{display_tag_id(choice or '')} ({info.name[0] if info else choice or 'Unknown'})"

            def _tag_header() -> list[str]:
                """The single-tag screen's header, over the actions' box: the
                picture, or the people, in a box titled with the tag."""
                if category == 'image':
                    # header + message + indicators + up to 6 action rows + the
                    # boxes' edges + hints. A literal, not len(actions): this
                    # closure is defined before that list is built.
                    return _art_lines_boxed(raw_val, 1 + 2 + 6 + 4 + 2, tag_title)
                cols = ui.get_terminal_width() - 2 * ui.MARGIN_H - 4      # inside the box
                people = getattr(raw_val, 'people', [])
                cw = max(12, (cols - 2) // 2)
                lines = [f"{C.DIM}{'ROLE':<{cw}}  NAME{C.RESET}", f"{'─' * cw}  {'─' * (cols - cw - 2)}"]
                lines += [f"{ui.truncate_text(role, cw):<{cw}}  {ui.truncate_text(name, cols - cw - 2)}"
                          for role, name in people[:8]]
                if len(people) > 8:
                    lines.append(f"{C.DIM}… +{len(people) - 8} more{C.RESET}")
                return prompt.box_lines(lines, cols + 4, len(lines) + 2, tag_title)

            actions = ["Copy", "Paste", "Edit", "Rename", "Delete"]
            if category in ('lyrics',) and choice.startswith(('USLT', 'SYLT')):
                actions.insert(0, "Import LRC")
            if category == 'image':
                actions.remove("Edit")
                actions.insert(0, "Manage")
            if choice.startswith('SYLT'):
                actions.remove("Edit")

            action = prompt.select("Action:", choices=actions,
                                   header=(_tag_header if category in ('image', 'people')
                                           else prompt.PanelTitle(tag_title)))

            if action == "Manage" and category == 'image':
                if _edit_apic_tag(audio, choice, raw_val, file_path):
                    _save(audio)
                break

            elif action == "Import LRC":
                _import_from_lrc(file_path, choice)
                break

            elif action == "Copy":
                if category == 'people':
                    text = people_to_text(getattr(raw_val, 'people', []))
                else:
                    text = summarize_tag_value(choice, raw_val)
                pyperclip.copy(text)
                ui.show_status("Copied to clipboard.")

            elif action == "Paste":
                clipboard = pyperclip.paste()
                if clipboard and prompt.confirm(f"Replace {choice}?"):
                    # Credits were copied as "role: name" lines: read them back.
                    value = people_from_text(clipboard) if category == 'people' else clipboard
                    new_frame = create_frame(choice, value)
                    if new_frame:                 # only then drop the old one
                        audio.delall(choice)
                        audio.add(new_frame)
                        _save(audio)
                        ui.show_status("Updated.")
                    else:
                        ui.show_error("that value doesn't fit this tag")

            elif action == "Rename":
                new_id = tag_id_input("New tag ID:", audio, like=choice)
                if new_id and new_id != choice:
                    old_frame = audio.pop(choice)
                    if rename_frame(audio, old_frame, new_id):
                        _save(audio)
                        ui.show_status(f"Renamed to {new_id}.")
                    else:
                        audio.add(old_frame)
                        ui.show_status(
                            f"{new_id} is already set on this file: delete or edit it instead."
                            if rename_would_replace(audio, new_id) else "Rename failed.")
                    break

            elif action == "Edit":
                current_frame = audio.get(choice)
                new_value = prompt_for_value(choice, current_value=current_frame,
                                             file_path=file_path)
                if new_value is not None:
                    new_frame = create_frame(choice, new_value)
                    if new_frame:
                        audio.add(new_frame)
                        _save(audio)
                        ui.show_status("Updated.")
                    else:
                        ui.show_error("that value doesn't fit this tag")
                    break

            elif action == "Delete":
                if prompt.confirm(f"Delete {choice}?"):
                    try:
                        audio.pop(choice)
                        _save(audio)
                        ui.show_status(f"Deleted {choice}.")
                    except KeyError:
                        ui.show_error(f"couldn't delete {choice}")
                    break

            elif not action:
                break


def _inspect_kv_loop(file_path: str, library_metadata: dict | None, library: list | None) -> None:
    """The tag editor for an m4a/mp4 (MP4 atoms) or FLAC/Ogg/Opus (Vorbis
    comments): every tag as a key and its values, plus the cover. Built from the
    ID3 editor's pieces; the ID3-only actions (lyric sync, trim, frame editors)
    aren't offered."""
    from backtrack.id3 import cover_matcher as cm
    from backtrack.id3 import tag_formats as tf
    from backtrack.id3 import tag_writer as tw

    kind = tf.kind(file_path)
    duration, fmt = _file_facts(file_path, library_metadata)

    def _edit_values(key: str, values: list[str]) -> list[str] | None:
        """New values for a key: one line, several in a list, prose (lyrics) in the text box."""
        if any("\n" in v for v in values) or key.upper() in ('LYRICS', 'UNSYNCEDLYRICS', '\xa9LYR'):
            text = prompt.multiline(f"{key}:", "\n".join(values))
            return None if text is None else [text.rstrip("\n")]
        if len(values) > 1:
            rows = prompt.list_edit(f"{key}:", [[v] for v in values], ("VALUE",))
            return None if rows is None else [r[0] for r in rows]
        new = prompt.text(f"{key}:", default=values[0] if values else "")
        return None if new is None else [new]

    def _store(key: str, values: list[str] | None, done: str) -> None:
        if values is None:
            return
        values = [v for v in values if v.strip()]
        try:
            tags, save = tf.open_tags(file_path)
            tf.kv_set(tags, key, values) if values else tf.kv_delete(tags, key)
            save()
        except (ValueError, OSError, mutagen.MutagenError) as e:
            ui.show_error(f"not saved: {e}")
            return
        _refresh_entry(file_path, library, library_metadata)
        ui.show_status(done)

    while True:
        try:
            tags, save = tf.open_tags(file_path)
        except (OSError, ValueError, mutagen.MutagenError) as e:
            ui.show_error(f"couldn't open the file: {e}")
            return
        items = dict(tf.kv_items(tags))
        title = (tf.texts(tags, 'title') or [''])[0]
        artist = (tf.texts(tags, 'artist') or tf.texts(tags, 'album_artist') or [''])[0]
        rows = [_filepath_row(),
                prompt.Choice(title="Cover art", value="__cover__",
                              cells=[[("Cover art", 'primary'), (" (picture)", 'dynamic-dim')], "",
                                     "embedded" if tw.has_cover(file_path) else "none"]),
                prompt.separator()]
        for key, values in items.items():
            label = tf.label_for(kind, key)
            summary = "; ".join(v.replace("\n", "\\") for v in values)
            rows.append(prompt.Choice(title=key, value=key, cells=[
                [(key, 'primary'), (f" ({label})" if label else "", 'dynamic-dim')], "", summary]))

        choice = prompt.select("Select tag to manage:", choices=rows,
                               header=lambda: _file_header(file_path, title, artist, duration, fmt),
                               shortcuts={'tags.add': 'Add Tag'}, extra_hints={'tags.add': 'add tag'},
                               columns=_TAG_COLUMNS)
        if not choice:
            return
        if choice == "__filepath__":
            _show_filepath(file_path)
        elif choice == "__cover__":
            act = prompt.select("Cover art:", choices=["Replace", "Remove"])
            if act == "Replace":
                picked = pick_nearby_cover(file_path)
                read = cm.read_image(picked) if isinstance(picked, str) else None
                res = tw.write_cover(file_path, read[0], read[1], overwrite=True) if read else None
                if res is not None and res.changed:
                    ui.show_status("Cover replaced.")
                elif res is not None:
                    ui.show_error("MP4 covers must be JPEG or PNG" if res.skipped_format else f"not saved: {res.error}")
            elif act == "Remove" and prompt.confirm("Remove the cover?"):
                res = tw.remove_cover(file_path)
                if res.error:
                    ui.show_error(f"not saved: {res.error}")
                else:
                    ui.show_status("Cover removed.")
            if act:
                _refresh_entry(file_path, library, library_metadata)
        elif choice == "Add Tag":
            key = tag_key_input("Tag key:", kind) or ""
            if key in items:
                ui.show_status(f"{key} is already set: edit it instead.")
            elif key:
                _store(key, _edit_values(key, []), f"Added {key}.")
        else:
            values = items[choice]
            act = prompt.select("", choices=["Copy", "Paste", "Edit", "Rename", "Delete"],
                                header=prompt.PanelTitle(choice, tf.label_for(kind, choice)))
            if act == "Copy":
                pyperclip.copy("\n".join(values))
                ui.show_status("Copied to clipboard.")
            elif act == "Paste":
                clip = pyperclip.paste()
                if clip and prompt.confirm(f"Replace {choice}?"):
                    _store(choice, [clip], "Updated.")
            elif act == "Edit":
                _store(choice, _edit_values(choice, values), "Updated.")
            elif act == "Rename":
                new_key = tag_key_input("New tag key:", kind) or ""
                if new_key in items or (kind == 'vorbis' and new_key.upper() in items):
                    ui.show_status(f"{new_key} is already set on this file: delete or edit it instead.")
                elif new_key and new_key != choice:
                    try:
                        tf.kv_set(tags, new_key, values)
                        tf.kv_delete(tags, choice)
                        save()
                    except (ValueError, OSError, mutagen.MutagenError) as e:
                        ui.show_error(f"not saved: {e}")
                        continue
                    _refresh_entry(file_path, library, library_metadata)
                    ui.show_status(f"Renamed to {new_key}.")
            elif act == "Delete" and prompt.confirm(f"Delete {choice}?"):
                _store(choice, [], f"Deleted {choice}.")
