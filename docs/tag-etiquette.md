# Tag etiquette — good ID3 practice, and how Backtrack reads your tags

This guide has two parts:

- **[Part 1 — General good practice](#part-1--general-good-practice)** is portable advice that
  holds in any tagger (Backtrack, Picard, Mp3tag, foobar2000, iTunes/Music).
- **[Part 2 — How Backtrack reads each tag](#part-2--how-backtrack-reads-each-tag)** is
  specific to this app: which frames it displays, edits, auto-generates, and how.

Backtrack works with **ID3v2** on MP3 and the **MP4 atom** family on `.m4a/.mp4/.m4p/.aac`.
Where a rule differs by format it is called out; unless noted, examples are ID3.

> Related: **[library-layout.md](library-layout.md)** (how file/folder names are parsed) and
> **[filesystem-etiquette.md](filesystem-etiquette.md)** (how to organise a library on disk).

---

## Part 1 — General good practice

### The golden rules

1. **Tag the file, not the file name.** File names get renamed, truncated, and lost; embedded
   tags travel with the audio. Backtrack always prefers tag data and clearly marks the file path
   as *not* ID3 in the editor.
2. **One fact per frame.** Put the title in the title frame, the year in the date frame, the
   composer in the composer frame — never cram "Artist - Title (2019)" into one field.
3. **Be consistent across an album.** The same album artist, album name, year, and spelling on
   every track is what lets a browser group them. One stray "The Beatles" vs "Beatles" splits an
   album in two.
4. **Prefer standard frames over custom `TXXX`.** A dedicated frame (composer, ISRC, mood…) is
   understood everywhere; a `TXXX:MyField` is understood only by whatever wrote it.
5. **Fill the identity fields first.** Title, artist, album, album-artist, track number, and year
   are the backbone. Everything else is enrichment.
6. **Don't invent values.** Leave a frame absent rather than writing "Unknown" or "N/A" — an absent
   frame is handled gracefully; a literal "Unknown Artist" pollutes browse/search.

### Artist vs album artist — the field people get wrong most

- **Artist** (`TPE1`) is *who performed this track*.
- **Album artist** (`TPE2`) is *whose album this is* — the value that groups the album together.

On a normal single-artist album they're the same. They differ on:

- **Compilations / various-artists albums** — album artist = `Various Artists`, per-track artist =
  the real performer. Also set the **compilation flag** (see below).
- **Guest features** — track artist `Artist feat. Guest`, album artist just `Artist`.
- **Classical** — album artist is usually the primary performer/ensemble; composer goes in its own
  frame (`TCOM`), never in the artist field.

Always set an album artist. Without it, players fall back to the track artist and a compilation
shatters into one "album" per performer.

### The compilation flag

Set **`TCMP = 1`** on every track of a various-artists album (soundtracks, label samplers, "Now
That's What I Call…"). It tells players to group by album rather than by artist. Pair it with album
artist `Various Artists`. Leave it unset (or `0`) for normal albums.

### Multiple values (artists, composers, genres)

The ID3v2.4 way to store "two artists" is **multiple values in one frame**, not `Artist A / Artist B`
crammed into a string. Multi-value is right for: artists, composers, conductors, remixers, genres,
languages, moods. It is wrong for single-identity fields (title, album, album artist) — those are
one value by definition.

- **Slashes are dangerous** in single-value ID3v2.3 storage — `AC/DC` or `9/11` can be mis-split.
  Backtrack sidesteps this by writing **v2.4** whenever a file holds any multi-value frame
  (see [ID3 version](#id3-version-v23-vs-v24) below).

### Credits: performers and production

Fine-grained credits belong in the two "people list" frames, each a list of **role → name** pairs:

- **`TMCL` — musician credits**: instrument/part → performer ("Guitar → Jimmy Page").
- **`TIPL` — involved people**: production role → person ("Producer → George Martin").

Use these instead of stuffing everyone into the artist field. Keep the *headline* performer in
`TPE1` and the supporting cast in `TMCL`/`TIPL`.

### Dates

Prefer a full **ISO 8601** timestamp (`YYYY`, `YYYY-MM-DD`, or `YYYY-MM-DDThh:mm:ss`) in the
recording-date frame. `2019` is fine; `2019-07-26` is better. Distinguish:

- **Recording/issue date** of *this* release — the everyday "year".
- **Original release date** (`TDOR`) — for reissues/remasters, the year the work first came out.

Avoid the legacy split `TYER`/`TDAT`/`TIME` frames on new files; they exist only for ID3v2.3
compatibility.

### Sort-order tags

Sort frames let a library sort "The Beatles" under **B** and "Ludwig van Beethoven" under
**Beethoven** without changing what's displayed. Only add them when the sort value **differs** from
the display value — a sort tag identical to the source is redundant. Backtrack can generate these
for you (see Part 2 → *Sort order*).

### Genre

Use real genre words (`Jazz`, `Post-Punk`), not the numeric ID3v1 codes (`(17)`). Multiple genres →
multiple values, not a slash-joined string.

### Cover art

- Embed a **front cover** (`APIC` type 3) at a sane size — 500–1000 px square is plenty; multi-MB
  scans bloat every file and slow scanning.
- One front cover per track is the norm. Extra picture types (back, artist) are optional.
- **MP4 caveat:** the `covr` atom stores **JPEG or PNG only** — convert WEBP/GIF/BMP first.

### Lyrics

- **`USLT`** — plain (unsynced) lyrics, a single text body.
- **`SYLT`** — synced lyrics, timestamped lines for karaoke-style highlighting.

### Don't hoard junk frames

Encoder strings (`TSSE`), private frames (`PRIV`), ownership, and legacy sync frames accumulate from
past tools. They're harmless but noisy — prune what you don't use.

### ID3 version: v2.3 vs v2.4

- **v2.3** has the widest player support and is the safe default for single-value files.
- **v2.4** adds proper multi-value frames and ISO 8601 dates.

Backtrack picks automatically: it saves **v2.4 only when a file actually holds a multi-value frame**,
and **v2.3 otherwise**, for maximum compatibility. You don't choose per-file.

---

## Part 2 — How Backtrack reads each tag

Open the editor on any track (**Edit tags** from a track's menu, or `a` to add a frame). Backtrack
routes each frame to a widget suited to its type and renders a friendly name you can customise
(**Settings → EDITORS → Tag Name Preferences**). The editor header shows real tag data (title ·
artist · duration · format), never the file name, and marks the **File path** row as *not ID3*.

### Core identity

| Frame | Friendly name | Values | Notes |
|---|---|---|---|
| `TIT2` | Title | single | The backbone. Derivable from the file name (**Derive from filename**). |
| `TALB` | Album | single | Groups tracks; also drives browse. |
| `TPE1` | Artist | **multi** | Per-track performer. Shown in browse/search/playback. |
| `TPE2` | Album artist | single | Groups the album; header subtitle in browse. Set to `Various Artists` for compilations. |
| `TIT3` | Subtitle | multi | Optional refinement. |
| `TOAL` / `TOPE` | Original album / artist | multi | For covers/reissues. |
| `TCMP` | Compilation | flag | `1` marks a various-artists album (set with album artist `Various Artists`). Edited as a **Yes/No toggle**; auto-set by **Derive from filename** when a VA compilation is detected. |

### People & credits

| Frame | Friendly name | Shape | Notes |
|---|---|---|---|
| `TMCL` | Musician credits | role → name list | Instrument/part → performer. In the player's `w` panel these become **PERFORMERS**. |
| `TIPL` | Involved people | role → name list | Production roles → person. Become **PRODUCTION TEAM** in the player. |
| `TCOM` | Composer | **multi** | Classical/soundtrack composer. Own sort frame `TSOC`. |
| `TPE3` | Conductor | **multi** | |
| `TPE4` | Interpreted/remixed by | **multi** | Remixer / arranger. |
| `TEXT` | Lyricist | **multi** | |

The people editor aggregates **role → name** across a bulk selection, edits/adds/removes an entry
across every file that has it, reorders entries (`K`/`J`), and imports from CSV/TSV/columns.

### Classical / multi-part works

| Frame | Friendly name | Notes |
|---|---|---|
| `TIT1` | Work name | The overall work (e.g. a symphony). |
| `MVNM` | Movement name | e.g. "II. Andante". |
| `MVIN` | Movement number | `n/total` fraction editor. |
| `TSOC` | Composer sort | Auto-generated from `TCOM`. |

### Dates

| Frame | Friendly name | Widget | Notes |
|---|---|---|---|
| `TDRC` | Year / recording date | ISO 8601 date+time picker | The everyday "year"; **Derive from filename** fills it from `(1997)` / `1997 - Album` folders. |
| `TDOR` | Original release date | ISO 8601 | For reissues. |
| `TDRL` | Release date | ISO 8601 | |
| `TDEN` / `TDTG` | Encoding / tagging date | ISO 8601 | Technical. |
| `TORY` / `TDAT` / `TRDA` / `TIME` | Legacy year/date/time | year / DD-MM / time pickers | ID3v2.3 only — prefer `TDRC`. |

The date editor cycles **date → time → timezone** with Tab; the timezone step opens a full-screen
world-map picker returning an ISO offset.

### Numbering

| Frame | Friendly name | Widget | Notes |
|---|---|---|---|
| `TRCK` | Track number | `n/total` fraction | e.g. `5/12`. |
| `TPOS` | Disc number | `n/total` fraction | e.g. `1/2`. Single-disc sets show no disc in playback. |

**Renumber tracks** (bulk) converts between continuous (1…N across all discs) and per-disc numbering.

### Genre, mood, language, key

| Frame | Friendly name | Values | Notes |
|---|---|---|---|
| `TCON` | Genre | **multi** | Real words, multiple values. |
| `TMOO` | Mood | **multi** | |
| `TLAN` | Language | **multi** | |
| `TKEY` | Key | single | **Musical-key picker** — majors/minors/off-key, or type custom. |

### Rating & play count

| Frame | Friendly name | Widget | Notes |
|---|---|---|---|
| `POPM` | Rating (Popularimeter) | **star editor** | 0–5 stars on the **Windows Media Player** scale (`0/1/64/128/196/255`), plus play count and rater email. Reads any byte to the nearest star. |
| `PCNT` | Play counter | number spinner | Non-negative integer. |

### Sort order

| Frame | Sourced from | Notes |
|---|---|---|
| `TSOT` | Title (`TIT2`) | |
| `TSOP` | Artist (`TPE1`) | Name-inverted ("Paul McCartney" → "McCartney, Paul"). |
| `TSO2` | Album artist (`TPE2`) | |
| `TSOA` | Album (`TALB`) | Article moved to end ("The Wall" → "Wall, The"). |
| `TSOC` | Composer (`TCOM`) | |

Backtrack **generates these heuristically**: it merges initials (`J. S.` → `J.S.`), joins Celtic
prefixes (`O' Connor` → `O'Connor`), strips honorifics (`Dr.`/`MC`) and suffixes (`Jr.`), handles
spacing prefixes (`von`/`van`/`de`), and moves leading articles. A simple name resolves to one
suggestion (silently prefilled); an ambiguous multi-word name offers a ranked list to pick from, and
**"type custom"** is always available. Nothing is written when a value already sorts as itself
(e.g. `Radiohead`). Available as **Add sort tag** in the editor and **Apply sort orders** in bulk.

### Numeric / technical

| Frame | Friendly name | Widget | Notes |
|---|---|---|---|
| `TBPM` | BPM | number spinner | Beats per minute. |
| `TLEN` / `TDLY` | Duration / playlist delay | number spinner (ms) | Milliseconds. |
| `TSRC` | ISRC | **validated field** | International Standard Recording Code; normalised (hyphens stripped, upper-cased) and shape-checked against `CC-RRR-YY-NNNNN`. |
| `TMED` | Media type | **picker** | CD / Digital / Vinyl / Cassette / … or a custom ID3 code. |
| `RBUF` | Recommended buffer | number spinner | Streaming buffer size in bytes. Rarely needed. |
| `TSSE` | Encoder | text | Encoding software/settings. |
| `TENC` | Encoded by | text | |

`RVRB` (reverb) is a structured multi-field frame with no simple form, so Backtrack reports it as
**not editable** rather than offering a lossy text box.

### Assets & structured editors

| Frame | Friendly name | Widget | Notes |
|---|---|---|---|
| `APIC` | Album art | image picker | Adding/editing offers the **most likely nearby cover** first, then a path prompt. Front cover (type 3) by default. MP3 APIC + MP4 `covr` (JPEG/PNG). |
| `SYLT` | Synced lyrics | lyric sync tool | Timestamped karaoke lines. |
| `USLT` | Lyrics | system editor | Plain lyric body. |
| `COMM` | Comments | system editor | Multi-line, language-tagged. |
| `EQU2` | Equalisation | graphic EQ | Per-band boost/cut; applied on playback via libvlc. |
| `RVA2` | Relative volume | dB meter | Master-channel gain. |

### URLs & IDs

`WOAR` (artist URL), `WOAF`/`WOAS`/`WORS`/`WPUB`/`WCOM`/`WCOP`/`WPAY` (various official pages) and
`WXXX` (custom URL) are plain text fields. `TXXX` holds custom `descriptor → value` text — use a
standard frame instead whenever one exists.

### Power-user editing

- **Plain-text mode** (Settings → EDITORS → *Toggle Plain-text Editing*) edits values as raw text
  instead of the smart widgets. **Ctrl-T** flips the current edit between the widget and raw text.
- **Multi-value frames** open a single-line field by default; **Ctrl-T** expands to a list editor
  (add/edit/delete/reorder/import). A frame already holding 2+ values opens the list automatically.
- **Import list from text/file** pre-fills a template or reads `role: name` / value lines from a file.

### Format notes

- **MP3 / ID3** exposes the full frame set above.
- **MP4** (`.m4a/.mp4/.m4p/.aac`) maps the common atoms (title, artist, album, album artist, track,
  disc, genre, year, compilation, cover, sort names). Frames with **no MP4 equivalent** (disc
  subtitle, most binary/sync frames) are skipped **and reported**, never silently dropped.
- Tag *editing* in the single-track editor is **MP3-only**; MP4 files play, scan, and are written by
  the cross-format **bulk** operations (derive, rename, sort orders, renumber, album art).
