# Library layout & naming — getting the most from "Derive from filename"

Backtrack can fill in missing tags by reading your **file names and folders**
(Browse → an album → *Edit tags* → **Derive from filename**). The better your
files are laid out, the more it can infer. This guide documents exactly what the
parser recognises, so you can structure a library for clean auto-derivation.

Everything here is read-only inference shown in a **preview** before anything is
written, the default is **fill blanks only** (existing tags are never
overwritten unless you ask), and you can press **`d`** on any preview row to see
the full, untruncated breakdown for that file. So there's no risk in trying it.

> This is the **parser reference**. For the *why* and broader conventions, see
> **[filesystem-etiquette.md](filesystem-etiquette.md)** (organising a library on
> disk) and **[tag-etiquette.md](tag-etiquette.md)** (good tagging practice).

---

## The ideal layout

```
Artist/
  Album/
    01 - Title.mp3
    02 - Title.mp3
```

Multi-disc / multi-series, and compilations:

```
Artist/                         Various Artists/
  Album/                          Now 90s/
    Disc 1/                         01 Oasis - Wonderwall.mp3
      1-01 - Title.mp3              02 Blur - Song 2.mp3
    Disc 2 - The Extras/
      2-01 - Title.mp3
```

From this, one derive pass fills: **title, track (+total), disc (+total), disc
subtitle, album, album artist, year, the compilation flag, and sort-order tags.**

---

## Track & disc numbers (in the file name)

The leading part of the file name (before the title) is matched, most-specific
first. A **4-digit** leading number is treated as a *year*, not a track.

| You write | Meaning |
|---|---|
| `01 Title`, `01. Title`, `01 - Title`, `01_Title` | track 1 |
| `1-05 Title` | **disc 1, track 5** (a `D-TT` prefix) |
| `S02E04 Title`, `2x04 Title` | **disc 2 (season), track 4 (episode)** |
| `A1 Title`, `B2 Title` | vinyl side A/B → disc 1/2, track 1/2 |
| `Track 3 - Title` | track 3 |
| `1999 - Title` | *not* a track — `1999` is read as a year |

## Disc / series folders

A subfolder whose name starts with **`CD`**, **`Disc`**, **`Disk`**,
**`Series`** or **`Season`** sets the disc number, and that folder "collapses"
so the folder *above* it is treated as the album.

| Folder | Disc | Disc subtitle (TSST) |
|---|---|---|
| `CD1`, `Disc 2`, `Disk 3` | 1 / 2 / 3 | — (redundant with the number) |
| `Disc 2 - The Extras` | 2 | **The Extras** |
| `Series 1`, `Season 2` | 1 / 2 | **Series 1 / Season 2** |
| `Series 1 - Origins` | 1 | **Origins** |

`Series`/`Season` folders use their own label as the subtitle, so the player
shows **"Series 1"** instead of "Disc 1". If a file name *and* a folder disagree
on the disc (`.../Disc 1/2-05 …`), the **file name wins**.

## Album & artist (from folders)

- **Album** = the containing folder (the one above any `Disc N`/`Series N`).
- **Album artist** = its parent folder.
- If there's no artist level, a single folder named **`Artist - Album (Year)`**
  is split into artist / album / year.

## Year / date

Taken from the album folder — `Album (1997)` or `1997 - Album` — or from a full
date in the file name (`1952-02-20`, common for old-time radio), which maps to
the year/date tag and is stripped out of the derived title.

## Compilations (various artists)

If the **top folder** is `Various Artists`, `Various`, `VA`, `V.A.` or
`Compilations`, the album is treated as a compilation:

- album artist → **"Various Artists"**, and the **compilation flag** is set;
- each track's own artist is read from the `Artist - Title` file name.

## Titles

Underscores become spaces and extra whitespace is collapsed (case is left
alone — `MF DOOM` stays `MF DOOM`). A leading `Artist - ` is pulled out as the
**track artist** *only* when there's evidence it really is one — the album is a
compilation, or the album artist appears in that prefix (so `Radiohead feat.
Björk - Reckoner` splits, but `Interlude - Reprise` and `Speak to Me - Breathe`
stay whole titles).

## Sort-order tags

Tick **"Sort-order tags"** to also write sort names for the fields you're
deriving: artists invert (`The Beatles` → `Beatles, The`, `Miles Davis` →
`Davis, Miles`), album/title only move a leading article (`The Wall` → `Wall,
The`), and values that need no change (`Radiohead`, `OK Computer`) get no tag.

---

## Worked examples

These are the parser's actual output:

| Path | Derives |
|---|---|
| `Radiohead/OK Computer/04 - Exit Music.mp3` | title `Exit Music` · track 4 · album `OK Computer` · album-artist `Radiohead` |
| `Radiohead/OK Computer (1997)/04 Exit Music.mp3` | …+ year `1997` |
| `The Beatles/White Album/Disc 1/1-01 Back in the USSR.mp3` | title `Back in the USSR` · track 1 · disc 1 · album `White Album` · album-artist `The Beatles` |
| `The Beatles/White Album/CD2 - The Extras/05 Something.mp3` | title `Something` · track 5 · disc 2 · subtitle `The Extras` |
| `John Finnemore's Double Acts/Series 1/1 - A Flock Of Tigers.mp3` | title `A Flock Of Tigers` · track 1 · disc 1 · subtitle `Series 1` · album `John Finnemore's Double Acts` |
| `Various Artists/Now 90s/07 Blur - Song 2.mp3` | title `Song 2` · track 7 · album `Now 90s` · album-artist `Various Artists` · artist `Blur` · compilation |
| `Podcasts/MyShow/S02E04 The One.mp3` | title `The One` · track 4 · disc 2 · album `MyShow` |

---

## When names don't fit a pattern — the template override

Pick **"Use a naming template"** and describe the file-name shape with tokens:

```
%disc%-%track% %title%
%track% - %artist% - %title%
```

Tokens: `%track%` `%disc%` `%title%` `%artist%` `%albumartist%` `%album%`
`%year%` `%date%` `%season%` `%episode%` and **`%ignore%`** (swallow a run of
characters you don't want to keep).

**Numbers that aren't digits.** Add `:r` (Roman) or `:en` (written out) to any
numeric token and it matches that form instead, converting to a real number:

```
Act %track:r% - %title%                            → Act III - The Duel      (track 3)
Series %disc:en% Episode %track:en% - %title%      → Series Three Episode Four - Pilot
```

A raw regex gets this for free — a `(?P<track>…)` group that captures `III` or
`three` lands as 3 without any extra work.

For example, an old-time-radio file like
`You Bet Your Life 1952-02-20 (160) Secret Word - Heart` cleans up with:

```
You Bet Your Life %ignore% (%track%) Secret Word - %title%
```

### Regex mode (power users)

For names the tokens can't express, pick **"Use a regex"** and write a Python
regex with **named groups** that map to fields. It's matched against the file
name (without extension) with a *search* (it can match part of the name), and
captured fields override auto-detection — folder-derived album/artist still fill
anything the regex doesn't capture.

Recognised group names (extras are ignored, with a heads-up):
`track` `disc` `title` `artist` `albumartist` `album` `year` `date` and the
aliases `season`→disc, `episode`→track.

```
(?P<track>\d+)\.\s*(?P<title>.+)          →  "04. Exit Music"      → track 4, title "Exit Music"
(?P<season>\d+)x(?P<episode>\d+) - (?P<title>.+)   →  "2x04 - The One"  → disc 2, track 4, title "The One"
(?P<artist>.+?) - (?P<title>.+?) \((?P<year>\d{4})\)   →  "Blur - Song 2 (1997)"
```

Unlike the token template, a captured `title` is kept **exactly** — no
artist-prefix stripping — so you're in full control.

**Match against the folder path.** After choosing "Use a regex" you can match
either the **file name** or the **folder path** (from your library root — the
prompt shows a sample so you can see exactly what you're matching, with `/`
between folders). Path mode lets one regex capture folder levels:

```
(?P<albumartist>[^/]+)/(?P<album>[^/]+)/(?P<track>\d+)\s*-\s*(?P<title>.+)
        matches  "Radiohead/OK Computer/04 - Exit Music"
/Series (?P<disc>\d+)/(?P<track>\d+) - (?P<title>.+)     ← just pull the series number
```

Captured groups override auto-detection; anything the regex doesn't capture
(album, artist, …) is still filled from the folder rules above.

---

## Format support

- **MP3** — full support; a fresh ID3 tag is created for files that have none.
- **MP4 / m4a / m4p / aac** — written natively, **except disc subtitle** (there
  is no standard MP4 atom for it — the preview says so rather than silently
  skipping).
- **Anything else** — left untouched and reported as skipped.
