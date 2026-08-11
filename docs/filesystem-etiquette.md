# Filesystem etiquette — organising a music library on disk

Good on-disk hygiene makes a library **portable** (survives moving between drives, OSes, and
players), **auto-detectable** (Backtrack can derive tags and covers from a clean layout), and
**browsable by a human** with a plain file manager. This is the good-practice companion to
**[library-layout.md](library-layout.md)**, which is the exact reference for *what Backtrack's
parser recognises*. Read this for the "why" and the conventions; read that for the precise patterns.

> Tags are the source of truth (see **[tag-etiquette.md](tag-etiquette.md)**). A tidy filesystem is
> what lets Backtrack **bootstrap** tags for untagged files and keep everything findable — it is not
> a substitute for tagging.

---

## The one principle

**Pick one scheme and apply it everywhere.** A library that is 100% consistent — even in an
imperfect scheme — beats a library that mixes three "better" schemes. Consistency is what lets
auto-detection, sorting, and your own muscle memory work.

The scheme Backtrack understands best, and the one most players and taggers assume:

```
Music/
└── Album Artist/
    └── Album (Year)/
        ├── 01 - Title.mp3
        ├── 02 - Title.mp3
        └── cover.jpg
```

Everything below is refinement on that spine.

---

## Top level: group by album artist

Group by **album artist**, not track artist — otherwise a compilation or a guest feature scatters an
album across folders.

```
Music/
├── Fleetwood Mac/
│   ├── Rumours (1977)/
│   └── Tusk (1979)/
└── Various Artists/
    └── Trainspotting (1996)/
```

- Use the album artist exactly as tagged, so folder and tag agree.
- A flat `Music/Album/` (no artist level) also works, but the artist level keeps large libraries
  navigable and gives the parser an album-artist signal.

## Album folders

Any of these read cleanly and let Backtrack lift the year:

- `Album` — simplest.
- `Album (1997)` — **recommended**; the year is unambiguous.
- `1997 - Album` — sorts folders chronologically within an artist.

Keep the album name matching the `TALB` tag. Don't append edition junk (`[320kbps][Deluxe][WEB]`) —
put "Deluxe Edition" in the album name only if it's genuinely part of the title.

## Track files

`NN - Title.ext` or `NN Title.ext` — both are fine; be consistent.

- **Zero-pad** track numbers to the album's width (`01`, `02` … `12`; `001` … for 100+). Padding
  keeps files in track order in every file manager.
- For a **compilation**, fold the performer in: `NN Artist - Title.ext`, so each track carries its
  artist in the name too.
- Don't repeat the album or artist in every file name unless it's a compilation — it's noise.
- The title in the name should match `TIT2`; a leading `Artist - ` is only stripped by the parser
  when it's a compilation or matches the album artist, so `Interlude - Reprise` stays a title.

## Multi-disc sets

Two equally good conventions — pick one per set:

**Disc subfolders** (best for large sets):
```
Album (2003)/
├── Disc 1/
│   ├── 01 - Title.mp3
│   └── …
└── Disc 2 - The B-Sides/     ← the "- Subtitle" becomes the disc subtitle (TSST)
    └── 01 - Title.mp3
```

**Disc-prefixed files** (best for two-disc sets):
```
Album (2003)/
├── 1-01 - Title.mp3
├── 1-02 - Title.mp3
└── 2-01 - Title.mp3
```

Either way, tag `TPOS` as `1/2`, `2/2`. A bare `Series 1` / `Season 2` subfolder is treated as a
disc subtitle so it shows in playback.

## Compilations & various-artists albums

```
Various Artists/
└── Now That's What I Call Music! 50 (2001)/
    ├── 01 Artist A - Title.mp3
    └── 02 Artist B - Title.mp3
```

- Album artist folder = `Various Artists`.
- Tag album artist `Various Artists` **and** set the compilation flag (`TCMP = 1`) — Backtrack's
  **Derive from filename** does both automatically when it detects a VA compilation.

## Classical & multi-part works

Classical resists a single clean scheme; favour whatever keeps a *work* together and readable:

```
Herbert von Karajan/
└── Beethoven — Symphony No. 9 (1963)/
    ├── 01 - I. Allegro ma non troppo.mp3
    └── …
```

Lean on **tags** for the real structure — work (`TIT1`), movement name (`MVNM`), movement number
(`MVIN`), composer (`TCOM`) — rather than trying to encode it all in folders. The composer belongs
in the composer tag, not the artist folder.

## TV-style / episodic collections

`SxxExx` names are recognised (season → disc, episode → track):

```
Bleak Expectations/
├── S01E01 A Childhood Cruelly Kippered.mp3
└── S01E02 An Adolescence Utterly Trashed.mp3
```

For per-season artwork, name covers so each season's tracks pair with one image (see *Cover art
files*).

## Singles & loose tracks

Give singles a home too, so they don't rot in a "downloads" pile:

```
Artist/
└── Single Name (2020)/
    └── 01 - Single Name.mp3
```

or a per-artist `Singles/` folder if you prefer.

## Cover art files

- **Whole-album cover:** put `cover.jpg` (or `folder.jpg`) in the album folder. Many players show it
  even when art isn't embedded. Still embed the front cover in the tags for portability.
- **Per-track covers:** when tracks have their own art, name the image to match the track — same base
  name (`01 - Title.jpg`), or a number that matches the track/disc/season. Backtrack's **Set album
  art from files** pairs these automatically (same-name, track-number, positional, `%token%`, or
  one-cover-per-disc/series). It looks in the album folder and in `artwork/`, `covers/`, `scans/`
  subfolders.
- Keep art reasonably sized (500–1000 px). Multi-MB scans slow every scan and bloat the tags.

---

## Character & cross-platform hygiene

File names travel between macOS, Windows, and Linux. Stay safe:

- **Never** use `/ \ : * ? " < > |` in names — illegal or reserved on some OS. Substitute a dash or
  drop them. (Backtrack's **Rename files from tags** strips these for you.)
- Avoid **trailing dots or spaces** (Windows trims them, causing mismatches).
- Avoid leading dots (hidden files on Unix).
- Prefer plain hyphens and spaces over exotic Unicode separators; keep accents (they're fine and
  correct) but be wary of look-alike characters.
- Watch **path length**: deeply nested `Artist/Album/Disc/…` plus a long title can exceed Windows'
  legacy 260-char limit. Keep names tight.
- Decide on **case** and stick to it; some filesystems are case-insensitive, so `Song.mp3` and
  `song.mp3` can collide.

## Keep the library clean

- **Separate incoming from the library.** Tag and rename in a scratch/`_incoming` folder, then move
  finished albums in. A clean library stays clean.
- **One file per track.** Delete duplicates, `Title (1).mp3`, and half-downloads.
- **Don't hand-edit inside the library while Backtrack is running** on it — let the app's rename/
  derive ops keep the file paths and the library cache in sync (they update both, collision-safely).
- **Back up before bulk operations.** Renames and tag writes are careful (two-phase, collision-safe)
  but a backup is cheap insurance before a big sweep.

## Let Backtrack do the tidying

Once files are roughly organised, the bulk tools finish the job:

- **Derive from filename** — read title/track/disc/album/artist/year/VA from a clean layout into tags.
- **Rename files from tags** — the inverse: produce uniform, filesystem-safe names from your tags.
- **Set album art from files** — embed per-track or per-disc covers found beside the tracks.
- **Apply sort orders**, **Renumber tracks**, **Assign by range/schedule** — bulk metadata cleanup.

See **[library-layout.md](library-layout.md)** for the exact folder/name patterns these recognise,
including the `%token%` template and regex overrides for libraries that don't fit a standard scheme.
