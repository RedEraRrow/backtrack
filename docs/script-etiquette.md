# Script etiquette — writing a dialogue markdown a transcript can be matched to

Backtrack's lyric editor and dialogue playback take **two** files for a spoken-word
track: a timed transcript (`.json`, word-level timings from Whisper or equivalent) and a
markdown **script** (`{track}.md`) next to the audio. The transcript owns *when* each word
is said. The script owns everything the timing cannot carry — who is speaking, the real
punctuation and capitalisation, emphasis, and the stage directions.

The two are joined by a single word-level alignment
([`lyrics_text.align_tokens`](../src/lyrics/lyrics_text.py)), so the script only does its job
if its words are recognisably the same words the transcript heard. This guide is how to
write one that matches.

> Related: **[tag-etiquette.md](tag-etiquette.md)** (ID3 frames, including `USLT`/`SYLT`) and
> **[library-layout.md](library-layout.md)** (where files live).

---

## The shape of a line

```markdown
*(Bing bong.)*
**DOUGLAS** *(over cabin address)*: Good evening, ladies and gentlemen.
**MARTIN**: Yes, *fine*, but -- *(he sighs)* -- we're diverting.
**DOUGLAS** and **MARTIN** *(simultaneously)*: Shut up, Arthur.
```

- **A speaker line** is `**NAME**: text`, with the first colon ending the header.
- **Several speakers** are each bolded: `**CAROLYN**, **DOUGLAS** and **MARTIN**:`. They
  become one banner, `CAROLYN & DOUGLAS & MARTIN`.
- **A whole-line stage direction** is a line that is nothing but a parenthetical,
  `*(Flight deck door opens.)*`. It becomes a beat of its own.
- **An inline stage direction** is a parenthetical inside a line. It splits the line into
  separate beats at that point.
- **Continuation lines** — a line with no `**NAME**:` header — are appended to the speaker
  above, which is how a song or a limerick stays one turn.

### One turn per line

A second `**NAME**:` further along the same line is **not** recognised. Everything after
the first colon is that first speaker's dialogue, so the second speaker's name ends up
spoken aloud in the word stream.

```markdown
<!-- wrong: Martin's whole turn is attributed to Arthur -->
**ARTHUR**: I'll go and see.  **MARTIN** *(into radio)*: Lundy, good afternoon.

<!-- right -->
**ARTHUR**: I'll go and see.
**MARTIN** *(into radio)*: Lundy, good afternoon.
```

---

## Stage directions

### A direction is its own thought, and gets its own emphasis

Write the direction in its **own** emphasis span, separate from any emphasised dialogue
around it:

```markdown
<!-- wrong: the whole span reads as one direction, and the dialogue in it is lost -->
**DOUGLAS**: *Patek Philippe. (Normal voice)* Well, he's certainly not a goodie.
**ARTHUR**: I can. I'm doing it now! *(Long pause) Wow!*

<!-- right -->
**DOUGLAS**: *Patek Philippe.* *(in a normal voice)* Well, he's certainly not a goodie.
**ARTHUR**: I can. I'm doing it now! *(Long pause)* *Wow!*
```

This is the one rule where getting it wrong costs you *words*, not just formatting. A
direction's text is never spoken, so anything swept into one is deleted from the script's
word stream, silently — it will not appear as a missing word in the verify report, because
as far as the matcher is concerned you never wrote it.

The emphasis markers must balance either side of the bracket: `*(dir)*` or `(dir)`, not
`*(dir)`.

### Brackets that are not directions

A parenthetical with **fewer than two letters** is treated as the script's own punctuation
and stays in the dialogue:

| Written | Read as |
|---|---|
| `That's nice(!)` | dialogue — the sarcasm marker is kept and displayed |
| `do you have (a) a bottle and (b) a corkscrew` | dialogue — "a" and "b" are spoken |
| `(Ding)` | a stage direction |
| `for the BBC! (In Spanish accent)` | a stage direction |

If you need a very short direction, mark it: `*(Ding)*` is always a direction whatever its
length.

### Directions on the speaker header

A direction in the header is a manner note for the whole line and is shown on the banner
rather than as a separate beat. More than one is kept, joined with `; `:

```markdown
**DEROCHE** *(female, Swiss accent)* *(muffled)*: Come in.   →   banner: "female, Swiss accent; muffled"
```

### Nesting and colons are fine

A direction may contain a bracket and may contain a colon; both are read correctly because
the whole-line check runs before the `Speaker:` split.

```markdown
*(Immediately: bing bong, bing bong.)*
*(A ringtone sounds ('Questa o quella' from Verdi's Rigoletto), then a beep.)*
```

### Put a direction where the script already breaks

A direction gets a beat of its own by cutting the segment at that point, and the bulk split
(`S`) will only cut where the script has **already punctuated its way out** of the word
before it — `. ! ? , ; : -- ...` or a closing `♪`. A direction that interrupts a sentence
is reported as `· MID-LINE` and left alone:

```markdown
<!-- cut in bulk: the script ended a thought first -->
**ARTHUR**: Shush! *(Australian accent)* Yip! *(Normal voice)* Mrs Badcrumble, ...

<!-- suggested only: "Captain" is mid-phrase -->
**DOUGLAS**: Captain *(he assumes a French accent)* Martin duCref, who joins us today.
```

The mid-sentence one may well deserve its own beat — that is a judgement about the
performance, and one to make by hand rather than have done to three hundred segments
unattended. Punctuating the script where the delivery really breaks is the way to make it
automatic. A speaker change is never mid-sentence and is always cut.

### Repeats need somewhere to go

Four directions on one line can only be shown separately if there are spoken words between
them to cut the segment at. `*(Ding)* Ding! *(Ding)* Ding!` works; two directions back to
back with nothing between them collapse to one.

---

## Editorial notes

An editorial note in square brackets, `*[Transcriber's note: he lisps throughout.]*`, is
dropped from the spoken stream wherever it appears, and shown as a beat of its own when it
is a whole line.

Prefer a plain direction, `*(Transcriber's note: he lisps throughout.)*`, for anything you
want on screen — a long one is given the silence around it in proportion to its reading
time, and rides along on the neighbouring line when there is not enough silence to read it
in.

---

## Words

The matcher normalises both sides before comparing: case, accents, and punctuation are
ignored; hyphens, full stops and slashes become word breaks. You do **not** need to match
the transcript's spelling for any of these — a difference in convention is reconciled
automatically and reported as a match:

| Script | Transcript | Result |
|---|---|---|
| `take-off`, `no-one` | `takeoff`, `noone` | matched — word boundaries only |
| `twenty-five`, `seven thousand` | `25`, `7000` | matched — same number |
| `C.P.L.` | `CPL` | matched |
| `Molokaʻi` | `Molokai` | matched — the ʻokina is punctuation |
| `'cause`, `'til`, `'ave` | `because`, `until`, `have` | matched **and flagged** `≈ ELIDED` |

An elision is deliberately still reported. It is matched so the timing stays continuous,
but the script and the transcript really do disagree about the word, and that is worth your
eye.

Two things the matcher does **not** reconcile, because they are not spelling:

- **Times and years read as digit pairs** — `eleven thirty` against `11:30` pairs word for
  word, but `nineteen forty-three` against `1943` does not.
- **Anything genuinely different.** If the transcript heard a different word, you get a
  `~ CHANGED` row. That is the point.

### `♪` and `...`

`♪`, `...` and `--` carry no comparison token, so they are free: write them where they
belong. An opening `♪` is kept with the phrase it introduces rather than stranded on the
line before.

---

## Checking your work

Open the track in the lyric editor:

- **`V`** — verify. Diffs the whole spoken word stream against the script and reports every
  `– MISSING`, `+ EXTRA`, `~ CHANGED` and `≈ ELIDED` word, with a match percentage. A
  well-matched script sits at or near 100%.
- **`S`** — split. Cuts every segment the script says is more than one beat, at speaker
  changes and at inline stage directions that follow punctuation, so there is one segment
  per beat. Confirmed and undoable.

The verify report also lists two things `S` will not do for you:

- `· MID-LINE` — a direction that would cut mid-sentence. Split it by hand if the delivery
  warrants it, or punctuate the script so it qualifies.
- `✦ UNPLACED` — a direction the script puts between two words the transcript has no
  boundary for, usually because it misheard the word it stands against.
