"""RSS feed ingest: parse, normalise, dedupe, download.

A podcast feed is a list of episodes whose titles were written by hand, over
years, by different people. This module turns that into something the library
can hold: it parses the XML, pulls the show, series, episode and date out of
whatever shape the title happens to be in, and keeps the raw title alongside so
nothing is lost to a guess.

Pure except for `fetch` (one urllib call) and `sync` (which downloads). The
parsing and normalisation take strings and return dataclasses, so the awkward
real-world title shapes are unit-tested without a network.

Nothing here puts a file into the library: `sync` writes the audio into a music
directory and calls `music_library.refresh_library_entry`, which is the path the
app already uses for a file that has appeared.
"""
from __future__ import annotations

import json
import os
import re
import unicodedata
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone

_ITUNES = '{http://www.itunes.com/dtds/podcast-1.0.dtd}'

# How long to wait for a feed or an enclosure before giving up.
FETCH_TIMEOUT_S = 30.0
# Read size for a streamed download.
_CHUNK = 65536


# --- title normalisation ----------------------------------------------------

_MONTHS = {name.lower(): number for number, name in enumerate(
    ('january', 'february', 'march', 'april', 'may', 'june', 'july',
     'august', 'september', 'october', 'november', 'december'), start=1)}
_MONTHS.update({name[:3]: number for name, number in list(_MONTHS.items())})

# "Ep 2." / "Ep5." / "Ep. 12" — the separator before it is whatever the writer
# felt like that week, including nothing at all.
_EPISODE_RE = re.compile(
    r'^(?P<show>.+?)[\s:\u2013\u2014-]*\bEp\.?\s*(?P<episode>\d+)\b\s*[.:]?\s*(?P<title>.*)$',
    re.IGNORECASE)

# "Show - 6. Title" — a bare number where another feed would write "Ep".
_NUMBERED_RE = re.compile(
    r'^(?P<show>.+?)\s*[\u2013\u2014-]\s*(?P<episode>\d+)\.\s+(?P<title>.+)$')

# "Show - 31st May" / "Show 25th February 2022". The year is often absent.
_DATE_RE = re.compile(
    r'^(?P<show>.+?)\s*[\u2013\u2014-]?\s*'
    r'(?P<day>\d{1,2})(?:st|nd|rd|th)\s+(?P<month>[A-Za-z]+)'
    r'(?:\s+(?P<year>\d{4}))?\s*$', re.IGNORECASE)

_SERIES_RE = re.compile(r'\bseries\s+(\d+)\b', re.IGNORECASE)


def normalise_whitespace(text: str) -> str:
    """Collapse every kind of space into single spaces and trim.

    Real feeds carry literal tabs mid-title ("Dead Ringers: Ep1.\\tKeir vs
    Kemi"), non-breaking spaces from a CMS, and double spaces from a typo. All
    three read as one space.
    """
    if not text:
        return ''
    text = unicodedata.normalize('NFC', text)
    text = text.replace('\u00a0', ' ').replace('\u200b', '')
    return re.sub(r'\s+', ' ', text).strip()


@dataclass
class ParsedTitle:
    """What a title turned out to say.

    `raw` is always the title as published — every other field is a reading of
    it, and a wrong reading should never cost the original.

    `date_source` says where the date came from, because a feed's `pubDate` is
    frequently the upload time rather than the broadcast date:

    * ``title``      — day, month and year all came from the title
    * ``title+feed`` — day and month from the title, year from `pubDate`
    * ``feed``       — nothing datelike in the title, so `pubDate` it is
    * ``''``         — no date at all
    """
    raw: str
    title: str = ''
    show: str = ''
    series: int | None = None
    episode: int | None = None
    date: str = ''                 # ISO yyyy-mm-dd, or '' when unknown
    date_source: str = ''


def parse_title(raw: str, pub_date: datetime | None = None) -> ParsedTitle:
    """Read a feed item's title into show, series, episode, title and date.

    Tries the shapes real feeds use, most specific first, and falls back to
    treating the whole string as the title rather than inventing structure.
    """
    text = normalise_whitespace(raw)
    parsed = ParsedTitle(raw=raw, title=text)
    if not text:
        return _with_feed_date(parsed, pub_date)

    series = _SERIES_RE.search(text)
    if series:
        parsed.series = int(series.group(1))

    match = _EPISODE_RE.match(text) or _NUMBERED_RE.match(text)
    if match:
        parsed.show = _clean_show(match.group('show'), parsed.series)
        parsed.episode = int(match.group('episode'))
        parsed.title = normalise_whitespace(match.group('title'))
        return _with_feed_date(parsed, pub_date)

    match = _DATE_RE.match(text)
    if match:
        month = _MONTHS.get(match.group('month').lower())
        if month:
            parsed.show = _clean_show(match.group('show'), parsed.series)
            # The date *is* the episode title here ("31st May"), kept as written.
            # Leaving it empty put the whole raw title in, show prefix and all,
            # so a track row read "Dead Ringers — Dead Ringers - 31st May".
            parsed.title = normalise_whitespace(text[match.start('day'):])
            day = int(match.group('day'))
            year = match.group('year')
            if year:
                parsed.date = f"{int(year):04d}-{month:02d}-{day:02d}"
                parsed.date_source = 'title'
            elif pub_date is not None:
                # The title names the broadcast day; only the year is missing,
                # and the feed's own year is the one reliable part of pubDate.
                parsed.date = f"{pub_date.year:04d}-{month:02d}-{day:02d}"
                parsed.date_source = 'title+feed'
            return parsed if parsed.date else _with_feed_date(parsed, pub_date)

    return _with_feed_date(parsed, pub_date)


def _clean_show(text: str, series: int | None = None) -> str:
    """Tidy a captured show prefix.

    Trailing punctuation is a separator, not part of the name. A series marker
    is its own field, so "A Show Series 2" is the show "A Show" — leaving it in
    filed each series under its own album.
    """
    cleaned = normalise_whitespace(text)
    if series is not None:
        cleaned = _SERIES_RE.sub('', cleaned)
    return normalise_whitespace(cleaned).rstrip(' :-\u2013\u2014')


def _with_feed_date(parsed: ParsedTitle, pub_date: datetime | None) -> ParsedTitle:
    """Fall back to the feed's own date, recording that that is what happened."""
    if pub_date is not None and not parsed.date:
        parsed.date = pub_date.date().isoformat()
        parsed.date_source = 'feed'
    return parsed


# --- feed parsing -----------------------------------------------------------

@dataclass
class Item:
    """One episode, as published and as read."""
    guid: str = ''
    url: str = ''
    length: int = 0
    mime: str = ''
    pub_date: str = ''
    description: str = ''
    duration: str = ''
    parsed: ParsedTitle = field(default_factory=lambda: ParsedTitle(raw=''))

    @property
    def key(self) -> str:
        """What identity means for this item.

        The GUID when the feed gives one, else the enclosure URL. Re-running a
        sync must not download anything twice, and a feed that reuses a URL
        under two GUIDs is far rarer than one with no GUID at all.
        """
        return self.guid or self.url

    def as_dict(self) -> dict:
        """A flat, JSON-able view — the shape `feed fetch --json` prints."""
        body = {k: v for k, v in asdict(self).items() if k != 'parsed'}
        body.update(asdict(self.parsed))
        body['key'] = self.key
        return body


@dataclass
class Feed:
    """A parsed feed: what the channel says, and its items."""
    title: str = ''
    link: str = ''
    description: str = ''
    items: list = field(default_factory=list)


def _pub_date(text: str) -> datetime | None:
    """Parse an RFC-822 pubDate, tolerating the forms feeds actually emit."""
    if not text:
        return None
    from email.utils import parsedate_to_datetime
    try:
        parsed = parsedate_to_datetime(text.strip())
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def parse_feed(data: bytes | str) -> Feed:
    """Parse RSS bytes (or a path) into a `Feed`.

    Raises `ValueError` on anything that is not parseable RSS, so a caller can
    tell "the server sent us an error page" from "the feed has no episodes".
    """
    if isinstance(data, str) and os.path.exists(data):
        with open(data, 'rb') as handle:
            data = handle.read()
    try:
        root = ET.fromstring(data)
    except ET.ParseError as exc:
        raise ValueError(f"not valid XML: {exc}") from exc

    channel = root.find('channel')
    if channel is None:
        raise ValueError("no <channel> — is this an RSS feed?")

    feed = Feed(title=normalise_whitespace(channel.findtext('title') or ''),
                link=(channel.findtext('link') or '').strip(),
                description=normalise_whitespace(
                    channel.findtext('description') or ''))

    for node in channel.findall('item'):
        enclosure = node.find('enclosure')
        published = _pub_date(node.findtext('pubDate') or '')
        raw_title = node.findtext('title') or ''
        try:
            length = int((enclosure.get('length') or 0) if enclosure is not None else 0)
        except (TypeError, ValueError):
            length = 0
        feed.items.append(Item(
            guid=(node.findtext('guid') or '').strip(),
            url=(enclosure.get('url') or '').strip() if enclosure is not None else '',
            length=length,
            mime=(enclosure.get('type') or '').strip() if enclosure is not None else '',
            pub_date=published.isoformat() if published else '',
            description=normalise_whitespace(node.findtext('description') or ''),
            duration=(node.findtext(f'{_ITUNES}duration') or '').strip(),
            parsed=parse_title(raw_title, published),
        ))
    return feed


def matching(items: list, substring: str = '') -> list:
    """Items whose raw title contains `substring`, case-insensitively.

    Matched against the raw title rather than the parsed one: the thing a person
    types a filter from is what they saw in their podcast app.
    """
    if not substring:
        return list(items)
    needle = substring.casefold()
    return [i for i in items if needle in i.parsed.raw.casefold()]


# --- stored feeds -----------------------------------------------------------

def _state_path():
    """Where subscribed feeds and their seen-item keys live."""
    from src.config import CONFIG_DIR
    return CONFIG_DIR / 'feeds.json'


def load_feeds() -> dict:
    """Every stored feed, keyed by its slug. `{}` when there are none."""
    path = _state_path()
    try:
        with open(path, 'r', encoding='utf-8') as handle:
            stored = json.load(handle)
    except (OSError, ValueError):
        return {}
    return stored if isinstance(stored, dict) else {}


def save_feeds(feeds: dict) -> None:
    """Write the feed store back, atomically."""
    path = _state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix('.tmp')
    with open(tmp, 'w', encoding='utf-8') as handle:
        json.dump(feeds, handle, indent=2, sort_keys=True)
    os.replace(tmp, path)


def slugify(text: str) -> str:
    """A short, filesystem- and command-line-safe name for a feed."""
    text = unicodedata.normalize('NFKD', text or '')
    text = ''.join(c for c in text if not unicodedata.combining(c))
    text = re.sub(r'[^A-Za-z0-9]+', '-', text).strip('-').lower()
    return text[:48] or 'feed'


def new_seen() -> list:
    """The empty seen-list a freshly added feed starts with."""
    return []


# --- network ----------------------------------------------------------------

def fetch(url: str, timeout: float = FETCH_TIMEOUT_S) -> bytes:
    """Download a URL's body. Raises `OSError` with a readable message."""
    import urllib.error
    import urllib.request

    request = urllib.request.Request(
        url, headers={'User-Agent': 'backtrack/0.1 (+podcast feed reader)'})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.read()
    except urllib.error.HTTPError as exc:
        raise OSError(f"{exc.code} {exc.reason}") from exc
    except urllib.error.URLError as exc:
        raise OSError(str(exc.reason)) from exc


def download(url: str, target: str, timeout: float = FETCH_TIMEOUT_S,
             on_progress=None) -> int:
    """Stream a URL to `target`, via a temp file so a part-download is never
    mistaken for a finished one. Returns the byte count.

    `on_progress(done, total)` is called as it goes; `total` is 0 when the
    server does not say.
    """
    import urllib.error
    import urllib.request

    request = urllib.request.Request(
        url, headers={'User-Agent': 'backtrack/0.1 (+podcast feed reader)'})
    os.makedirs(os.path.dirname(os.path.abspath(target)), exist_ok=True)
    tmp = target + '.part'
    done = 0
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            try:
                total = int(response.headers.get('Content-Length') or 0)
            except (TypeError, ValueError):
                total = 0
            with open(tmp, 'wb') as handle:
                while True:
                    chunk = response.read(_CHUNK)
                    if not chunk:
                        break
                    handle.write(chunk)
                    done += len(chunk)
                    if on_progress is not None:
                        on_progress(done, total)
    except urllib.error.HTTPError as exc:
        _discard(tmp)
        raise OSError(f"{exc.code} {exc.reason}") from exc
    except urllib.error.URLError as exc:
        _discard(tmp)
        raise OSError(str(exc.reason)) from exc
    except BaseException:
        _discard(tmp)
        raise
    os.replace(tmp, target)
    return done


def _discard(path: str) -> None:
    """Remove a part-file, ignoring the case where it never existed."""
    try:
        os.remove(path)
    except OSError:
        pass


# --- naming -----------------------------------------------------------------

_ILLEGAL = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def safe_name(text: str) -> str:
    """One path component, with the characters a filesystem objects to removed."""
    cleaned = _ILLEGAL.sub('', normalise_whitespace(text)).strip(' .')
    return cleaned[:120] or 'untitled'


def extension_for(item: Item) -> str:
    """The file extension an enclosure should land under."""
    from urllib.parse import urlparse

    guess = os.path.splitext(urlparse(item.url).path)[1].lower()
    if guess in ('.mp3', '.m4a', '.mp4', '.m4p', '.aac'):
        return guess
    return {'audio/mp4': '.m4a', 'audio/x-m4a': '.m4a',
            'audio/aac': '.aac'}.get(item.mime.lower(), '.mp3')


def target_path(item: Item, root: str, feed_title: str = '') -> str:
    """Where one episode's audio should be written.

    ``<root>/<show>/<episode> - <title>.<ext>``, with the show falling back to
    the feed's own title when a particular item's prefix could not be read.
    """
    parsed = item.parsed
    show = safe_name(parsed.show or feed_title or 'Podcast')
    stem_parts = []
    if parsed.episode is not None:
        stem_parts.append(f"{parsed.episode:02d}")
    stem_parts.append(parsed.title or parsed.date or normalise_whitespace(parsed.raw))
    stem = safe_name(' - '.join(p for p in stem_parts if p))
    return os.path.join(root, show, stem + extension_for(item))


def tag_plan(item: Item, feed_title: str = '') -> tuple[dict, dict]:
    """The tags an imported episode should carry: `(fields, frames)`.

    `fields` are `tag_writer`'s cross-format ones, written to MP3 and MP4 alike.
    `frames` are raw ID3 the writer has no field for — the genre, the full
    broadcast date (its `year` field would keep only the year), and the raw
    title kept verbatim in a comment so the original survives however the parse
    went. MP4 downloads get the fields and not the frames, which is the same
    split every other cross-format write in the app makes.
    """
    parsed = item.parsed
    show = parsed.show or feed_title
    fields: dict = {
        'title': parsed.title or normalise_whitespace(parsed.raw),
        'album': show,
        'artist': show,
        'album_artist': feed_title or parsed.show,
    }
    if parsed.episode is not None:
        fields['track'] = str(parsed.episode)
    if parsed.series is not None:
        fields['disc'] = str(parsed.series)
    if parsed.date:
        # MP4 has no frame write below, so the year goes through the
        # cross-format writer; MP3 gets the full date as TDRC instead.
        fields['year'] = parsed.date[:4]

    frames: dict = {'TCON': 'Podcast', 'COMM::eng': parsed.raw}
    if parsed.date:
        frames['TDRC'] = parsed.date
    return ({k: v for k, v in fields.items() if v},
            {k: v for k, v in frames.items() if v})
