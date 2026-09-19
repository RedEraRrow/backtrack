"""Tunable constants — every timing, threshold and weight the app is dialled in on.

These are the numbers you would reach for to change how the app *feels* rather
than what it does: how often the player loop wakes, how long a toast stays up,
how much a play count is worth when ranking a search. They were spread through
the modules as bare literals, which made them impossible to find, compare, or
change with any confidence that a related value elsewhere didn't also need
moving — a tick interval in one file and the broadcast interval it feeds in
another, with nothing to say they were related.

Structural constants stay where they are used: an ANSI escape's length, the
number of border columns in a box, the width of a rating byte. Those aren't
tuning, they're facts about the thing being drawn or parsed.

This module imports nothing, so anything may import it.
"""
from __future__ import annotations

# --- playback session (VLC) -------------------------------------------------

# VLC reports length and position asynchronously; a just-started track needs a
# moment before either is meaningful. Deliberately outside the session lock.
VLC_PLAY_SETTLE_S = 0.3
# How often the session polls VLC for position/state while playing.
TICK_INTERVAL_S = 0.1
# Seeking to exactly the end trips end-of-track handling, so stop just short.
SEEK_END_MARGIN_S = 0.5
# Stand-in length when neither mutagen nor VLC will say how long a track is.
# Large enough that the progress bar reads as "unknown" rather than "nearly over".
DURATION_FALLBACK_S = 999.0
# libVLC's equaliser accepts ±20 dB per band; outside that it clips silently.
EQ_GAIN_LIMIT_DB = 20.0
# Restart the current track rather than stepping back, if it is this far in.
PREV_RESTART_AFTER_S = 5.0
# Hex characters of a uuid4 kept as a session / process id. Short enough to sit
# in a socket path and a status line, long enough not to collide in practice.
ID_SLICE_LEN = 8
# Waiting for a departing host's successor to rebind the session socket:
# tries × interval is the total grace period (~2.5 s).
HANDOFF_POLL_TRIES = 25
HANDOFF_POLL_INTERVAL_S = 0.1

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
# A terminal resize arrives as a burst of SIGWINCHes; redraw once it settles.
RESIZE_DEBOUNCE_S = 0.15
# After arrowing through lyrics by hand, how long before the display returns to
# following the audio.
MANUAL_LYRIC_REVERT_S = 4.0
# Toast lifetimes: short for a value the user is scrubbing (volume, seek), longer
# for something they need to read.
TOAST_SHORT_S = 1.0
TOAST_MEDIUM_S = 2.0
TOAST_LONG_S = 2.5
# The 'e' key's jump to near the end of a track.
NEAR_END_JUMP_S = 35

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

# --- lyrics timing ----------------------------------------------------------

# Speaking rate assumed when a line has no measured word timings.
LYRIC_FALLBACK_WPS = 2.2
# No line is shown for less than this, however the arithmetic works out.
LYRIC_MIN_LINE_S = 0.5
# How far ahead in the transcript a sentence may look for its next word.
LYRIC_MATCH_WINDOW_WORDS = 80
# A silence longer than this gets a visible gap indicator between lines.
LYRIC_AIR_THRESHOLD_S = 2.0
# Assumed reading rate for a stage direction shown as its own beat, in words per
# second, and the floor below which even a one-word note is too quick to register.
LYRIC_READ_WPS = 2.3
LYRIC_READ_MIN_S = 0.8
# How much of a direction's reading time the silence has to cover before the
# direction is given that silence as a beat of its own. Below it, the direction
# rides along as a cue on the neighbouring line instead, where it stays up for as
# long as that line does: a 40-word transcriber's note flashed through a half-
# second gap is unreadable, and unreadable is worse than late.
LYRIC_SD_READ_FRACTION = 0.75
# The last SYLT entry has no following timestamp, so it is given this long.
LYRIC_FABRICATED_END_MS = 5000

# --- player panes -----------------------------------------------------------

# Rows to reserve when deciding whether a pane fits, before its real content is
# built. Estimates, deliberately: the layout has to be budgeted before the text
# that fills it exists.
PANE_CREDITS_EST_ROWS = 5
PANE_LYRICS_EST_ROWS = 6
