"""Tunable constants: every timing, threshold and weight the app is dialled in on.

These are the numbers you would reach for to change how the app *feels* rather
than what it does: how often the player loop wakes, how long a toast stays up,
how much a play count is worth when ranking a search. Kept together so they
can be found and compared, and so it shows when a related value elsewhere also
needs moving: a tick interval in one file and the broadcast interval it feeds in
another.

Structural constants stay where they are used: an ANSI escape's length, the
number of border columns in a box, the width of a rating byte. Those aren't
tuning, they're facts about the thing being drawn or parsed.

This module imports nothing, so anything may import it.
"""
from __future__ import annotations

# --- playback session (mpv) -------------------------------------------------

# How long to wait for mpv to say how long a track is, when its tags didn't.
# Deliberately outside the session lock.
LENGTH_WAIT_S = 1.0
# How long a screen waits for the session's now-playing snapshot before it
# draws the last one instead (the session is busy loading a track).
NOW_PLAYING_WAIT_S = 0.02
# How often the session polls mpv for position/state while playing.
TICK_INTERVAL_S = 0.1
# Seeking to exactly the end trips end-of-track handling, so stop just short.
SEEK_END_MARGIN_S = 0.5
# Stand-in length when neither mutagen nor mpv will say how long a track is.
# Large enough that the progress bar reads as "unknown" rather than "nearly over".
DURATION_FALLBACK_S = 999.0
# The most an EQU2 band or a gain tag moves the level, either way: a sane
# bound on whatever a tagger wrote.
EQ_GAIN_LIMIT_DB = 20.0
# Restart the current track rather than stepping back, if it is this far in.
PREV_RESTART_AFTER_S = 5.0
# Audiobook playback speed: its range and the step of one key press.
RATE_MIN = 0.5
RATE_MAX = 3.0
RATE_STEP = 0.1
# Sleep timer: the lengths a key press cycles through (then end of chapter),
# how long the volume fades before it pauses, and how far ahead of a chapter or
# track end it pauses, so the player can't move on to the next one first.
SLEEP_MINUTES = (15, 30, 60, 90)
SLEEP_FADE_S = 5.0
SLEEP_END_EARLY_S = 0.5
# Hex characters of a uuid4 kept as a session / process id. Short enough to sit
# in a socket path and a status line, long enough not to collide in practice.
ID_SLICE_LEN = 8
# Waiting for a departing host's successor to rebind the session socket:
# tries × interval is the total grace period (~2.5 s).
HANDOFF_POLL_TRIES = 25
HANDOFF_POLL_INTERVAL_S = 0.1
# Queue edits undo can take back, at most.
UNDO_DEPTH = 20
# The queue is saved between runs after every edit and track change, on stop,
# and this often while playing.
SAVE_QUEUE_EVERY_S = 10

# --- local IPC (multi-window sessions) --------------------------------------

# How often the host pushes a state snapshot to connected clients. Fast enough
# that a second window's progress bar tracks the first, slow enough to be cheap.
IPC_BROADCAST_INTERVAL_S = 0.25
IPC_CONNECT_TIMEOUT_S = 0.4
# A client whose socket blocks sends this long is dropped rather than allowed to
# stall snapshot delivery to everyone else.
IPC_SEND_TIMEOUT_S = 2.0
# Accept timeout on the listening socket, so the server loop can notice shutdown.
IPC_ACCEPT_TIMEOUT_S = 0.3
IPC_RECV_CHUNK = 4096
IPC_LISTEN_BACKLOG = 8

# --- player view loop -------------------------------------------------------

# The idle sleep of the player loop, and how long a key read waits. The loop
# tick is the finer of the two: it bounds how quickly the display reacts.
LOOP_TICK_S = 0.02
KEY_POLL_INTERVAL_S = 0.05
# A resize is redrawn as it arrives; this is how long the size must then hold
# still before the player sends the full-quality art image. Brief pauses mid-drag
# stay on the preview: a full send in progress holds up the next redraw.
ART_FULL_IMAGE_SETTLE_S = 0.5
# Toast lifetimes: short for a value the user is scrubbing (volume, seek), longer
# for something they need to read.
TOAST_SHORT_S = 1.0
TOAST_MEDIUM_S = 2.0
TOAST_LONG_S = 2.5
# Status-bar message lengths live with the status bar, in backbone.
from backbone.ui import STATUS_S, STATUS_WARNING_S  # noqa: E402,F401
# The 'e' key's jump to near the end of a track (Diagnostics only).
NEAR_END_JUMP_S = 35
# The seek keys: seconds each moves, by action (player.<name>).
SEEK_STEPS_S = {'back_1': -1, 'fwd_1': 1, 'back_5': -5, 'fwd_5': 5, 'back_30': -30, 'fwd_30': 30}
# One press of a volume key, in percent. Presses this close together step from
# the level the last one asked for (a joined window hears the host's level
# only with its next snapshot), so quick presses add up.
VOLUME_STEP = 5
VOLUME_PRESS_WINDOW_S = 1.0

# --- search ranking ---------------------------------------------------------

# Matches are scored in two parts: a "solid" tier multiplied up by SOLID_BAND so
# a better kind of match always outranks a worse one, plus a fine score that only
# orders within a tier.
SEARCH_SOLID_BAND = 1000.0
SEARCH_ENTITY_QUALITY = 1000.0
# An earlier hit in the field ranks higher; positions past the cap all tie.
SEARCH_POSITION_CAP = 30
SEARCH_POSITION_SPAN = 60.0
# Below this share of the query matched, a result is not shown at all.
SEARCH_MIN_RATIO = 0.15
# Play count as a tie-breaker: worth this much each, up to the cap.
SEARCH_PLAY_COUNT_CAP = 20
SEARCH_PLAY_COUNT_WEIGHT = 0.05
# Multiplier applied to a recently-played track's fine score.
SEARCH_RECENCY_BOOST = 1.15
# Share of the typo tier's score staked on the edits looking like typing slips:
# at 1.0 a wholly unexplained edit would score nothing.
SEARCH_TYPO_SLIP_DISCOUNT = 0.35
# How implausible each kind of edit is as a typing slip, 0 (a finger landing one
# key over) to 1 (an unrelated letter). A dropped or doubled keystroke sits in
# between: a common slip, but no evidence the query was aimed at this word.
SEARCH_SLIP_ADJACENT = 0.0
SEARCH_SLIP_INDEL = 0.6
SEARCH_SLIP_RANDOM = 1.0
# A subsequence match counts only this long and this dense (matched letters
# over the span they're spread across).
SEARCH_SUBSEQ_MIN_LEN = 3
SEARCH_SUBSEQ_MIN_DENSITY = 0.5
# Weight of an entity's group size against match quality when ranking them.
SEARCH_ENTITY_SIZE = 50.0
# Results listed per section (artists, albums, ...) before "more".
SEARCH_SECTION_CAP = 4
# Below this many characters, only entities are listed. Short prefixes match a
# huge number of tracks and almost none of them usefully; the artist or album
# you are heading for is nearly always what you meant by "jo".
SEARCH_ENTITY_ONLY_UNTIL = 3

# --- lyrics timing ----------------------------------------------------------

# Speaking rate assumed when the track's length isn't known yet.
LYRIC_FALLBACK_WPS = 2.2
# No line is shown for less than this, however the arithmetic works out.
LYRIC_MIN_LINE_S = 0.5
# How far ahead in the transcript a sentence may look for its next word.
LYRIC_MATCH_WINDOW_WORDS = 80
# A silence longer than this becomes a dead-air beat (the pane shows nothing).
LYRIC_AIR_THRESHOLD_S = 2.0
# How early a line may appear, at most, in the silence before its first word. On
# time is too late: the eye needs a moment to land and start reading before the
# words are said. Giving a line the WHOLE preceding pause is the other error: it
# then sits on screen through a two-second silence looking like it has already been
# spoken. A short fixed anticipation reads as punctual either side of it.
LYRIC_LEAD_IN_S = 0.35
# The playback clock (session.elapsed) runs on its own between mpv's uneven
# readings: each new reading pulls it this fraction of the way
# to agreeing, and one this far out is a jump (a seek, a stall) taken at once.
CLOCK_EASE = 0.15
CLOCK_JUMP_S = 0.75
# Assumed reading rate for a stage direction shown as its own beat, in words per
# second, and the floor below which even a one-word note is too quick to register.
# Reading is about twice as fast as speech; 4 w/s is 240 wpm, ordinary silent reading.
LYRIC_READ_WPS = 4.0
LYRIC_READ_MIN_S = 0.8
# How much of a direction's reading time its own beat is given. When the silence
# it sits in is shorter, the rest is borrowed from the end of the beat before.
LYRIC_SD_READ_FRACTION = 0.75
# The last SYLT entry has no following timestamp, so it is given this long.
LYRIC_FABRICATED_END_MS = 5000

# --- player panes -----------------------------------------------------------

# Rows to reserve when deciding whether a pane fits, before its real content is
# built. Estimates, deliberately: the layout has to be budgeted before the text
# that fills it exists.
PANE_CREDITS_EST_ROWS = 5
PANE_LYRICS_EST_ROWS = 6
# The queue panel under the controls is only kept this much: a gap, the header
# and one track. Its tracks give way before the art shrinks, and any rows the art
# doesn't need go to it.
PANE_QUEUE_MIN_ROWS = 3

# How long the volume shows (over the middle of the screen) after it changes.
VOLUME_SHOWN_S = 1.5

# --- player art -------------------------------------------------------------

# The cover as an image: pixels sent per cell column (sharp on a Retina screen,
# a fraction of a full-size cover), those of the quick first image (~1/16 the
# pixels), and the size of the pieces a full image goes out in, so a resize
# mid-send can cut it short.
ART_INLINE_PX_PER_COL = 10
ART_INLINE_PREVIEW_PX = 3
ART_INLINE_CHUNK = 16 * 1024
# Accents taken from the cover (the Album art theme): the least colourful a
# colour may be to count, how far round the wheel a second one must be, and how
# many colours the cover is reduced to, to find them.
ART_ACCENT_MIN_SATURATION = 0.2
ART_ACCENT_MIN_HUE_GAP = 0.08
ART_ACCENT_PALETTE = 16

# --- editors (lyrics, trim, chapters) ---------------------------------------

# Lyrics audition: seconds played at a line's start, and at its end, and how far
# a whole line moves per arrow press, and per , / . press.
LYRIC_AUDITION_CLIP_S = 1.0
LYRIC_AUDITION_STEP_S = 0.05
LYRIC_AUDITION_COARSE_S = 0.25
# Trim: the coarse nudge (the fine one is one frame), seconds heard either side
# of a join, and the window "up to the out-point" plays.
TRIM_COARSE_STEP_S = 0.25
TRIM_JOIN_LEAD_S = 1.0
TRIM_JOIN_TAIL_S = 1.0
TRIM_TAIL_PREVIEW_S = 5.0
# Chapters: how close two starts may sit, and how far a rewritten file's length
# may drift from the original's before it's rejected.
CHAPTER_MIN_GAP_S = 0.1
CHAPTER_LENGTH_TOLERANCE_S = 0.5

# --- library and feeds ------------------------------------------------------

# How often the library is checked against the disk while the app runs.
LIBRARY_SYNC_INTERVAL_S = 30
# How long to wait for a podcast feed or an enclosure before giving up.
FEED_FETCH_TIMEOUT_S = 30.0
