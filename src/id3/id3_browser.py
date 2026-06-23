from __future__ import annotations
import os
import re
import sys
import tempfile
import pyperclip
import subprocess
import numpy as np
import cv2

from src.utils import prompt
from src.lyrics.lyric_timer import save_sylt_entries
from mutagen.id3 import ID3
import mutagen.id3
from mutagen.id3._frames import APIC, USLT

from src.utils import ui_utils
from src.utils.ui_utils import Colors as C, get_terminal_height, get_terminal_width
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
    _EXT_TO_MIME,
    parse_composite_tag_id,
)


def _get_image_from_apic(apic_frame: APIC) -> tuple:
    try:
        img_data = getattr(apic_frame, 'data', b"")
        mime_type = getattr(apic_frame, 'mime', "image/jpeg")
        if not img_data:
            return None, mime_type, b""
        nparr = np.frombuffer(img_data, np.uint8)
        image = cv2.imdecode(nparr, cv2.IMREAD_COLOR)
        return image, mime_type, img_data
    except (ValueError, cv2.error):
        return None, "unknown", b""


def _convert_apic_to_viu(apic_frame: APIC, width: int = 80) -> str:
    img_bytes = getattr(apic_frame, 'data', None)
    if not img_bytes:
        return "Error: No image data."
    return render_with_viu(img_bytes, width=width, is_bytes=True)


def _open_apic_preview(apic_frame: APIC) -> bool:
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
    except (OSError, subprocess.CalledProcessError) as e:
        print(f"Error opening preview: {e}")
        return False


_LRC_TIMESTAMP_RE = re.compile(r'\[(\d+):(\d+)(?:[.:](\d{1,3}))?\]')
_LRC_META_RE = re.compile(r'^\s*\[(ti|ar|al|by|offset|re|ve)\s*:.+\]\s*$', re.I)

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


def _import_from_lrc(file_path: str, audio: ID3, tag_id: str) -> None:
    default_lrc = os.path.splitext(file_path)[0] + ".lrc"
    lrc_path = prompt.text("LRC file path:", default=default_lrc)
    if not lrc_path or not os.path.exists(lrc_path):
        ui_utils.show_status("File not found." if lrc_path else "Cancelled.")
        return

    entries = _parse_lrc_file(lrc_path)
    if not entries:
        ui_utils.show_status("No usable lines in LRC file.")
        return

    timed = [(text, ts) for text, ts in entries if ts is not None]

    if timed:
        sylt_data = [(text, int(ts)) for text, ts in timed if text]
        if not sylt_data:
            if tag_id.startswith('SYLT'):
                ui_utils.show_status("LRC has no timestamps for SYLT import.")
                return
        else:
            save_sylt_entries(file_path, sylt_data)
            ui_utils.show_status(f"Imported {len(sylt_data)} lines to SYLT.")
            return

    if tag_id.startswith('SYLT'):
        ui_utils.show_status("LRC has no timestamps; cannot import to SYLT.")
        return

    uslt_text = "\n".join(text for text, _ in entries if text)
    if uslt_text.strip():
        audio.delall('USLT')
        audio.add(USLT(encoding=3, lang='eng', desc='', text=uslt_text))
        audio.save(v2_version=3)
        ui_utils.show_status("Imported to USLT.")


def _edit_apic_tag(audio_obj: ID3, tag_name: str, apic_frame: APIC) -> bool:
    view_mode = "viu"
    cols = get_terminal_width()

    def _apic_header() -> list[str]:
        art_width = min(round(get_terminal_height()*1.5),get_terminal_width())
        art = _convert_apic_to_viu(apic_frame, width=art_width)
        image, mime, img_data = _get_image_from_apic(apic_frame)
        h = w = 0
        if image is not None:
            h, w = image.shape[:2]
        kb = len(img_data) / 1024
        info = f"{w}×{h}px  {mime}  {kb:.0f} KB"

        lines = [
            f"  {C.BOLD}{tag_name}{C.RESET}",
            f"  {C.DIM}{info}{C.RESET}",
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
        actions = []
        if view_mode != "viu":
            actions.append("View Art")
        if view_mode != "info":
            actions.append("View Info")
        actions.extend(["Open Preview", "Replace", "Edit Description"])

        action = prompt.select("Action:", choices=actions, header=_apic_header)

        if action == "View Art":
            view_mode = "viu"
        elif action == "View Info":
            view_mode = "info"
        elif action == "Open Preview":
            if _open_apic_preview(apic_frame):
                ui_utils.show_status("Opening...")
            else:
                ui_utils.show_status("Could not open preview.")
        elif action == "Replace":
            img_path = prompt.path("Path to new image:")
            if img_path and os.path.isfile(img_path):
                try:
                    with open(img_path, 'rb') as f:
                        new_data = f.read()
                    ext = os.path.splitext(img_path)[1].lower()
                    mime = _EXT_TO_MIME.get(ext, 'image/jpeg')
                    new_frame = create_apic_frame(
                        new_data, mime, 3,
                        getattr(apic_frame, 'desc', '')
                    )
                    if new_frame is not None:
                        audio_obj.delall(tag_name)
                        audio_obj.add(new_frame)
                        apic_frame = new_frame
                    ui_utils.show_status("Image replaced.")
                except (OSError, IOError) as e:
                    ui_utils.show_status(f"Error: {e}")
            elif img_path:
                ui_utils.show_status("File not found.")
        elif action == "Edit Description":
            new_desc = prompt.text(
                f"Description:",
                default=getattr(apic_frame, 'desc', '')
            )
            if new_desc is not None:
                apic_frame.desc = new_desc
                ui_utils.show_status("Updated.")
        elif not action:
            audio_obj.save(v2_version=3)
            return True


def inspect_tag_loop(
    file_path: str,
    library_metadata: dict | None = None,
    library: list | None = None
) -> None:
    show_xml = False

    # Duration is constant for the file — compute it ONCE here, not per render.
    # (The header is redrawn on every keypress/resize; reading the file each
    # time made resizing feel sluggish.)
    _cached_dur = 0.0
    try:
        _cached_dur = float((library_metadata or {}).get("duration") or 0.0)
    except (TypeError, ValueError):
        _cached_dur = 0.0
    if not _cached_dur:
        try:
            import mutagen
            _mf = mutagen.File(file_path)
            if _mf is not None and getattr(_mf, "info", None) is not None:
                _cached_dur = float(getattr(_mf.info, "length", 0.0) or 0.0)
        except Exception:
            _cached_dur = 0.0

    def _save(audio_obj):
        audio_obj.save(v2_version=3)
        if library is not None:
            try:
                fresh = refresh_library_entry(library, file_path)
                if library_metadata is not None:
                    library_metadata.update(fresh)
            except (OSError, KeyError) as e:
                ui_utils.show_status(f"Warning: cache update failed: {e}")

    def _main_header() -> list[str]:
        cols = ui_utils.get_terminal_width()
        ext = os.path.splitext(file_path)[1].upper().lstrip('.')

        try:
            size_str = f"  {os.path.getsize(file_path) / (1024*1024):.1f} MB"
        except OSError:
            size_str = ""

        dur_str = f"  {ui_utils.format_time(int(_cached_dur))}" if _cached_dur else ""

        # Prefer real tags over the (possibly cryptic) filename. Read live from
        # the loaded ID3 object, falling back to the cached library metadata,
        # and only to the filename when there is no title at all.
        meta = library_metadata or {}
        _PLACEHOLDERS = {"", "Unknown Artist", "Unknown Album", "Unknown Genre", "Unknown Year"}

        def _from_tags(frame: str, meta_key: str) -> str:
            try:
                fr = audio.get(frame)
                if fr is not None and getattr(fr, "text", None):
                    val = str(fr.text[0]).strip()
                    if val:
                        return val
            except Exception:
                pass
            val = str(meta.get(meta_key, "")).strip()
            return "" if val in _PLACEHOLDERS else val

        title = _from_tags("TIT2", "title")
        artist = _from_tags("TPE1", "artist") or _from_tags("TPE2", "album_artist")
        if not title:
            title = os.path.splitext(os.path.basename(file_path))[0]

        # Full-width rounded box: bold title (· dim artist) left, dim meta right.
        inner = max(12, cols - 4)
        right = f"[{ext}]{dur_str}{size_str}"
        avail = max(4, inner - len(right) - 2)

        if len(title) > avail:
            title = title[:avail - 1] + "…"
        left_styled = f"{C.BOLD}{title}{C.RESET}"
        left_vis = len(title)
        rem = avail - left_vis
        if artist and rem > 5:
            suffix = f" · {artist}"
            if len(suffix) > rem:
                suffix = suffix[:rem - 1] + "…"
            left_styled += f"{C.DIM}{suffix}{C.RESET}"
            left_vis += len(suffix)

        gap = max(1, inner - left_vis - len(right))
        title_line = f"{left_styled}{' ' * gap}{C.DIM}{right}{C.RESET}"

        lines = [
            f"{C.DIM}╭{'─' * (inner + 2)}╮{C.RESET}",
            f"{C.DIM}│{C.RESET} {title_line} {C.DIM}│{C.RESET}",
            f"{C.DIM}╰{'─' * (inner + 2)}╯{C.RESET}",
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
        except (mutagen.id3.ID3NoHeaderError, OSError) as e:  # type: ignore[reportPrivateImportUsage]
            print(f"Error loading tags: {e}")
            input("Press Enter to continue...")
            break

        cols = ui_utils.get_terminal_width()

        def _tag_title(tag_id: str) -> str:
            # Plain text; select(columns=True) lays out and greys the columns:
            #   TAG (friendly name)   type   | value
            info = get_tag_info(tag_id)
            alias = info.name[0] if info else ""
            if len(alias) > 22:
                alias = alias[:21] + "…"
            paren = f" ({alias})" if alias else ""
            category = get_tag_category(tag_id)
            val = summarize_tag_value(tag_id, audio[tag_id])
            return f"{tag_id}{paren} [{category}] | {val}"

        # A clearly-labelled, read-only filesystem path row — distinct from the
        # ID3 tags below it so it is never mistaken for a stored tag value.
        filepath_row = prompt.Choice(
            title=f"⌁ File path  {C.DIM}(filesystem — not an ID3 tag){C.RESET}",
            value="__filepath__",
        )
        tag_choices = [filepath_row, prompt.separator()] + [
            prompt.Choice(title=_tag_title(t), value=t) for t in tags
        ]
        xml_data = library_metadata.get('xml_data') if library_metadata else None
        has_id3 = file_path.lower().endswith('.mp3')

        extras = (["Toggle XML"] if xml_data and has_id3 else [])

        _shortcuts = {'a': 'Add Tag'} if has_id3 else None
        _extra_hints = {'a': 'add tag'} if has_id3 else None

        choice = prompt.select(
            "Select tag to manage:",
            choices=tag_choices + extras,
            header=_main_header,
            shortcuts=_shortcuts,
            extra_hints=_extra_hints,
            columns=True,
        )

        if choice == "Toggle XML":
            show_xml = not show_xml
            continue

        if choice == "__filepath__":
            _fp_header = [
                f"  {C.BOLD}File path{C.RESET}  {C.DIM}(filesystem location — not stored in ID3){C.RESET}",
                f"{C.DIM}{'─' * cols}{C.RESET}",
                f"  {file_path}",
                f"{C.DIM}{'─' * cols}{C.RESET}",
            ]
            fp_action = prompt.select(
                "Action:",
                choices=["Copy path to clipboard"],
                header=_fp_header,
            )
            if fp_action == "Copy path to clipboard":
                pyperclip.copy(file_path)
                ui_utils.show_status("File path copied.")
            continue

        if choice == "Add Tag":
            tag_id = prompt.text("Tag ID (e.g. TPE2, TXXX:Transcription:eng, COMM::fre):")
            if not tag_id:
                continue

            # Check if it's a known category base via our new parser
            base_id, _, _ = parse_composite_tag_id(tag_id)
            info = get_tag_info(base_id)

            if info:
                value = prompt_for_value(base_id)
            else:
                value = prompt.text(f"Value for {tag_id}:")

            if value is not None:
                new_frame = create_frame(tag_id, value)
                if new_frame:
                    audio.add(new_frame)
                    _save(audio)
                    ui_utils.show_status(f"Added {tag_id}.")
                else:
                    ui_utils.show_status(f"Could not create frame for {tag_id}.")
            continue

        if not choice:
            break

        # Edit tag
        while True:
            audio = ID3(file_path)
            if choice not in audio:
                break

            raw_val = audio[choice]
            category = get_tag_category(choice)

            def _tag_header() -> list[str]:
                cols = ui_utils.get_terminal_width()
                info = get_tag_info(choice) if choice else None
                label = info.name[0] if info else choice or "Unknown"

                lines = [
                    f"  {C.BOLD}{choice}{C.RESET}  {C.DIM}({label}){C.RESET}",
                    f"{C.DIM}{'─' * cols}{C.RESET}",
                ]

                if category == 'image':
                    art_width = min(round(get_terminal_height()*1.5),get_terminal_width())
                    art = _convert_apic_to_viu(raw_val, width=art_width)
                    lines.extend(art.splitlines())
                    lines.append(f"{C.DIM}{'─' * cols}{C.RESET}")

                if category == 'people':
                    people = getattr(raw_val, 'people', [])
                    cw = max(12, (cols - 6) // 2)
                    lines.append(f"  {C.DIM}{'ROLE':<{cw}}  NAME{C.RESET}")
                    lines.append(f"  {'─' * cw}  {'─' * (cols - cw - 4)}")
                    for role, name in people[:8]:
                        r = ui_utils.truncate_text(role, cw)
                        n = ui_utils.truncate_text(name, cols - cw - 4)
                        lines.append(f"  {r:<{cw}}  {n}")
                    if len(people) > 8:
                        lines.append(f"  {C.DIM}… +{len(people) - 8} more{C.RESET}")
                    lines.append(f"{C.DIM}{'─' * cols}{C.RESET}")

                return lines

            # Action selection
            actions = ["Copy", "Paste", "Edit", "Rename", "Delete"]
            if category in ('lyrics',) and choice.startswith(('USLT', 'SYLT')):
                actions.insert(0, "Import LRC")
            if category == 'image':
                actions.remove("Edit")
                actions.insert(0, "Manage")
            if choice.startswith('SYLT'):
                actions.remove("Edit")

            action = prompt.select("Action:", choices=actions, header=_tag_header)

            if action == "Manage" and category == 'image':
                if _edit_apic_tag(audio, choice, raw_val):
                    _save(audio)
                break

            elif action == "Import LRC":
                _import_from_lrc(file_path, audio, choice)
                break

            elif action == "Copy":
                if category == 'people':
                    text = "\n".join(f"{r}: {n}" for r, n in getattr(raw_val, 'people', []))
                else:
                    text = summarize_tag_value(choice, raw_val)
                pyperclip.copy(text)
                ui_utils.show_status("Copied to clipboard.")

            elif action == "Paste":
                clipboard = pyperclip.paste()
                if clipboard and prompt.confirm(f"Replace {choice}?"):
                    audio.delall(choice)
                    new_frame = create_frame(choice, clipboard)
                    if new_frame:
                        audio.add(new_frame)
                        _save(audio)
                        ui_utils.show_status("Updated.")
                    else:
                        ui_utils.show_status("Could not create frame - wrong data type for this tag.")

            elif action == "Rename":
                new_id = prompt.text("New tag ID:")
                if new_id and new_id != choice:
                    old_frame = audio.pop(choice)
                    if rename_frame(audio, old_frame, new_id):
                        _save(audio)
                        ui_utils.show_status(f"Renamed to {new_id}.")
                    else:
                        audio.add(old_frame)
                        ui_utils.show_status("Rename failed.")
                    break

            elif action == "Edit":
                current_frame = audio.get(choice)
                new_value = prompt_for_value(choice, current_value=current_frame)
                if new_value is not None:
                    new_frame = create_frame(choice, new_value)
                    if new_frame:
                        audio.add(new_frame)
                        _save(audio)
                        ui_utils.show_status("Updated.")
                    else:
                        ui_utils.show_status("Could not create frame - check data format.")
                    break

            elif action == "Delete":
                if prompt.confirm(f"Delete {choice}?"):
                    try:
                        audio.pop(choice)
                        _save(audio)
                        ui_utils.show_status(f"Deleted {choice}.")
                    except KeyError:
                        ui_utils.show_status(f"Could not delete {choice}.")
                    break

            elif not action:
                break
