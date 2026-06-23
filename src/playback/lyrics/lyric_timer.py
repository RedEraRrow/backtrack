from __future__ import annotations
import sys
import time
import os

try:
    import vlc
    _HAS_VLC = True
except ImportError:
    vlc = None
    _HAS_VLC = False
from mutagen.id3 import ID3

from src.config import load_config
from src.playback.lyrics.lyrics import normalize_lyric_newlines
from src.utils import ui_utils
from src.utils.terminal_input import raw_mode, get_key_non_blocking
from src.utils.ui_utils import Colors as C

PROGRESS_ROW = 4


def _draw_progress(elapsed: float, total: float, cols: int) -> None:
    progress    = min(elapsed / total, 1.0) if total else 0
    bar         = ui_utils.get_progress_bar(progress, cols - 18)
    elapsed_str = ui_utils.format_time(elapsed)
    total_str   = ui_utils.format_time(total)
    sys.stdout.write(
        f"\033[{PROGRESS_ROW + 1};1H\033[K"
        f"  {bar}  {C.YELLOW}{elapsed_str}{C.DIM}/{C.RESET}{C.YELLOW}{total_str}{C.RESET}"
    )
    sys.stdout.flush()


def _draw(file_path: str, lines: list, i: int, start_time: float, total: float, marked_count: int, just_marked_at: float | None = None, offset_ms: int = 0) -> None:
    cols = ui_utils.get_terminal_width()
    rows = ui_utils.get_terminal_height()
    # Clear using home+erase (no \033[2J scroll-to-scrollback side-effect).
    sys.stdout.write("\033[H\033[3J\033[J")

    def rw(r: int, content: str) -> None:
        """Write content at absolute row r, bounded to terminal height."""
        if r <= rows - 1:
            sys.stdout.write(f"\033[{r};1H\033[K{content}")

    title = os.path.basename(file_path)
    rw(2, f"{C.CYAN}{ui_utils.divider(cols)}{C.RESET}")
    rw(3, f"   {C.BOLD}LYRIC SYNC  {C.DIM}│{C.RESET}  {C.PRIMARY}{title}{C.RESET}")
    rw(4, f"{C.CYAN}{ui_utils.divider(cols)}{C.RESET}")
    rw(PROGRESS_ROW + 2, f"{C.DIM}{'─' * cols}{C.RESET}")

    line_num   = max(i, 0) + 1
    line_str   = f"  Line {line_num} of {len(lines)}"
    marked_str = f"✓ {marked_count} marked"
    padding    = (cols - 2) - len(line_str) - len(marked_str) - 2
    rw(7, f" {C.PRIMARY}{line_str}{C.RESET}{' ' * max(padding, 1)}{C.GREEN}{marked_str}{C.RESET}")

    prev = lines[i - 1] if i > 0 else None
    rw(9, f"   {C.ACCENT}{C.DIM}{prev}{C.RESET}" if prev else f"   {C.DIM}— start —{C.RESET}")

    curr = lines[i] if i >= 0 else "GET READY…"
    rw(11, f"   {C.PRIMARY}{C.BOLD}▶  {curr}{C.RESET}")

    nxt = lines[i + 1] if i < len(lines) - 1 else None
    rw(13, f"   {C.GREEN}{nxt}{C.RESET}" if nxt else f"   {C.DIM}— end —{C.RESET}")

    rw(15, f"{C.DIM}{ui_utils.divider(cols)}{C.RESET}")

    if just_marked_at is not None:
        marked_time = ui_utils.format_time(just_marked_at / 1000)
        rw(16, f"   {C.GREEN}{C.BOLD}✓ Marked at {marked_time}{C.RESET}")
    else:
        rw(16, f"   {C.DIM}waiting for mark…{C.RESET}")

    offset_str = f"{offset_ms:+d}ms"
    rw(18, f"   {C.YELLOW}SPACE / ENTER{C.RESET}  {C.DIM}mark     {C.RESET}{C.ACCENT}Q{C.RESET}  {C.DIM}quit     {C.RESET}{C.DIM}offset: {C.RESET}{C.YELLOW}{offset_str}{C.RESET}  {C.DIM}(set in Settings › Lyric Lead-in){C.RESET}")
    rw(19, f"{C.CYAN}{'─' * cols}{C.RESET}")

    sys.stdout.flush()


def sync_lyrics(file_path: str) -> None:
    if not _HAS_VLC:
        ui_utils.show_status("VLC is not available; cannot sync lyrics.", duration=4.0)
        return

    try:
        audio = ID3(file_path)
        uslt_tags = audio.getall('USLT')

        if not uslt_tags:
            ui_utils.show_status(f"No lyrics found in {os.path.basename(file_path)}")
            return

        raw_lyrics = uslt_tags[0].text
        normalized = normalize_lyric_newlines(raw_lyrics)
        lines = ["START"]

        for line in normalized.split('\n'):
            if line.strip(): lines.append(line.strip())

        while lines and not lines[0]: lines.pop(0)
        while lines and not lines[-1]: lines.pop()

    except (OSError, ImportError, AssertionError) as e:
        ui_utils.show_status(f"Error loading lyrics: {e}", duration=4.0)
        return

    from src.music_library import get_song_duration
    total = get_song_duration(file_path) or 999

    # VLC playback — stderr suppressed to kill codec noise
    old_stderr = os.dup(2)
    try:
        devnull = os.open(os.devnull, os.O_WRONLY)
        os.dup2(devnull, 2)
        os.close(devnull)
        instance = vlc.Instance('--no-video', '--quiet')  # type: ignore[union-attr]
        assert instance is not None
        media = instance.media_new(file_path)
        assert media is not None
        mp = instance.media_player_new()
        assert mp is not None
        mp.set_media(media)
        mp.play()
        time.sleep(0.3)
    except (OSError, ImportError, AssertionError) as e:
        os.dup2(old_stderr, 2)
        os.close(old_stderr)
        ui_utils.show_status(f"Audio init error: {e}", duration=4.0)
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
        _save_sylt(file_path, lines[1:], synced_data, offset_ms)
    else:
        ui_utils.show_status("No timestamps recorded.")


def _save_sylt(file_path: str, lines: list, synced_data: list, offset_ms: int = 0) -> None:
    # synced_data is [(line_idx, timestamp_ms), …] where the timestamp is when the user pressed the key.
    # synced_data indices are 1-based (0 = "START" marker, 1 = first lyric).
    # lines here is lines[1:] (actual lyrics, 0-indexed), so add 1 to align.
    ts_map = {line_idx: ts for line_idx, ts in synced_data}
    sylt_entries = []

    for idx in range(len(lines)):
        map_key = idx + 1
        if map_key in ts_map:
            sylt_entries.append((lines[idx], max(0, ts_map[map_key] + offset_ms)))

    save_sylt_entries(file_path, sylt_entries)
    if sylt_entries:
        ui_utils.show_status(f"Synced {len(sylt_entries)} lines — saved to {os.path.basename(file_path)}")


def save_sylt_entries(file_path: str, sylt_entries: list[tuple[str, int]]) -> None:
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
    except (OSError, Exception) as exc:
        ui_utils.show_status(f"Failed to save SYLT: {exc}", duration=4.0)
        raise
