"""
Lyric synchronization tool for matching lyrics to timestamps.
"""
from __future__ import annotations
import sys
import time
import os
 
import vlc
from mutagen.id3 import ID3
 
from src.utils import ui_utils
from src.utils.terminal_input import raw_mode, get_key_non_blocking
 
def normalize_lyric_newlines(text: str) -> str:
    """Normalize CRLF/CR newlines to LF and strip trailing spaces.
 
    This mirrors the behavior expected by the rest of this module: it
    converts \r\n and \r to \n and leaves blank lines so the caller
    can decide how to handle them.
    """
    if text is None:
        return ""
    # Normalize different newline styles to \n
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    # Strip trailing whitespace on each line but preserve blank lines
    lines = [line.rstrip() for line in normalized.split("\n")]
    return "\n".join(lines)
 
from src.config import load_config
 
from src.utils.ui_utils import Colors as C
 
PROGRESS_ROW = 4
 
 
def _draw_progress(elapsed: float, total: float, cols: int) -> None:
    """Draw progress bar for lyric sync."""
    progress    = min(elapsed / total, 1.0) if total else 0
    bar         = ui_utils.get_progress_bar(progress, cols - 16)
    elapsed_str = ui_utils.format_time(elapsed)
    total_str   = ui_utils.format_time(total)
    sys.stdout.write(
        f"\033[{PROGRESS_ROW};1H\033[K"
        f"  {bar}  {C.YELLOW}{elapsed_str}{C.DIM}/{C.RESET}{C.YELLOW}{total_str}{C.RESET}"
    )
    sys.stdout.flush()
 
 
def _draw(file_path: str, lines: list, i: int, start_time: float, total: float, marked_count: int, just_marked_at: float | None = None, offset_ms: int = 0) -> None:
    """Draw lyric sync interface."""
    cols = ui_utils.get_terminal_width()
    ui_utils.clear_screen()
 
    print(f"{C.CYAN}{ui_utils.divider(cols)}{C.RESET}")
    title = os.path.basename(file_path)
    print(f"{C.BOLD}  LYRIC SYNC  {C.DIM}│{C.RESET}  {C.PRIMARY}{title}{C.RESET}")
    print(f"{C.CYAN}{ui_utils.divider(cols)}{C.RESET}")
 
    print()  # placeholder for progress bar
    print(f"{C.DIM}{'─' * cols}{C.RESET}")
 
    line_num   = max(i, 0) + 1
    line_str   = f"  Line {line_num} of {len(lines)}"
    marked_str = f"✓ {marked_count} marked"
    padding    = cols - len(line_str) - len(marked_str) - 2
    print(f"{C.PRIMARY}{line_str}{C.RESET}{' ' * max(padding, 1)}{C.GREEN}{marked_str}{C.RESET}")
    print()
 
    prev = lines[i - 1] if i > 0 else None
    if prev:
        print(f"  {C.RED}{C.DIM}{prev}{C.RESET}")
    else:
        print(f"  {C.DIM}— start —{C.RESET}")
    print()
 
    curr = lines[i] if i >= 0 else "GET READY…"
    print(f"  {C.PRIMARY}{C.BOLD}▶  {curr}{C.RESET}")
    print()
 
    nxt = lines[i + 1] if i < len(lines) - 1 else None
    if nxt:
        print(f"  {C.GREEN}{nxt}{C.RESET}")
    else:
        print(f"  {C.DIM}— end —{C.RESET}")
 
    print()
    print(f"{C.DIM}{ui_utils.divider(cols)}{C.RESET}")
 
    if just_marked_at is not None:
        marked_time = ui_utils.format_time(just_marked_at / 1000)
        print(f"  {C.GREEN}{C.BOLD}✓ Marked at {marked_time}{C.RESET}")
    else:
        print(f"  {C.DIM}waiting for mark…{C.RESET}")
 
    print()
    offset_str = f"{offset_ms:+d}ms"
    print(f"  {C.YELLOW}SPACE / ENTER{C.RESET}  {C.DIM}mark     {C.RESET}{C.RED}Q{C.RESET}  {C.DIM}quit     {C.RESET}{C.DIM}offset: {C.RESET}{C.YELLOW}{offset_str}{C.RESET}  {C.DIM}(set in Settings › Lyric Lead-in){C.RESET}")
    print(f"{C.CYAN}{'─' * cols}{C.RESET}")
 
    sys.stdout.flush()
 
 
def sync_lyrics(file_path: str) -> None:
    """Interactive lyric synchronization: match lyrics to timestamps."""
    try:
        audio = ID3(file_path)
        uslt_tags = audio.getall('USLT')
 
        if not uslt_tags:
            print(f"No lyrics found in {os.path.basename(file_path)}")
            time.sleep(2)
            return
 
        raw_lyrics = uslt_tags[0].text
        normalized = normalize_lyric_newlines(raw_lyrics)
        lines = ["START"]
 
        for l in normalized.split('\n'):
            if l.strip(): lines.append(l.strip())
 
        while lines and not lines[0]: lines.pop(0)
        while lines and not lines[-1]: lines.pop()
 
    except Exception as e:
        print(f"Error loading lyrics: {e}")
        time.sleep(2)
        return
 
    from src.music_library import get_song_duration
    total = get_song_duration(file_path) or 999
 
    # VLC playback — stderr suppressed to kill codec noise
    old_stderr = os.dup(2)
    try:
        devnull = os.open(os.devnull, os.O_WRONLY)
        os.dup2(devnull, 2)
        os.close(devnull)
        instance = vlc.Instance('--no-video', '--quiet')
        assert instance is not None
        media = instance.media_new(file_path)
        assert media is not None
        mp = instance.media_player_new()
        assert mp is not None
        mp.set_media(media)
        mp.play()
        time.sleep(0.3)
    except Exception as e:
        os.dup2(old_stderr, 2)
        os.close(old_stderr)
        print(f"Error initializing audio: {e}")
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
 
                    ts = mp.get_time()  # milliseconds from VLC
                    c = get_key_non_blocking()
                    if c == 'q':
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
        print(f"{C.GREEN}{C.BOLD}✓ Synced {len(synced_data)} of {len(lines)} lines{C.RESET}")
        _save_sylt(file_path, lines[1:], synced_data, offset_ms)
    else:
        print(f"{C.DIM}No timestamps recorded.{C.RESET}")
    time.sleep(1.5)
 
 
def _save_sylt(file_path: str, lines: list, synced_data: list, offset_ms: int = 0) -> None:
    """Write synced timestamps back to the file as a SYLT ID3 tag."""
    from mutagen.id3 import ID3
    from mutagen.id3._frames import SYLT
    from mutagen.id3._util import ID3NoHeaderError
 
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
        print(f"{C.GREEN}Saved SYLT to {os.path.basename(file_path)}{C.RESET}")
    except Exception as e:
        print(f"\033[1;31mFailed to save SYLT: {e}\033[0m")
 
 
def save_sylt_entries(file_path: str, sylt_entries: list[tuple[str, int]]) -> None:
    """Write a SYLT ID3 frame from explicit (line, timestamp_ms) entries."""
    from mutagen.id3 import ID3
    from mutagen.id3._frames import SYLT
    from mutagen.id3._util import ID3NoHeaderError
 
    try:
        try:
            audio = ID3(file_path)
        except ID3NoHeaderError:
            audio = ID3()
 
        audio.delall('SYLT')
        audio.add(SYLT(
            encoding=3,
            lang='eng',
            format=2,
            type=1,
            text=sylt_entries
        ))
        audio.save(file_path, v2_version=3)
    except Exception as exc:
        print(f"\033[1;31mFailed to save SYLT: {exc}\033[0m")