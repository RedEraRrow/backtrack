"""
Metadata browser for inspecting and managing track metadata.
"""
from __future__ import annotations
import os
import re
import sys
import time
import textwrap
import pyperclip
import subprocess
import numpy as np
import cv2
from collections import Counter
import tempfile

from src.utils import prompt
from src.playback.lyrics.lyric_timer import save_sylt_entries
from mutagen.id3 import ID3
from mutagen.id3._frames import USLT, COMM, SYLT, TextFrame, APIC, TXXX

from src.utils import ui_utils
from src.utils.ui_utils import Colors as C
from src.art.album_art import render_with_viu
from src.music_library import refresh_library_entry


# ============================================================================
# Helper functions for widget-based editing
# ============================================================================

def _ms_to_hms_display(milliseconds: int | str) -> str:
    """Convert milliseconds to HH:MM:SS.mmm display format."""
    try:
        ms = int(milliseconds) if milliseconds else 0
    except (ValueError, TypeError):
        return "00:00:00.000"
    
    hours = ms // (1000 * 3600)
    minutes = (ms % (1000 * 3600)) // (1000 * 60)
    seconds = (ms % (1000 * 60)) // 1000
    millis = ms % 1000
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}.{millis:03d}"


def _hms_display_to_ms(time_str: str) -> int:
    """Convert HH:MM:SS.mmm display format back to milliseconds."""
    try:
        parts = time_str.split(':')
        if len(parts) < 2:
            return 0
        
        h = int(parts[0])
        m = int(parts[1])
        
        sec_parts = parts[2].split('.') if len(parts) > 2 else ['0', '0']
        s = int(sec_parts[0])
        ms = int((sec_parts[1] + "000")[:3]) if len(sec_parts) > 1 else 0
        
        return h * 3600000 + m * 60000 + s * 1000 + ms
    except (ValueError, IndexError):
        return 0


def _get_image_from_apic(apic_frame: APIC) -> tuple:
    """Decode APIC frame to a numpy image array. Returns (ndarray | None, mime_str)."""
    try:
        img_data  = getattr(apic_frame, 'data', b"")
        mime_type = getattr(apic_frame, 'mime', "image/jpeg")
        if not img_data:
            return None, mime_type
        nparr = np.frombuffer(img_data, np.uint8)
        image = cv2.imdecode(nparr, cv2.IMREAD_COLOR)
        return image, mime_type, img_data
    except Exception as e:
        return None, "unknown", b""


def _convert_from_apic_to_viu(apic_frame: APIC, width: int = 80) -> str:
    """Convert APIC tag data to block art string via viu."""
    img_bytes = getattr(apic_frame, 'data', None)
    if not img_bytes:
        return "Error: No image data in tag."
    return render_with_viu(img_bytes, width=width, is_bytes=True)


def _get_art_width() -> int:
    """Choose an art width based on current terminal size."""
    cols = ui_utils.get_terminal_width()
    return max(20, min(cols - 8, 100))


_LRC_TIMESTAMP_RE = re.compile(r'\[(\d+):(\d+)(?:[.:](\d{1,3}))?\]')
_LRC_META_RE = re.compile(r'^\s*\[(ti|ar|al|by|offset|re|ve)\s*:.+\]\s*$', re.I)
_SPEAKER_RE = re.compile(r'^[A-Z][A-Z0-9 .\'"?!-]{1,40}\s*:\s*')


def _parse_lrc_file(lrc_path: str) -> list[tuple[str, int | None]]:
    with open(lrc_path, "r", encoding="utf-8") as f:
        raw = f.read()

    entries: list[tuple[str, int | None]] = []
    for raw_line in raw.splitlines():
        if _LRC_META_RE.match(raw_line):
            continue
        timestamps = list(_LRC_TIMESTAMP_RE.finditer(raw_line))
        text = _LRC_TIMESTAMP_RE.sub("", raw_line).strip()
        if not text and not timestamps:
            continue
        if timestamps:
            for match in timestamps:
                mins = int(match.group(1))
                secs = int(match.group(2))
                frac = match.group(3) or "0"
                ms = int(frac.ljust(3, "0")[:3])
                entries.append((text, mins * 60_000 + secs * 1_000 + ms))
        else:
            entries.append((text, None))
    return entries

def _import_from_lrc(file_path: str, audio: ID3, choice: str) -> None:
    default_lrc = os.path.splitext(file_path)[0] + ".lrc"
    lrc_path = prompt.text("LRC file path:", default=default_lrc)
    if not lrc_path:
        return
    if not os.path.exists(lrc_path):
        print(f"LRC file not found: {lrc_path}")
        time.sleep(1.5)
        return

    entries = _parse_lrc_file(lrc_path)
    if not entries:
        print("No usable lines found in LRC file.")
        time.sleep(1.5)
        return

    timed = [entry for entry in entries if entry[1] is not None]
    if timed:
        sylt_data = [(text, int(timestamp)) for text, timestamp in timed if text and timestamp is not None]
        if not sylt_data:
            print("LRC file contained timestamps but no text lines.")
            time.sleep(1.5)
            return
        save_sylt_entries(file_path, sylt_data)
        print(f"Imported {len(sylt_data)} timed lines from {os.path.basename(lrc_path)} into SYLT.")
    else:
        if choice.startswith('SYLT'):
            print("LRC file did not contain timestamps, so it cannot be imported into SYLT.")
            time.sleep(1.5)
            return
        uslt_text = "\n".join(text for text, _ in entries)
        if not uslt_text.strip():
            print("LRC file did not contain any text lines.")
            time.sleep(1.5)
            return
        audio.delall('USLT')
        audio.add(USLT(encoding=3, lang='eng', desc='', text=uslt_text))
        audio.save(v2_version=3)
        print(f"Imported lyrics from {os.path.basename(lrc_path)} into USLT.")

    time.sleep(1.5)

def _open_apic_preview(apic_frame: APIC) -> bool:
    """Open APIC image in system preview. Returns True if successful."""
    
    image, mime_type, img_bytes = _get_image_from_apic(apic_frame)
    
    # Safe check for empty data (handles None, empty lists, and empty numpy arrays)
    if img_bytes is None or (hasattr(img_bytes, 'size') and img_bytes.size == 0) or not len(img_bytes):
        return False
        
    try:
        ext = {
            'image/jpeg': '.jpg', 'image/jpg': '.jpg',
            'image/png': '.png',  'image/gif': '.gif',
        }.get(mime_type, '.jpg')
        
        with tempfile.NamedTemporaryFile(suffix=ext, delete=False) as tmp:
            
            # BYPASS PIL: Convert NumPy array back to standard Python bytes
            if hasattr(img_bytes, 'tobytes'):
                data_to_write = img_bytes.tobytes()
            elif hasattr(img_bytes, 'tostring'):  # Fallback for older NumPy versions
                data_to_write = img_bytes.tostring()
            else:
                data_to_write = img_bytes  # It was already standard bytes
                
            tmp.write(data_to_write)
            tmp_path = tmp.name
        
        # Cross-platform open commands
        if sys.platform == 'darwin':      # macOS
            subprocess.run(['open', tmp_path], check=True)
        elif sys.platform == 'win32':     # Windows
            os.startfile(tmp_path)
        elif sys.platform.startswith('linux'): # Linux
            subprocess.run(['xdg-open', tmp_path], check=True)
        else:
            raise OSError(f"Unsupported operating system: {sys.platform}")
            
        return True
    except Exception as e:
        print(f"Error opening preview: {e}")
        return False
    
def _edit_apic_tag(audio_obj: ID3, tag_name: str, apic_frame: APIC) -> bool:
    """Edit APIC (image) tag with options to replace image, view modes, etc."""
    view_mode = "viu"  # viu, raw, or image
    def _apic_header() -> list[str]:
        art    = _convert_from_apic_to_viu(apic_frame, width=_get_art_width())
        image, mime, img_data = _get_image_from_apic(apic_frame)
        h = w = 0
        if image is not None:
            h, w = image.shape[:2]
        kb = len(img_data) / 1024
        info = f"{w}×{h}px  {mime}  {kb:.0f} KB"
        lines = [
            f"{C.BOLD}{tag_name}{C.RESET} {"(Album Art)"}",
            f"{C.DIM}{info}{C.RESET}",
            f"{C.DIM}{'─' * ui_utils.get_terminal_width()}{C.RESET}",
        ]
        if view_mode == "viu":
            art = _convert_from_apic_to_viu(apic_frame, width=_get_art_width())
            lines.extend(art.splitlines())
        elif view_mode == "raw":
            lines.append(repr(apic_frame))
        elif view_mode == "info":
            image, mime, img_data = _get_image_from_apic(apic_frame)
            if image is not None:
                h, w = image.shape[:2]
                channels = image.shape[2] if len(image.shape) == 3 else 1
                color_mode = {1: "Grayscale", 3: "RGB", 4: "RGBA"}.get(channels, f"{channels}ch")
                size_kb = len(getattr(apic_frame, 'data', b"")) / 1024
                lines += [
                    f"  MIME type   : {mime}",
                    f"  Description : {getattr(apic_frame, 'desc', '') or '(none)'}",
                    f"  Dimensions  : {w} × {h} px",
                    f"  Color mode : {color_mode}",
                    f"  File size   : {size_kb:.1f} KB ({len(getattr(apic_frame, 'data', b'')):,} bytes)",
                    f"  Encoding    : {getattr(apic_frame, 'encoding', 3)}",
                ]
            else:
                lines.append("Could not read image information.")
        lines.append(f"{C.DIM}{'─' * ui_utils.get_terminal_width()}{C.RESET}")
        return lines

    while True:
        actions = []
        if view_mode != "viu":
            actions.append("View Album Art")
        if view_mode != "raw":
            actions.append("View as Raw Data")
        if view_mode != "info":
            actions.append("View as Info")
        actions.extend(["Open in Preview", "Replace Image", "Edit Description", "Back"])

        action = prompt.select("Action:", choices=actions, header=_apic_header)

        if action == "View as Album Art":
            view_mode = "viu"
        elif action == "View as Raw Data":
            view_mode = "raw"
        elif action == "View as Info":
            view_mode = "info"
        elif action == "Open in Preview":
            if _open_apic_preview(apic_frame):
                print("Opening preview...")
                time.sleep(2)
            else:
                print("Could not open preview.")
                time.sleep(1.5)
        elif action == "Replace Image":
            image_path = prompt.path("Path to new image file:")
            if image_path and os.path.isfile(image_path):
                try:
                    with open(image_path, 'rb') as f:
                        new_data = f.read()

                    # Determine MIME type
                    ext = os.path.splitext(image_path)[1].lower()
                    mime_map = {'.jpg': 'image/jpeg', '.jpeg': 'image/jpeg', '.png': 'image/png', '.gif': 'image/gif'}
                    mime_type = mime_map.get(ext, 'image/jpeg')

                    # Create new APIC frame
                    new_frame = APIC(
                        encoding=3,
                        mime=mime_type,
                        type=3,
                        desc=getattr(apic_frame, 'desc', ''),
                        data=new_data
                    )
                    audio_obj.delall(tag_name)
                    audio_obj.add(new_frame)
                    apic_frame = new_frame
                    print("Image replaced successfully!")
                    time.sleep(1)
                except Exception as e:
                    print(f"Error replacing image: {e}")
                    time.sleep(1.5)
            elif image_path:
                print("File not found.")
                time.sleep(1)
        elif action == "Edit Description":
            new_desc = prompt.text(f"Description (current: '{getattr(apic_frame, 'desc', '')}'):", default=getattr(apic_frame, 'desc', ''))
            if new_desc is not None:
                apic_frame.desc = new_desc
                print("Description updated.")
                time.sleep(1)
        elif not action or action == "Back":
            audio_obj.save(v2_version=3)
            return True

def _edit_text_in_editor(initial_text: str) -> str | None:
    """Open system editor for long text (USLT lyrics etc)."""
    import tempfile
    with tempfile.NamedTemporaryFile(suffix=".txt", mode='w+', encoding='utf-8', delete=False) as tf:
        tf.write(initial_text)
        temp_path = tf.name
    try:
        editor = os.environ.get('EDITOR', 'nano')
        subprocess.run([editor, temp_path], check=True)
        with open(temp_path, 'r', encoding='utf-8') as f:
            result = f.read().strip()
        # An empty result after editing USLT is almost certainly a mistake
        return result if result else None
    except Exception as e:
        print(f"Error launching editor: {e}")
        return None
    finally:
        if os.path.exists(temp_path):
            os.remove(temp_path)


def _edit_inline(label: str, initial: str) -> str | None:
    """Simple inline prompt for short single-line tag values."""
    from src.utils import prompt as _prompt
    result = _prompt.text(f"{label}:", default=initial)
    # Distinguish explicit clear (user deleted all) from cancel (Ctrl-C → None)
    return result

def _create_frame(frame_id: str, value: str):
    """Create a new ID3 frame for the given ID and value."""
    from mutagen.id3 import Frames
    from mutagen.id3._frames import COMM, USLT

    parts   = frame_id.split(':')
    base_id = parts[0].upper()
    desc    = parts[1] if len(parts) > 1 else ''
    lang    = parts[2] if (len(parts) > 2 and parts[2]) else 'eng'

    if base_id == 'COMM':
        return COMM(encoding=3, lang=lang, desc=desc, text=[value])
    if base_id == 'USLT':
        return USLT(encoding=3, lang=lang, desc=desc, text=value)
    if base_id == 'TXXX':
        return TXXX(encoding=3, desc=desc, text=[value])

    frame_cls = Frames.get(base_id)
    if frame_cls is None:
        raise ValueError(f"Unknown ID3 frame ID: {base_id!r}")
    return frame_cls(encoding=3, text=[value])

def perform_rename(audio_obj: ID3, old_frame: TextFrame, new_id: str) -> bool:
    """Re-assign frame ID to a new value, handling special bracket syntax."""
    try:
        lang = 'eng'
        if '[' in new_id and ']' in new_id:
            parts = new_id.split('[')
            base_id = parts[0]
            lang = parts[1].split(']')[0]
        else:
            base_id = new_id

        if base_id.startswith('COMM'):
            new_frame = COMM(encoding=3, lang=lang, desc='', text=getattr(old_frame, 'text', [''])[0])
        elif base_id.startswith('USLT'):
            new_frame = USLT(encoding=3, lang=lang, desc='', text=getattr(old_frame, 'text', [''])[0])
        else:
            from mutagen.id3 import Frames
            frame_cls = Frames.get(base_id)
            if frame_cls is None:
                print(f"Rename failed: unknown frame ID {base_id!r}")
                return False
            new_frame = frame_cls(encoding=3, text=getattr(old_frame, 'text', ['']))

        audio_obj.add(new_frame)
        return True
    except Exception as e:
        print(f"Rename failed: {e}")
        return False

def inspect_tag_loop(file_path: str, library_metadata: dict | None = None, library: list | None = None) -> None:
    """Browse and edit ID3 tags for a single file.

    library, if provided, is updated in-place and the cache saved after any tag change.
    """
    from src.utils.ui_utils import format_time

    from src.music_library import TAG_MAP as _TAG_MAP

    show_xml = False  # default: hide XML when ID3 available

    def _save(audio_obj):
        """Save tags and propagate to library + cache."""
        audio_obj.save(v2_version=3)
        if library is not None:
            try:
                fresh = refresh_library_entry(library, file_path)
                if library_metadata is not None:
                    library_metadata.update(fresh)
            except Exception as e:
                print(f"Warning: cache update failed: {e}")
                time.sleep(1)

    def _main_header() -> list[str]:
        cols = ui_utils.get_terminal_width()
        name = os.path.basename(file_path)
        ext  = os.path.splitext(file_path)[1].upper().lstrip('.')

        size_str = ""
        try:
            size_str = f"  {os.path.getsize(file_path) / (1024*1024):.1f} MB"
        except OSError:
            pass
        inner = cols - 2
        content_right = f"  [{ext}]{size_str}  "
        reserved_space = len(content_right) + 2
        content_left = f"  {ui_utils.truncate_text(name, max(1, inner - reserved_space))}"
        padding = ' ' * max(0, inner - len(content_left) - len(content_right))
        lines = [
            f"{C.DIM}╭{'─' * inner}╮{C.RESET}",
            f"{C.DIM}│{C.RESET}{C.BOLD}{content_left}{C.RESET}{C.DIM}{content_right}{padding}│{C.RESET}",
            f"{C.DIM}╰{'─' * inner}╯{C.RESET}",
            "",
        ]
        xml_data = library_metadata.get('xml_data') if library_metadata else None
        has_id3  = os.path.splitext(file_path)[1].lower() == '.mp3'
        if xml_data and (show_xml or not has_id3):
            lines.extend(ui_utils.get_xml_metadata_lines(xml_data))
        return lines

    clean_data = ""

    while True:
        cols = ui_utils.get_terminal_width()

        xml_data = library_metadata.get('xml_data') if library_metadata else None
        has_id3 = os.path.splitext(file_path)[1].lower() == '.mp3'

        try:
            audio = ID3(file_path)
            tags  = sorted(audio.keys())
        except Exception as e:
            xml_data    = library_metadata.get('xml_data') if library_metadata else None
            display_data = xml_data or library_metadata
            if display_data:
                ui_utils.clear_screen()
                print(f"INSPECTING: {os.path.basename(file_path)}")
                print("\nNo ID3 tags found (M4P or MP4 file)\n")
                ui_utils.display_xml_metadata({'xml_data': display_data})
                print("\nThis file uses Apple Music metadata from Library.xml")
                input("\nPress Enter to continue...")
            else:
                print(f"Error loading tags: {e}")
                input("Press Enter to continue...")
            break

        # Compute column widths from actual tags present
        ALIAS_MAX = 22
        TAG_MAX   = 28
        def _alias(tag):
            return _TAG_MAP.get(tag.split(':')[0].split('[')[0], '')
        tag_col   = min(TAG_MAX,   max((len(t) for t in tags), default=6))
        alias_col = min(ALIAS_MAX, max((len(_alias(t)) + 3 for t in tags), default=0))

        def _tag_title(tag):
            alias = _alias(tag)
            tag_disp = tag if len(tag) <= tag_col else tag[:tag_col - 1] + "…"
            if alias:
                a = f"({alias})"
                if len(a) > alias_col:
                    a = a[:alias_col - 2] + "…)"
                alias_str = f" {a}"
            else:
                alias_str = ""
            raw = audio[tag]
            if tag.startswith('TMCL') or tag.startswith('TIPL'):
                preview = f"{len(raw.people)} people"
            elif tag.startswith('APIC'):
                preview = f"image/{raw.mime}  {len(raw.data):,} bytes"
            elif tag.startswith('SYLT'):
                preview = f"{len(raw.text)} lines"
            else:
                val = str(raw)
                preview = repr(val)[:cols - tag_col - alias_col - 12]
            return f"{tag_disp:<{tag_col}} {alias_str:<{alias_col + 1}} | {preview}"

        tag_choices = [prompt.Choice(title=_tag_title(t), value=t) for t in tags]

        xml_toggle = "Hide XML data" if show_xml else "Show XML data"
        extras = (["Add New Tag"] if has_id3 else []) + ([xml_toggle] if xml_data and has_id3 else []) + ["Back to track list"]

        choice = prompt.select(
            "Select a tag to manage:",
            choices=tag_choices + extras,
            header=_main_header
        )

        if choice == xml_toggle:
            show_xml = not show_xml
            continue

        if choice == "Add New Tag":
            target_id = prompt.text("Enter Tag ID to add (e.g. TPE2, COMM::eng, TXXX:MyCustomTag:):")
            if target_id:
                target_val = prompt.text(f"Enter value for {target_id}:")
                if target_val is not None:
                    audio.add(_create_frame(target_id, target_val))
                    _save(audio)
            continue

        if not choice or choice == "Back to track list":
            break

        while True:
            # Re-read audio fresh each inner loop so edits show immediately
            audio = ID3(file_path)
            if choice not in audio:
                # Tag was deleted — return to outer list
                break
            raw_val = audio[choice]

            if choice.startswith('SYLT'):
                display_lines = [f"[{format_time(ts/1000)}] {txt}" for txt, ts in raw_val.text]
                full_text = "\n".join(display_lines)
            elif choice.startswith('APIC'):
                full_text = _convert_from_apic_to_viu(raw_val, width=_get_art_width())
            elif choice.startswith(('TMCL', 'TIPL')):
                full_text = str(raw_val)
            elif choice.startswith('TRCK') or choice.startswith('TPOS'):
                # Show track/total as "3/12" instead of "3/12" (with proper fraction slash)
                full_text = str(raw_val).replace('/', '⁄')
            else:
                # Always join as a plain string — don't expose the internal list
                full_text = "".join(str(t) for t in raw_val.text) if hasattr(raw_val, 'text') else str(raw_val)

            def _tag_header() -> list[str]:
                assert choice is not None
                c     = ui_utils.get_terminal_width()
                inner = max(10, c - 4)
                alias = _TAG_MAP.get(choice.split(':')[0].split('[')[0], '')
                alias_str = f"  {C.DIM}({alias}){C.RESET}" if alias else ""

                if choice.startswith('APIC'):
                    art    = _convert_from_apic_to_viu(raw_val, width=_get_art_width())
                    image, mime, img_data = _get_image_from_apic(raw_val)
                    h = w = 0
                    if image is not None:
                        h, w = image.shape[:2]
                    kb = len(img_data) / 1024
                    info = f"{w}×{h}px  {mime}  {kb:.0f} KB"
                    return [
                        f"{C.BOLD}{choice}{C.RESET}{alias_str}",
                        f"{C.DIM}{info}{C.RESET}",
                        f"{C.DIM}{'─' * c}{C.RESET}",
                        *art.splitlines(),
                        f"{C.DIM}{'─' * c}{C.RESET}",
                    ]
                
                if choice.startswith(('TMCL', 'TIPL')):
                    cw = max(12, (inner - 6) // 2)
                    lines = [
                        f"{C.BOLD}{choice}{C.RESET}{alias_str}",
                        f"{C.DIM}{'─' * c}{C.RESET}",
                        f"  {C.DIM}{'ROLE':<{cw}}  NAME{C.RESET}",
                        f"  {'─' * (cw)}  {'─' * (inner - cw - 4)}",
                    ]
                    for role, name in raw_val.people:
                        r = ui_utils.truncate_text(role, cw)
                        n = ui_utils.truncate_text(name, inner - cw - 4)
                        lines.append(f"  {r:<{cw}}  {n}")
                    lines.append(f"{C.DIM}{'─' * c}{C.RESET}")
                    return lines

                if choice.startswith('SYLT'):
                    lines = [
                        f"{C.BOLD}{choice}{C.RESET}{alias_str}  {C.DIM}({len(raw_val.text)} lines){C.RESET}",
                        f"{C.DIM}{'─' * c}{C.RESET}",
                    ]
                    for txt, ts in raw_val.text[:6]:
                        t_fmt = ui_utils.format_time(ts / 1000)
                        lines.append(f"  {C.DIM}{t_fmt:>6}{C.RESET}  {ui_utils.truncate_text(txt, inner - 12)}")
                    if len(raw_val.text) > 6:
                        lines.append(f"  {C.DIM}… {len(raw_val.text) - 6} more{C.RESET}")
                    lines.append(f"{C.DIM}{'─' * c}{C.RESET}")
                    return lines

                body    = full_text if choice.startswith('USLT') else repr(full_text)
                wrapped = ui_utils.wrap_text(body, max_width=c, margin=6)
                return [
                    f"{C.BOLD}{choice}{C.RESET}{alias_str}",
                    f"{C.DIM}╭{'─' * inner}╮{C.RESET}",
                    *[f"{C.DIM}│{C.RESET} {ln:<{inner - 1}}{C.DIM}│{C.RESET}" for ln in wrapped[:12]],
                    f"{C.DIM}╰{'─' * inner}╯{C.RESET}",
                ]

            assert choice is not None
            actions = ["Copy to clipboard", "Paste from clipboard", "Edit content", "Rename tag", "Delete tag"]
            if choice.startswith(('USLT', 'SYLT')):
                actions.insert(0, "Import from LRC file")
            if choice.startswith('SYLT'):
                actions.insert(0, "Export to LRC file")
            if choice.startswith('APIC'):
                actions.insert(0, "Edit Image")
                actions.remove("Edit content")
                actions.remove("Rename tag")
            actions.append("Back")

            action = prompt.select("Action:", choices=actions, header=_tag_header)

            if action == "Import from LRC file":
                assert choice is not None
                _import_from_lrc(file_path, audio, choice)
                audio = ID3(file_path)
                if library is not None:
                    refresh_library_entry(library, file_path)
                break

            if action == "Export to LRC file":
                lrc_path = os.path.splitext(file_path)[0] + ".lrc"
                try:
                    with open(lrc_path, "w", encoding="utf-8") as f:
                        f.write(f"[ti:{audio.get('TIT2', 'Unknown')}]\n")
                        for txt, ts in raw_val.text:
                            mins, secs = divmod(ts // 1000, 60)
                            cs = (ts % 1000) // 10
                            f.write(f"[{mins:02d}:{secs:02d}.{cs:02d}] {txt}\n")
                    print(f"Exported: {os.path.basename(lrc_path)}")
                except Exception as e:
                    print(f"Export failed: {e}")
                time.sleep(1.5)

            elif action == "Edit Image":
                _edit_apic_tag(audio, choice, raw_val)
                audio = ID3(file_path)
                if library is not None:
                    refresh_library_entry(library, file_path)
                break

            elif action == "Copy to clipboard":
                pyperclip.copy(full_text)
                print("Copied to clipboard!")
                time.sleep(1)

            elif action == "Paste from clipboard":
                raw_clipboard = pyperclip.paste()
                if not raw_clipboard:
                    continue
                clean_data = raw_clipboard.strip().replace('\r\n', '\n').replace('\r', '\n')
                if prompt.confirm(f"Replace {choice} with clipboard content?"):
                    audio.delall(choice)
                    if choice.startswith('USLT'):
                        new_frame = USLT(encoding=3, lang='eng', desc='', text=clean_data)
                    elif choice.startswith('SYLT'):
                        sylt_lines = clean_data.split('\r')
                        sylt_data = [(line.strip(), 0) for line in sylt_lines if line.strip()]
                        new_frame = SYLT(encoding=3, lang='eng', format=2, type=1, text=sylt_data)
                    elif choice.startswith('COMM'):
                        new_frame = COMM(encoding=3, lang='eng', desc='', text=[clean_data])
                    else:
                        from mutagen.id3 import Frames as _Frames
                        base_id   = choice.split('[')[0].split(':')[0]
                        frame_cls = _Frames.get(base_id)
                        if frame_cls is None:
                            print(f"Unknown frame ID: {base_id}")
                            time.sleep(1)
                            continue
                        new_frame = frame_cls(encoding=3, text=[clean_data])
                    audio.add(new_frame)
                    _save(audio)
                    print(f"{choice} updated.")
                    time.sleep(1)
                continue

            elif action == "Rename tag":
                new_id = prompt.text("New tag ID (e.g. TPE2, COMM[eng]):")
                if not new_id:
                    print("Rename cancelled.")
                    time.sleep(1)
                    continue
                if new_id == choice:
                    print("New tag ID must be different from the current tag ID.")
                    time.sleep(1)
                    continue
                old_frame = audio.pop(choice)
                assert isinstance(new_id, str)
                if perform_rename(audio, old_frame, new_id):
                    _save(audio)
                    print(f"Renamed {choice} to {new_id}")
                    time.sleep(1)
                    break
                else:
                    audio.add(old_frame)
                    print(f"Rename failed for {choice} -> {new_id}")
                    time.sleep(1)
                    continue

            elif action == "Edit content":
                base_id = choice.split('[')[0].split(':')[0].upper()
                from mutagen.id3 import Frames as _Frames
                
                # Independent dual-value tags
                if base_id in ("TRCK", "TPOS", "MVIN"):
                    # Call the single-widget utility directly passing the tag type and current text
                    res = prompt.fraction_edit(
                        message=f"Edit current and total {"movement" if base_id == "MVIN" else "track" if base_id == "TRCK" else "disc"}s:",
                        tag=base_id,
                        value=full_text
                    )
                    
                    if res is not None:
                        def _clean_numeric(v):
                            try:
                                f = float(v.strip())
                                # Strip 3.0 down to "3", but leave float counts like 3.5 intact
                                return str(int(f)) if f.is_integer() else str(f)
                            except ValueError:
                                return v.strip()

                        final_curr = _clean_numeric(res['current'])
                        final_tot = _clean_numeric(res['total'])
                        
                        # Build output text syntax ("3.5/12" or "3.5" or raw string text name)
                        if final_tot:
                            formatted = f"{final_curr}/{final_tot}"
                        else:
                            formatted = final_curr

                        audio.delall(choice)
                        if formatted:
                            from mutagen.id3 import Frames as _Frames
                            cls = _Frames.get(base_id) or TextFrame
                            audio.add(cls(encoding=3, text=[formatted]))
                        
                        _save(audio)
                        print(f"{_TAG_MAP[base_id]} successfully updated.")
                        time.sleep(1)
                    break
                
                # ── 2. Generalized Calendar Widget Selection (Dates) ──
                elif base_id in ("TDRC", "TDAT", "TIME", "TYER", "TDRL", "TDOR", "TDTG"):
                    new_content = prompt.calendar_select(
                        message=f"Select calendar target for {base_id}:",
                        initial=full_text
                    )
                    if new_content:
                        audio.delall(choice)
                        if base_id in _Frames:
                            audio.add(_Frames[base_id](encoding=3, text=[new_content]))
                            _save(audio)
                            print(f"Calendar field {choice} committed as: {new_content}")
                            time.sleep(1)
                    break
                
                # ── 3. Generalized Playback Duration/Offsets (Timelines) ──
                elif base_id in ("TLEN", "TDLY"):
                    display_time = _ms_to_hms_display(full_text) if full_text.isdigit() else (full_text or "00:00:00.000")
                    
                    result_time = prompt.time_edit(
                        message=f"Edit timeline offset duration for {base_id}:",
                        initial=display_time
                    )
                    if result_time:
                        ms_value = str(_hms_display_to_ms(result_time))
                        audio.delall(choice)
                        if base_id in _Frames:
                            audio.add(_Frames[base_id](encoding=3, text=[ms_value]))
                            _save(audio)
                            print(f"{choice} millisecond constraint updated.")
                            time.sleep(1)
                    break

                # ── 4. Synced Lyric Frames ──
                elif choice.startswith('SYLT'):
                    new_content = prompt.text("New content (time-stamped lyrics):", default=full_text)
                    if new_content is not None:
                        sylt_lines = new_content.split('\n')
                        sylt_data = [(line.strip(), 0) for line in sylt_lines if line.strip()]
                        audio.delall(choice)
                        audio.add(SYLT(encoding=3, lang='eng', format=2, type=1, text=sylt_data))
                        _save(audio)
                        print(f"{choice} updated.")
                        time.sleep(1)
                    break

                # ── 5. Involved People / Musician Credits ──
                elif choice.startswith(('TMCL', 'TIPL')):
                    edited = prompt.list_edit("Edit people (role and name):", raw_val.people, ("ROLE", "NAME") if choice.startswith('TMCL') else ("JOB", "NAME"))
                    if edited != raw_val.people:
                        from mutagen.id3._frames import TMCL as _TMCL, TIPL as _TIPL
                        audio.delall(choice)
                        cls = _TMCL if choice.startswith('TMCL') else _TIPL
                        audio.add(cls(encoding=3, people=edited))
                        _save(audio)
                        print(f"{choice} updated.")
                        time.sleep(1)
                    else:
                        print(f"{choice} not updated.")
                        time.sleep(1)
                    break

                # ── 6. Fallback Plain Text Fields ──
                else:
                    if choice.startswith('USLT'):
                        new_content = _edit_text_in_editor(full_text)
                    else:
                        tag_label = _TAG_MAP.get(choice.split(':')[0].split('[')[0], choice)
                        new_content = _edit_inline(tag_label, full_text)

                    if new_content is not None:
                        audio.delall(choice)
                        if choice.startswith('USLT'):
                            new_frame = USLT(encoding=3, lang='eng', desc='', text=new_content)
                        elif choice.startswith('COMM'):
                            new_frame = COMM(encoding=3, lang='eng', desc='', text=[new_content])
                        else:
                            frame_cls = _Frames.get(base_id)
                            if frame_cls is None:
                                print(f"Unknown frame ID: {base_id}")
                                time.sleep(1)
                                continue
                            new_frame = frame_cls(encoding=3, text=[new_content])
                        audio.add(new_frame)
                        _save(audio)
                        print(f"{choice} updated.")
                        time.sleep(1)
                    break

            elif action == "Delete tag":
                if prompt.confirm(f"Delete {choice}?"):
                    try:
                        audio.pop(choice)
                        _save(audio)
                        print(f"Deleted {choice}.")
                    except KeyError:
                        print(f"Could not delete {choice}: tag not found.")
                    time.sleep(1)
                    break

            elif not action or action == "Back":
                break