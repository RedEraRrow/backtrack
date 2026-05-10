"""
Lyric synchronization tool for matching lyrics to timestamps.
"""

import sys
import time
import os
import select
import re

import vlc
from mutagen.id3 import ID3

from src import ui_utils
from src.terminal_input import raw_mode
from src.playback import normalise_lyric_newlines
from src.config import load_config


RED     = "\033[1;31m"
GREEN   = "\033[1;32m"
WHITE   = "\033[1;37m"
YELLOW  = "\033[1;33m"
CYAN    = "\033[1;36m"
DIM     = "\033[2m"
RESET   = "\033[0m"
BOLD    = "\033[1m"

PROGRESS_ROW = 4


def _draw_progress(elapsed, total, cols):
    progress    = min(elapsed / total, 1.0) if total else 0
    bar         = ui_utils.get_progress_bar(progress, cols - 16)
    elapsed_str = ui_utils.format_time(elapsed)
    total_str   = ui_utils.format_time(total)
    sys.stdout.write(
        f"\033[{PROGRESS_ROW};1H\033[K"
        f"  {bar}  {YELLOW}{elapsed_str}{DIM}/{RESET}{YELLOW}{total_str}{RESET}"
    )
    sys.stdout.flush()


def _draw(file_path, lines, i, start_time, total, marked_count, just_marked_at=None, offset_ms=0):
    cols = ui_utils.get_terminal_width()
    ui_utils.clear_screen()

    print(f"{CYAN}{ui_utils.divider(cols)}{RESET}")
    title = os.path.basename(file_path)
    print(f"{BOLD}  LYRIC SYNC  {DIM}│{RESET}  {WHITE}{title}{RESET}")
    print(f"{CYAN}{ui_utils.divider(cols)}{RESET}")

    print()  # placeholder for progress bar
    print(f"{DIM}{'─' * cols}{RESET}")

    line_num   = max(i, 0) + 1
    line_str   = f"  Line {line_num} of {len(lines)}"
    marked_str = f"✓ {marked_count} marked"
    padding    = cols - len(line_str) - len(marked_str) - 2
    print(f"{WHITE}{line_str}{RESET}{' ' * max(padding, 1)}{GREEN}{marked_str}{RESET}")
    print()

    prev = lines[i - 1] if i > 0 else None
    if prev:
        print(f"  {RED}{DIM}{prev}{RESET}")
    else:
        print(f"  {DIM}— start —{RESET}")
    print()

    curr = lines[i] if i >= 0 else "GET READY…"
    print(f"  {WHITE}{BOLD}▶  {curr}{RESET}")
    print()

    nxt = lines[i + 1] if i < len(lines) - 1 else None
    if nxt:
        print(f"  {GREEN}{nxt}{RESET}")
    else:
        print(f"  {DIM}— end —{RESET}")

    print()
    print(f"{DIM}{ui_utils.divider(cols)}{RESET}")

    if just_marked_at is not None:
        marked_time = ui_utils.format_time(just_marked_at / 1000)
        print(f"  {GREEN}{BOLD}✓ Marked at {marked_time}{RESET}")
    else:
        print(f"  {DIM}waiting for mark…{RESET}")

    print()
    offset_str = f"{offset_ms:+d}ms"
    print(f"  {YELLOW}SPACE / ENTER{RESET}  {DIM}mark     {RESET}{RED}Q{RESET}  {DIM}quit     {RESET}{DIM}offset: {RESET}{YELLOW}{offset_str}{RESET}  {DIM}(set in Settings › Lyric Lead-in){RESET}")
    print(f"{CYAN}{'─' * cols}{RESET}")

    sys.stdout.flush()


def sync_lyrics(file_path):
    """Interactive lyric synchronization: match lyrics to timestamps."""
    try:
        audio = ID3(file_path)
        uslt_tags = audio.getall('USLT')

        if not uslt_tags:
            print(f"No lyrics found in {os.path.basename(file_path)}")
            time.sleep(2)
            return

        raw_lyrics = uslt_tags[0].text
        normalised = normalise_lyric_newlines(raw_lyrics)
        lines = [l.strip() for l in normalised.split('\n') if l.strip()]

        while lines and not lines[0]: lines.pop(0)
        while lines and not lines[-1]: lines.pop()

    except Exception as e:
        print(f"Error loading lyrics: {e}")
        time.sleep(2)
        return

    from music_library import get_song_duration
    total = get_song_duration(file_path) or 999

    # VLC playback — stderr suppressed to kill codec noise
    try:
        devnull = os.open(os.devnull, os.O_WRONLY)
        old_stderr = os.dup(2)
        os.dup2(devnull, 2)
        os.close(devnull)
        instance = vlc.Instance('--no-video', '--quiet')
        media = instance.media_new(file_path)
        mp = instance.media_player_new()
        mp.set_media(media)
        mp.play()
        time.sleep(0.3)
    except Exception as e:
        os.dup2(old_stderr, 2)
        os.close(old_stderr)
        print(f"Error initialising audio: {e}")
        time.sleep(3)
        return

    cfg        = load_config()
    offset_ms  = -int(cfg.get("lyric_lead_in", 0.0) * 1000)
    synced_data    = []
    just_marked_at = None
    mark_flash_end = 0
    start_time = time.time()

    try:
        with raw_mode(sys.stdin):
            i = 0
            last_ui_update = 0
            ui_update_interval = 0.05
            select_timeout = 0.001

            while i < len(lines):
                now = time.time()
                if now - last_ui_update > ui_update_interval:
                    elapsed = mp.get_time() / 1000.0
                    flash = just_marked_at if now < mark_flash_end else None
                    _draw(file_path, lines, i, start_time, total, len(synced_data), flash, offset_ms=offset_ms)
                    _draw_progress(elapsed, total, ui_utils.get_terminal_width())
                    last_ui_update = now

                rlist, _, _ = select.select([sys.stdin], [], [], select_timeout)
                if rlist:
                    ts = mp.get_time()  # milliseconds from VLC
                    c = sys.stdin.read(1)
                    if c.lower() == 'q':
                        raise KeyboardInterrupt
                    if c in (' ', '\r', '\n'):
                        synced_data.append((i, ts))
                        just_marked_at = ts
                        mark_flash_end = time.time() + 1.2
                        i += 1
                        last_ui_update = 0

    except KeyboardInterrupt:
        pass
    finally:
        mp.stop()
        os.dup2(old_stderr, 2)
        os.close(old_stderr)

    ui_utils.clear_screen()
    if synced_data:
        print(f"{GREEN}{BOLD}✓ Synced {len(synced_data)} of {len(lines)} lines{RESET}")
        _save_sylt(file_path, lines, synced_data, offset_ms)
    else:
        print(f"{DIM}No timestamps recorded.{RESET}")
    time.sleep(1.5)


def _save_sylt(file_path: str, lines: list, synced_data: list, offset_ms: int = 0) -> None:
    """Write synced timestamps back to the file as a SYLT ID3 tag."""
    from mutagen.id3 import ID3, SYLT, ID3NoHeaderError

    # synced_data is [(line_idx, timestamp_ms), ...] where the timestamp is
    # when the user pressed the key — i.e. the cue point FOR that line.
    ts_map = {line_idx: ts for line_idx, ts in synced_data}
    sylt_entries = []

    for idx in range(len(lines)):
        if idx in ts_map:
            sylt_entries.append((lines[idx], max(0, ts_map[idx] + offset_ms)))

    try:
        try:
            audio = ID3(file_path)
        except ID3NoHeaderError:
            audio = ID3()

        audio.delall('SYLT')
        audio.add(SYLT(
            encoding=3,
            lang='eng',
            format=2,   # milliseconds
            type=1,     # lyrics
            text=sylt_entries
        ))
        audio.save(file_path, v2_version=3)
        print(f"{GREEN}Saved SYLT to {os.path.basename(file_path)}{RESET}")
    except Exception as e:
        print(f"\033[1;31mFailed to save SYLT: {e}\033[0m")