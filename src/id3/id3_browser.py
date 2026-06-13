"""
ID3 metadata browser - unified tag editing using id3_tag_handler.

Single source of truth for all tag operations, widget selection, and frame creation.
"""
from __future__ import annotations
import os
import sys
import time
import tempfile
import pyperclip
import subprocess
import numpy as np
import cv2
import re

from src.utils import prompt
from src.playback.lyrics.lyric_timer import save_sylt_entries
from mutagen.id3 import ID3
from mutagen.id3._frames import APIC, USLT

from src.utils import ui_utils
from src.utils.ui_utils import Colors as C, get_terminal_height, get_terminal_width, visual_len
from src.art.album_art import render_with_viu
from src.music_library import refresh_library_entry

from src.id3.id3_tag_handler import (
    get_tag_info,
    get_tag_category,
    summarize_tag_value,
    prompt_for_value,
    create_frame,
    rename_frame,
    create_apic_frame,
    TAG_REGISTRY,
)


# ============================================================================
# APIC/IMAGE UTILITIES
# ============================================================================

def _get_image_from_apic(apic_frame: APIC) -> tuple:
    """Decode APIC frame to numpy array and metadata."""
    try:
        img_data = getattr(apic_frame, 'data', b"")
        mime_type = getattr(apic_frame, 'mime', "image/jpeg")
        if not img_data:
            return None, mime_type, b""
        nparr = np.frombuffer(img_data, np.uint8)
        image = cv2.imdecode(nparr, cv2.IMREAD_COLOR)
        return image, mime_type, img_data
    except Exception:
        return None, "unknown", b""


def _convert_apic_to_viu(apic_frame: APIC, width: int = 80) -> str:
    """Convert APIC to terminal art via viu."""
    img_bytes = getattr(apic_frame, 'data', None)
    if not img_bytes:
        return "Error: No image data."
    return render_with_viu(img_bytes, width=width, is_bytes=True)


def _open_apic_preview(apic_frame: APIC) -> bool:
    """Open APIC image in system preview."""
    image, mime_type, img_bytes = _get_image_from_apic(apic_frame)
    
    if not img_bytes or (hasattr(img_bytes, 'size') and img_bytes.size == 0) or not len(img_bytes):
        return False
    
    try:
        ext = {
            'image/jpeg': '.jpg', 'image/jpg': '.jpg',
            'image/png': '.png', 'image/gif': '.gif',
        }.get(mime_type, '.jpg')
        
        with tempfile.NamedTemporaryFile(suffix=ext, delete=False) as tmp:
            data_to_write = img_bytes.tobytes() if hasattr(img_bytes, 'tobytes') else img_bytes
            tmp.write(data_to_write)
            tmp_path = tmp.name
        
        if sys.platform == 'darwin':
            subprocess.run(['open', tmp_path], check=True)
        elif sys.platform == 'win32':
            os.startfile(tmp_path)
        elif sys.platform.startswith('linux'):
            subprocess.run(['xdg-open', tmp_path], check=True)
        else:
            raise OSError(f"Unsupported OS: {sys.platform}")
        
        return True
    except Exception as e:
        print(f"Error opening preview: {e}")
        return False


# ============================================================================
# LRC IMPORT
# ============================================================================

_LRC_TIMESTAMP_RE = re.compile(r'\[(\d+):(\d+)(?:[.:](\d{1,3}))?\]')
_LRC_META_RE = re.compile(r'^\s*\[(ti|ar|al|by|offset|re|ve)\s*:.+\]\s*$', re.I)

def _parse_lrc_file(lrc_path: str) -> list[tuple[str, int | None]]:
    """Parse .lrc file into (text, timestamp_ms) tuples."""
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


def _import_from_lrc(file_path: str, audio: ID3, tag_id: str) -> None:
    """Import lyrics from .lrc file into USLT or SYLT."""
    default_lrc = os.path.splitext(file_path)[0] + ".lrc"
    lrc_path = prompt.text("LRC file path:", default=default_lrc)
    if not lrc_path or not os.path.exists(lrc_path):
        print("File not found." if lrc_path else "Cancelled.")
        time.sleep(1.5)
        return

    entries = _parse_lrc_file(lrc_path)
    if not entries:
        print("No usable lines in LRC file.")
        time.sleep(1.5)
        return

    timed = [(text, ts) for text, ts in entries if ts is not None]
    
    if timed:
        sylt_data = [(text, int(ts)) for text, ts in timed if text]
        if not sylt_data:
            if tag_id.startswith('SYLT'):
                print("LRC has no timestamps for SYLT import.")
                time.sleep(1.5)
                return
        else:
            save_sylt_entries(file_path, sylt_data)
            print(f"Imported {len(sylt_data)} lines to SYLT.")
            time.sleep(1.5)
            return
    
    if tag_id.startswith('SYLT'):
        print("LRC has no timestamps; cannot import to SYLT.")
        time.sleep(1.5)
        return
    
    uslt_text = "\n".join(text for text, _ in entries if text)
    if uslt_text.strip():
        audio.delall('USLT')
        audio.add(USLT(encoding=3, lang='eng', desc='', text=uslt_text))
        audio.save(v2_version=3)
        print(f"Imported to USLT.")
        time.sleep(1.5)


# ============================================================================
# APIC EDITING
# ============================================================================

def _edit_apic_tag(audio_obj: ID3, tag_name: str, apic_frame: APIC) -> bool:
    """Edit APIC (album art) tag."""
    view_mode = "viu"
    cols = get_terminal_width()
    
    def _apic_header() -> list[str]:
        art_width = min(round(get_terminal_height()*1.5), get_terminal_width())
        art = _convert_apic_to_viu(apic_frame, width=art_width)
        image, mime, img_data = _get_image_from_apic(apic_frame)
        h = w = 0
        if image is not None:
            h, w = image.shape[:2]
        kb = len(img_data) / 1024
        info = f"{w}×{h}px  {mime}  {kb:.0f} KB"
        
        lines = [
            f"{C.BOLD}{tag_name}{C.RESET}",
            f"{C.DIM}{info}{C.RESET}",
            f"{C.DIM}{'─' * cols}{C.RESET}"
        ]
        
        if view_mode == "viu":
            lines.extend(art.splitlines())
        elif view_mode == "info":
            if image is not None:
                h, w = image.shape[:2]
                channels = image.shape[2] if len(image.shape) == 3 else 1
                color_mode = {1: "Grayscale", 3: "RGB", 4: "RGBA"}.get(channels, f"{channels}ch")
                size_kb = len(getattr(apic_frame, 'data', b"")) / 1024
                lines += [
                    f"  Description : {getattr(apic_frame, 'desc', '') or '(none)'}",
                    f"  Dimensions  : {w} × {h} px",
                    f"  Color mode  : {color_mode}",
                    f"  File size   : {size_kb:.1f} KB",
                ]
                lines.append(f"{C.DIM}{'─' * cols}{C.RESET}")
        
        return lines

    while True:
        try:
            audio = ID3(audio_obj.filename)
        except Exception:
            audio = audio_obj
        
        if tag_name not in audio:
            return False
        
        apic_frame = audio[tag_name]
        
        actions = ["Toggle View", "Replace", "Delete", "Back"]
        action = prompt.select("Action:", choices=actions, header=_apic_header)
        
        if action == "Toggle View":
            view_mode = "info" if view_mode == "viu" else "viu"
            continue
        elif action == "Replace":
            from bulk_id3_manager import prompt_for_image_payload
            payload = prompt_for_image_payload()
            if payload:
                img_data, mime, pic_type, desc = payload
                new_frame = create_apic_frame(img_data, mime, pic_type, desc)
                if new_frame:
                    audio.delall(tag_name)
                    audio.add(new_frame)
                    audio.save(v2_version=3)
                    return True
        elif action == "Delete":
            if prompt.confirm(f"Delete {tag_name}?"):
                audio.delall(tag_name)
                audio.save(v2_version=3)
                return True
        elif not action or action == "Back":
            break
    
    return False


# ============================================================================
# MAIN BROWSER
# ============================================================================

def id3_browser(file_path: str, library: list = []) -> None:
    """
    Browse and edit ID3 tags for a single file using the responsive 
    pseudo-table layout.
    """
    library = library or []
    library_metadata = next((s for s in library if s.get('path') == file_path), {})
    show_xml = False
    
    def _save(audio_obj):
        try:
            audio_obj.save(v2_version=3)
            try: refresh_library_entry(library, file_path)
            except Exception: pass
        except Exception as e:
            print(f"Save error: {e}")
            time.sleep(1.5)

    def _main_header() -> list[str]:
        try:
            audio = ID3(file_path)
            n_tags = len(audio)
        except Exception: n_tags = 0
        
        cols = ui_utils.get_terminal_width()
        inner = max(10, cols - 6)
        basename = os.path.basename(file_path)
        content_left = f"ID3 Browser  {basename}"
        content_right = f"  {n_tags} tags"
        
        lines = [
            f"{C.DIM}╭{'─' * inner}╮{C.RESET}",
            f"{C.DIM}│{C.RESET}{C.BOLD}{content_left}{C.RESET}{C.DIM}{content_right}{' ' * (inner - visual_len(content_left) - len(content_right))}│{C.RESET}",
            f"{C.DIM}╰{'─' * inner}╯{C.RESET}",
            "",
        ]
        
        xml_data = library_metadata.get('xml_data') if library_metadata else None
        has_id3 = file_path.lower().endswith('.mp3')
        if xml_data and (show_xml or not has_id3):
            lines.extend(ui_utils.get_xml_metadata_lines(xml_data))
        return lines

    while True:
        try:
            audio = ID3(file_path)
            tags = sorted(audio.keys())
        except Exception as e:
            print(f"Error loading tags: {e}")
            input("Press Enter to continue...")
            break

        # Map tags to Choice objects for the new select engine
        tag_choices = []
        for t in tags:
            info = get_tag_info(t)
            tag_choices.append(prompt.Choice(
                title=t,
                value=t,
                category=get_tag_category(t).lower() if info else "",
                sub_label=info.label if info and info.label else "",
                display_val=summarize_tag_value(t, audio[t]),
            ))

        xml_data = library_metadata.get('xml_data') if library_metadata else None
        has_id3 = file_path.lower().endswith('.mp3')
        extras = (["Add Tag"] if has_id3 else []) + (["Toggle XML"] if xml_data and has_id3 else []) + ["Back"]
        
        # Merge tag choices with functional extras
        all_choices = tag_choices + [prompt.Choice(e) for e in extras]

        choice = prompt.select(
            "Select tag to manage:",
            choices=all_choices,
            header=_main_header
        )

        if choice == "Toggle XML":
            show_xml = not show_xml
            continue
        elif choice == "Add Tag":
            tag_id = prompt.text("Tag ID (e.g. TPE2, TXXX:Transcription:eng, COMM::fre):")
            if tag_id:
                from src.id3.id3_tag_handler import parse_composite_tag_id
                base_id, _, _ = parse_composite_tag_id(tag_id)
                info = get_tag_info(base_id)
                value = prompt_for_value(base_id) if info else prompt.text(f"Value for {tag_id}:")
                
                if value is not None:
                    new_frame = create_frame(tag_id, value)
                    if new_frame:
                        audio.add(new_frame)
                        _save(audio)
                        print(f"Added {tag_id}.")
                        time.sleep(1)
            continue
        elif not choice or choice == "Back":
            break

        # Edit tag logic (preserving your existing flow)
        while True:
            audio = ID3(file_path)
            if choice not in audio: break
            
            raw_val = audio[choice]
            category = get_tag_category(choice)

            def _tag_header() -> list[str]:
                cols = ui_utils.get_terminal_width()
                if tag_id:
                    info = get_tag_info(tag_id)
                else:
                    info = None
                label = info.label if info else choice
                lines = [f"{C.BOLD}{choice}{C.RESET}  {C.DIM}({label}){C.RESET}", f"{C.DIM}{'─' * cols}{C.RESET}"]
                
                if category == 'image':
                    art = _convert_apic_to_viu(raw_val, width=min(round(get_terminal_height()*1.5), cols))
                    lines.extend(art.splitlines() + [f"{C.DIM}{'─' * cols}{C.RESET}"])
                elif category == 'people':
                    people = getattr(raw_val, 'people', [])
                    cw = max(12, (cols - 6) // 2)
                    lines.extend([f"  {C.DIM}{'ROLE':<{cw}}  NAME{C.RESET}", f"  {'─' * cw}  {'─' * (cols - cw - 4)}"])
                    for role, name in people[:8]:
                        lines.append(f"  {ui_utils.truncate_text(role, cw):<{cw}}  {ui_utils.truncate_text(name, cols - cw - 4)}")
                    if len(people) > 8: lines.append(f"  {C.DIM}… +{len(people) - 8} more{C.RESET}")
                    lines.append(f"{C.DIM}{'─' * cols}{C.RESET}")
                return lines

            actions = ["Copy", "Paste", "Edit", "Rename", "Delete"]
            if category in ('lyrics',) and choice.startswith(('USLT', 'SYLT')): actions.insert(0, "Import LRC")
            if category == 'image': 
                actions.remove("Edit")
                actions.insert(0, "Manage")
            if choice.startswith('SYLT'): actions.remove("Edit")
            actions.append("Back")

            action = prompt.select("Action:", choices=actions, header=_tag_header)

            if action == "Manage" and category == 'image':
                if _edit_apic_tag(audio, choice, raw_val): _save(audio)
                break
            elif action == "Import LRC":
                _import_from_lrc(file_path, audio, choice)
                break
            elif action == "Copy":
                pyperclip.copy("\n".join(f"{r}: {n}" for r, n in getattr(raw_val, 'people', [])) if category == 'people' else raw_val)
                print("Copied.")
                time.sleep(1)
            elif action == "Paste":
                clipboard = pyperclip.paste()
                if clipboard and prompt.confirm(f"Replace {choice}?"):
                    audio.delall(choice)
                    if (f := create_frame(choice, clipboard)):
                        audio.add(f); _save(audio); print("Updated.")
                    else: print("Format error."); time.sleep(1.5)
            elif action == "Rename":
                if (new_id := prompt.text("New tag ID:")) and new_id != choice:
                    old = audio.pop(choice)
                    if rename_frame(audio, old, new_id): _save(audio); print(f"Renamed to {new_id}.")
                    else: audio.add(old); print("Rename failed.")
                    time.sleep(1); break
            elif action == "Edit":
                if (new_v := prompt_for_value(choice, current_value=audio.get(choice))) is not None:
                    if (f := create_frame(choice, new_v)): audio.add(f); _save(audio); print("Updated.")
                    else: print("Format error."); time.sleep(1.5)
                    time.sleep(1); break
            elif action == "Delete":
                if prompt.confirm(f"Delete {choice}?"):
                    try: audio.pop(choice); _save(audio); print("Deleted.")
                    except KeyError: print("Delete failed.")
                    time.sleep(1); break
            elif not action or action == "Back": break
                
if __name__ == '__main__':
    import sys
    if len(sys.argv) > 1:
        id3_browser(sys.argv[1])
    else:
        print("Usage: python id3_browser.py <file_path>")