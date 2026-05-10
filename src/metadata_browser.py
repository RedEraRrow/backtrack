"""
Metadata browser for inspecting and managing track metadata.
"""

import os
import time
import string
import pyperclip
import tempfile
import subprocess
import numpy as np
import cv2
from collections import Counter

from src import prompt
from mutagen.id3 import ID3, USLT, COMM, SYLT, TextFrame, APIC

from src import ui_utils
from src.ascii_art import convert_image_to_ascii


def _sort_category(name: str) -> str:
    """Return first-letter bucket for an artist name."""
    first = (name.lstrip("The ")[0].upper()) if name else "#"
    return first if first in string.ascii_uppercase else "#"


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
            new_frame = COMM(encoding=3, lang=lang, desc='', text=old_frame.text)
        elif base_id.startswith('USLT'):
            new_frame = USLT(encoding=3, lang=lang, desc='', text=old_frame.text)
        else:
            from mutagen.id3 import Frames
            frame_cls = Frames.get(base_id, TextFrame)
            new_frame = frame_cls(encoding=3, text=old_frame.text)
        
        audio_obj.add(new_frame)
        return True
    except Exception as e:
        print(f"Rename failed: {e}")
        return False


def _get_image_from_apic(apic_frame: APIC) -> tuple:
    """Extract image data and mime type from APIC frame. Returns (image_array, mime_type)."""
    try:
        img_data = apic_frame.data
        nparr = np.frombuffer(img_data, np.uint8)
        image = cv2.imdecode(nparr, cv2.IMREAD_COLOR)
        return image, apic_frame.mime
    except Exception as e:
        print(f"Error extracting image: {e}")
        return None, None


def _convert_ascii_from_apic(apic_frame: APIC, width: int = 80) -> str:
    """Convert APIC tag data to ASCII art string."""
    image, _ = _get_image_from_apic(apic_frame)
    if image is None:
        return "Error: Could not decode image data."
    return convert_image_to_ascii(image, width)


def _get_ascii_width() -> int:
    """Choose an ASCII art width based on current terminal size."""
    cols = ui_utils.get_terminal_width()
    return max(20, min(cols - 8, 100))


def _open_apic_preview(apic_frame: APIC) -> bool:
    """Open APIC image in system preview. Returns True if successful."""
    image, mime_type = _get_image_from_apic(apic_frame)
    if image is None:
        return False
    
    try:
        # Determine file extension from mime type
        ext_map = {
            'image/jpeg': '.jpg',
            'image/jpg': '.jpg',
            'image/png': '.png',
            'image/gif': '.gif'
        }
        ext = ext_map.get(mime_type, '.jpg')
        
        # Create temp file and save image
        with tempfile.NamedTemporaryFile(suffix=ext, delete=False) as tmp:
            img_data = apic_frame.data
            tmp.write(img_data)
            tmp_path = tmp.name
        
        # Open with system preview
        subprocess.run(['open', tmp_path], check=True)
        return True
    except Exception as e:
        print(f"Error opening preview: {e}")
        return False


def _edit_apic_tag(audio_obj: ID3, tag_name: str, apic_frame: APIC) -> bool:
    """Edit APIC (image) tag with options to replace image, view modes, etc."""
    view_mode = "ascii"  # ascii, raw, or image
    last_size = ui_utils.get_terminal_size()
    
    while True:
        current_size = ui_utils.get_terminal_size()
        if current_size != last_size:
            last_size = current_size
            continue  # Redraw on resize
        
        ui_utils.clear_screen()
        cols = ui_utils.get_terminal_width()
        print(f"EDITING: {tag_name} (APIC - Album Art)")
        print()
        
        # Display based on current view mode
        if view_mode == "ascii":
            ascii_art = _convert_ascii_from_apic(apic_frame, width=_get_ascii_width())
            print(ascii_art)
        elif view_mode == "raw":
            # Full raw representation — big block like repr() of the frame
            print(repr(apic_frame))
        elif view_mode == "info":
            image, mime = _get_image_from_apic(apic_frame)
            if image is not None:
                h, w = image.shape[:2]
                channels = image.shape[2] if len(image.shape) == 3 else 1
                colour_mode = {1: "Greyscale", 3: "RGB", 4: "RGBA"}.get(channels, f"{channels}ch")
                size_kb = len(apic_frame.data) / 1024
                print(f"  MIME type   : {mime}")
                print(f"  Description : {apic_frame.desc or '(none)'}")
                print(f"  Dimensions  : {w} × {h} px")
                print(f"  Colour mode : {colour_mode}")
                print(f"  File size   : {size_kb:.1f} KB ({len(apic_frame.data):,} bytes)")
                print(f"  Encoding    : {apic_frame.encoding}")
            else:
                print("Could not read image information.")
        
        print()
        
        actions = []
        if view_mode != "ascii":
            actions.append("View as ASCII Art")
        if view_mode != "raw":
            actions.append("View as Raw Data")
        if view_mode != "info":
            actions.append("View as Info")
        
        actions.extend([
            "Open in Preview",
            "Replace Image",
            "Edit Description",
            "Back"
        ])
        
        action = prompt.select("Action:", choices=actions)
        
        if action == "View as ASCII Art":
            view_mode = "ascii"
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
            image_path = prompt.text("Path to new image file:")
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
                        desc=apic_frame.desc,
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
            new_desc = prompt.text(f"Description (current: '{apic_frame.desc}'):", default=apic_frame.desc)
            if new_desc is not None:
                apic_frame.desc = new_desc
                print("Description updated.")
                time.sleep(1)
        elif action == "Back":
            audio_obj.save(v2_version=3)
            return True
    
    return True


def _is_people_frame(tag_name: str) -> bool:
    """True for TMCL/TIPL which store [[role, name], ...] pairs."""
    return tag_name.startswith(('TMCL', 'TIPL'))


def _edit_list_data(tag_data, tag_name: str):
    """Edit list or 2D list data. TMCL/TIPL get a role/name pair editor."""

    # ── TMCL / TIPL: [[role, name], ...] people frames ───────────────────
    if _is_people_frame(tag_name):
        # tag_data here is frame.people which is a list of [role, name]
        rows = [list(r) for r in tag_data]

        last_size = ui_utils.get_terminal_size()
        while True:
            current_size = ui_utils.get_terminal_size()
            if current_size != last_size:
                last_size = current_size
                continue  # Redraw on resize
            
            ui_utils.clear_screen()
            cols = ui_utils.get_terminal_width()
            print(f"EDITING: {tag_name}  ({len(rows)} entries)")
            print("─" * cols)

            col_w = max(20, (cols - 8) // 2)
            header = f"  {'ROLE':<{col_w}}  NAME"
            print(header)
            print("  " + "─" * (cols - 4))
            for i, (role, name) in enumerate(rows):
                r_disp = role[:col_w - 2] if len(role) > col_w else role
                print(f"  {i:>2}. {r_disp:<{col_w}}  {name}")
            print()

            action = prompt.select(
                "Action:",
                choices=["Add entry", "Edit entry", "Remove entry", "Clear all", "Done"]
            )

            if action == "Done" or action is None:
                return rows

            elif action == "Add entry":
                role = prompt.text("Role (e.g. producer, writer, performer):")
                name = prompt.text("Name:")
                if role is not None and name is not None:
                    rows.append([role.strip(), name.strip()])

            elif action == "Edit entry" and rows:
                choices = [prompt.Choice(f"{i:>2}. {r:<25}  {n}", value=str(i))
                           for i, (r, n) in enumerate(rows)]
                idx = prompt.select("Edit which entry?", choices=choices)
                if idx is not None:
                    idx = int(idx)
                    role = prompt.text("Role:", default=rows[idx][0])
                    name = prompt.text("Name:", default=rows[idx][1])
                    if role is not None and name is not None:
                        rows[idx] = [role.strip(), name.strip()]

            elif action == "Remove entry" and rows:
                choices = [prompt.Choice(f"{i:>2}. {r:<25}  {n}", value=str(i))
                           for i, (r, n) in enumerate(rows)]
                idx = prompt.select("Remove which entry?", choices=choices)
                if idx is not None:
                    rows.pop(int(idx))

            elif action == "Clear all":
                if prompt.confirm("Clear all entries?"):
                    rows = []

    # ── Simple 1D list ────────────────────────────────────────────────────
    is_2d = (isinstance(tag_data, (list, tuple)) and len(tag_data) > 0
             and isinstance(tag_data[0], (list, tuple)))

    if not is_2d:
        ui_utils.clear_screen()
        print(f"EDITING: {tag_name} (List)")
        print()
        for i, item in enumerate(tag_data):
            print(f"  [{i}] {repr(item)}")
        print()

        action = prompt.select(
            "Action:", choices=["Add item", "Remove item", "Edit item", "Clear all", "Back"]
        )

        if action == "Add item":
            v = prompt.text("New item:")
            if v is not None:
                return list(tag_data) + [v]
        elif action == "Remove item" and tag_data:
            idx_str = prompt.select(
                "Remove which?",
                choices=[f"[{i}] {repr(x)}" for i, x in enumerate(tag_data)]
            )
            if idx_str:
                idx = int(idx_str.split(']')[0].strip('['))
                lst = list(tag_data); lst.pop(idx); return lst
        elif action == "Edit item" and tag_data:
            idx_str = prompt.select(
                "Edit which?",
                choices=[f"[{i}] {repr(x)}" for i, x in enumerate(tag_data)]
            )
            if idx_str:
                idx = int(idx_str.split(']')[0].strip('['))
                v = prompt.text("New value:", default=str(tag_data[idx]))
                if v is not None:
                    lst = list(tag_data); lst[idx] = v; return lst
        elif action == "Clear all":
            if prompt.confirm("Clear all?"):
                return []
        return tag_data

    # ── Generic 2D list ───────────────────────────────────────────────────
    ui_utils.clear_screen()
    print(f"EDITING: {tag_name} (2D List)")
    print()
    for i, row in enumerate(tag_data):
        print(f"  Row {i}: {row}")
    print()

    action = prompt.select(
        "Action:", choices=["Add row", "Remove row", "Edit row", "Edit cell", "Clear all", "Back"]
    )

    if action == "Add row":
        row_str = prompt.text("New row (comma-separated or ['a','b']):")
        if row_str:
            try:
                new_row = eval(row_str) if row_str.startswith('[') else [x.strip() for x in row_str.split(',')]
                return list(tag_data) + [new_row]
            except Exception as e:
                print(f"Parse error: {e}"); time.sleep(1.5)
    elif action == "Remove row" and tag_data:
        idx_str = prompt.select("Remove which?", choices=[f"Row {i}: {r}" for i, r in enumerate(tag_data)])
        if idx_str:
            idx = int(idx_str.split(':')[0].split()[-1])
            lst = list(tag_data); lst.pop(idx); return lst
    elif action == "Edit row" and tag_data:
        idx_str = prompt.select("Edit which?", choices=[f"Row {i}: {r}" for i, r in enumerate(tag_data)])
        if idx_str:
            idx = int(idx_str.split(':')[0].split()[-1])
            row_str = prompt.text("Edit row:", default=str(tag_data[idx]))
            if row_str:
                try:
                    new_row = eval(row_str) if row_str.startswith('[') else [x.strip() for x in row_str.split(',')]
                    lst = list(tag_data); lst[idx] = new_row; return lst
                except Exception as e:
                    print(f"Parse error: {e}"); time.sleep(1.5)
    elif action == "Edit cell" and tag_data:
        idx_str = prompt.select("Which row?", choices=[f"Row {i}: {r}" for i, r in enumerate(tag_data)])
        if idx_str:
            row_num = int(idx_str.split(':')[0].split()[-1])
            row = tag_data[row_num]
            cell_str = prompt.select("Which cell?", choices=[f"Cell {i}: {repr(c)}" for i, c in enumerate(row)])
            if cell_str:
                cell_num = int(cell_str.split(':')[0].split()[-1])
                v = prompt.text("New value:", default=str(row[cell_num]))
                if v is not None:
                    lst = [list(r) for r in tag_data]; lst[row_num][cell_num] = v; return lst
    elif action == "Clear all":
        if prompt.confirm("Clear all?"):
            return []

    return tag_data


def bulk_tag_manager(library: list, album_name: str = None, paths: list = None) -> None:
    """Bulk tag operations across a set of tracks.

    Pass either album_name (looks up from library) or paths directly.
    """
    if paths is not None:
        album_tracks = paths
    elif album_name is not None:
        album_tracks = [s['path'] for s in library if s['album'] == album_name]
    else:
        return
    
    if not album_tracks:
        print("No tracks found.")
        return

    operation = prompt.select(
        "Select bulk operation:",
        choices=["Delete Tags", "Rename Tags", "Set Common Value", "Cancel"]
    )

    if not operation or operation == "Cancel":
        return

    from src.music_library import TAG_MAP as _TAG_MAP
    import re as _re
    _cols = ui_utils.get_terminal_width()

    label = album_name or f'{len(album_tracks)} tracks'
    print(f"Scanning {len(album_tracks)} tracks in '{label}'...")
    all_tag_counts = Counter()
    tag_values: dict = {}  # tag -> list of values across tracks

    for path in album_tracks:
        try:
            audio = ID3(path)
            all_tag_counts.update(audio.keys())
            for k in audio.keys():
                raw = audio[k]
                if k.startswith(('APIC', 'SYLT')):
                    val = k  # sentinels handled in _value_summary
                elif k.startswith(('TMCL', 'TIPL')):
                    val = str(len(raw.people))
                elif hasattr(raw, 'text'):
                    val = " / ".join(str(t) for t in raw.text)
                else:
                    val = str(raw)
                tag_values.setdefault(k, []).append(val)
        except Exception:
            continue

    if not all_tag_counts:
        print("No tags found.")
        return

    ALIAS_MAX = 22
    TAG_MAX   = 28
    VAL_MAX   = 30  # max chars for value preview column

    def _balias(tag):
        return _TAG_MAP.get(tag.split(':')[0].split('[')[0], '')

    def _value_summary(tag) -> str:
        """
        Common value if all tracks agree, otherwise longest common prefix
        followed by {N values} as a placeholder for the diverging tail.
        Special tags (APIC, TMCL, TIPL, SYLT) get a short descriptor.
        """
        if tag.startswith('APIC'):
            return "‹image›"
        if tag.startswith(('TMCL', 'TIPL')):
            vals = tag_values.get(tag, [])
            unique_counts = set(vals)
            return f"‹{vals[0]} people›" if len(unique_counts) == 1 else "‹varies›"
        if tag.startswith('SYLT'):
            return "‹synced lyrics›"

        vals = tag_values.get(tag, [])
        if not vals:
            return ""
        unique = set(vals)
        if len(unique) == 1:
            # All the same
            v = vals[0]
            return v if len(v) <= VAL_MAX else v[:VAL_MAX - 1] + "…"
        # Find longest common prefix across all values
        prefix = vals[0]
        for v in vals[1:]:
            # shorten prefix until it matches
            while not v.startswith(prefix):
                prefix = prefix[:-1]
                if not prefix:
                    break
        n_vary = len(unique)
        if prefix:
            stub = prefix if len(prefix) <= VAL_MAX - 12 else prefix[:VAL_MAX - 13] + "…"
            return f"{stub}{{{n_vary} values}}"
        return f"{{{n_vary} values}}"

    bulk_tag_col   = min(TAG_MAX,   max((len(t) for t in all_tag_counts), default=6))
    bulk_alias_col = min(ALIAS_MAX, max((len(_balias(t)) + 3 for t in all_tag_counts), default=0))
    # Value col: fit in terminal, leave room for tag+alias+count columns
    _fixed = bulk_tag_col + bulk_alias_col + 18
    bulk_val_col = min(VAL_MAX, max(_cols - _fixed, 10))

    def _tag_option_title(tag, count):
        alias = _balias(tag)
        tag_disp = tag if len(tag) <= bulk_tag_col else tag[:bulk_tag_col - 1] + "…"
        if alias:
            a = f"({alias})"
            if len(a) > bulk_alias_col:
                a = a[:bulk_alias_col - 2] + "…)"
            alias_str = f" {a}"
        else:
            alias_str = ""
        val_disp = _value_summary(tag)
        val_disp = val_disp if len(val_disp) <= bulk_val_col else val_disp[:bulk_val_col - 1] + "…"
        count_str = f"{count}/{len(album_tracks)}"
        return (f"{tag_disp:<{bulk_tag_col}}"
                f"{alias_str:<{bulk_alias_col + 1}}  "
                f"{val_disp:<{bulk_val_col}}  "
                f"{count_str}")

    tag_options = [
        prompt.Choice(title=_tag_option_title(tag, count), value=tag)
        for tag, count in sorted(all_tag_counts.items())
    ]

    selected_tags = prompt.checkbox(
        f"Select tags to {operation.lower()}:",
        choices=tag_options
    )

    if not selected_tags:
        return

    target_val = None
    if operation == "Rename Tags":
        target_val = prompt.text("New tag ID (e.g. TPE2, COMM[eng]):")
    elif operation == "Set Common Value":
        # Pattern detection: if all values share a common format, offer template editing
        for tag in selected_tags:
            vals = tag_values.get(tag, [])
            if len(vals) > 1:
                # Detect common pattern by replacing digits with X
                patterns = [_re.sub(r'\d', 'X', v) for v in vals]
                if len(set(patterns)) == 1:
                    template = patterns[0]
                    print(f"\nPattern detected in {tag}: {template}")
                    print(f"Example value: {vals[0]}")
                    use_template = prompt.confirm(
                        f"Edit using pattern template? (backspace to truncate)"
                    )
                    if use_template:
                        edited = prompt.text(
                            f"Edit template (X=digit placeholder):",
                            default=vals[0]
                        )
                        if edited is not None:
                            target_val = edited
                            break
        if target_val is None:
            target_val = prompt.text("Enter common value for these tags:")

    if not target_val and operation != "Delete Tags":
        return

    # APIC in bulk: handle separately before the per-file loop
    apic_tags = [t for t in selected_tags if t.startswith('APIC')]
    non_apic_tags = [t for t in selected_tags if not t.startswith('APIC')]
    new_apic_frame = None  # shared replacement frame for all tracks

    if apic_tags and operation != "Delete Tags":
        # Offer the same flow as single-track: replace image or edit description
        apic_action = prompt.select(
            f"Bulk APIC action ({len(apic_tags)} tag(s)):",
            choices=["Replace Image (apply same image to all tracks)", "Edit Description", "Skip APIC"]
        )

        if apic_action == "Replace Image (apply same image to all tracks)":
            image_path = prompt.text("Path to new image file:")
            if image_path and os.path.isfile(image_path):
                ext = os.path.splitext(image_path)[1].lower()
                mime_map = {'.jpg': 'image/jpeg', '.jpeg': 'image/jpeg', '.png': 'image/png', '.gif': 'image/gif'}
                mime_type = mime_map.get(ext, 'image/jpeg')
                with open(image_path, 'rb') as f:
                    img_data = f.read()
                new_apic_frame = APIC(encoding=3, mime=mime_type, type=3, desc='', data=img_data)
            else:
                print("File not found, skipping APIC.")
                apic_tags = []
        elif apic_action == "Edit Description":
            new_desc = prompt.text("New description for all tracks:")
            # Store as sentinel — applied per-track below
            new_apic_frame = new_desc  # str signals desc-only edit
        else:
            apic_tags = []  # skip

    if not prompt.confirm("Apply changes to all selected tracks?"):
        return

    count_modified = 0
    for path in album_tracks:
        try:
            audio = ID3(path)
            changed = False

            # APIC
            for tag in apic_tags:
                if tag in audio:
                    if operation == "Delete Tags":
                        audio.pop(tag)
                        changed = True
                    elif new_apic_frame is not None:
                        if isinstance(new_apic_frame, str):
                            # Description-only edit
                            audio[tag].desc = new_apic_frame
                        else:
                            audio.delall(tag)
                            audio.add(new_apic_frame)
                        changed = True

            # All other tags
            for tag in non_apic_tags:
                if tag in audio:
                    if operation == "Delete Tags":
                        audio.pop(tag)
                        changed = True
                    elif operation == "Rename Tags":
                        old_frame = audio.pop(tag)
                        if perform_rename(audio, old_frame, target_val):
                            changed = True
                    elif operation == "Set Common Value":
                        audio[tag].text = [target_val]
                        changed = True

            if changed:
                audio.save(v2_version=3)
                count_modified += 1

        except Exception as e:
            print(f"Error on {os.path.basename(path)}: {e}")

    print(f"Successfully processed {count_modified} files.")
    time.sleep(1.5)


def inspect_tag_loop(file_path: str, library_metadata: dict | None = None) -> None:
    """Browse and edit ID3 tags for a single file."""
    from src.ui_utils import format_time
    
    from src.music_library import TAG_MAP as _TAG_MAP

    show_xml = False  # default: hide XML when ID3 available

    while True:
        ui_utils.clear_screen()
        cols = ui_utils.get_terminal_width()
        div = "═" * cols
        print(div)
        print(f"  INSPECTING: {os.path.basename(file_path)}")
        print(div)

        xml_data = library_metadata.get('xml_data') if library_metadata else None
        has_id3 = os.path.splitext(file_path)[1].lower() == '.mp3'

        if xml_data and (show_xml or not has_id3):
            ui_utils.display_xml_metadata({'xml_data': xml_data})

        try:
            audio = ID3(file_path)
            tags = sorted(audio.keys())

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
            extras = ([xml_toggle] if xml_data and has_id3 else []) + ["Back to track list"]

            choice = prompt.select(
                "Select a tag to manage:",
                choices=tag_choices + extras
            )

            if choice == xml_toggle:
                show_xml = not show_xml
                continue

            if not choice or choice == "Back to track list":
                break

            last_size = ui_utils.get_terminal_size()
            while True:
                current_size = ui_utils.get_terminal_size()
                if current_size != last_size:
                    last_size = current_size
                    continue  # Redraw on resize
                
                ui_utils.clear_screen()
                raw_val = audio[choice]
                
                if choice.startswith('SYLT'):
                    display_lines = [f"[{format_time(ts/1000)}] {txt}" for txt, ts in raw_val.text]
                    full_text = "\n".join(display_lines)
                elif choice.startswith('APIC'):
                    # For APIC, show ASCII art by default
                    full_text = _convert_ascii_from_apic(raw_val, width=_get_ascii_width())
                elif choice.startswith('TMCL') or choice.startswith('TIPL'):
                    full_text = str(raw_val)
                else:
                    full_text = "".join(raw_val.text) if hasattr(raw_val, 'text') else str(raw_val)
                
                _cols = ui_utils.get_terminal_width()
                _inner = max(10, _cols - 2)
                alias = _TAG_MAP.get(choice.split(':')[0].split('[')[0], '')
                alias_str = f" ({alias})" if alias else ""
                print(f"TAG: {choice}{alias_str}")
                print("┌" + "─" * _inner + "┐")
                if choice.startswith('APIC'):
                    print(full_text)
                elif choice.startswith('TMCL') or choice.startswith('TIPL'):
                    for role, name in raw_val.people:
                        line = f"  {role:<25}  {name}"
                        print(line[:_inner])
                else:
                    content = full_text if choice.startswith('SYLT') else repr(full_text)
                    # Wrap to inner width
                    for chunk in [content[i:i+_inner] for i in range(0, max(1,len(content)), _inner)]:
                        print(chunk)
                print("└" + "─" * _inner + "┘")

                actions = ["Copy to clipboard", "Paste from clipboard", "Edit content", "Rename tag", "Delete tag"]
                if choice.startswith('SYLT'):
                    actions.insert(0, "Export to LRC file")
                if choice.startswith('APIC'):
                    actions.insert(0, "Edit Image")
                actions.append("Back")

                action = prompt.select("Action:", choices=actions)

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
                    # Reload audio to get updated frames
                    audio = ID3(file_path)
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
                            lines = clean_data.split('\r')
                            sylt_data = [(line.strip(), 0) for line in lines if line.strip()]
                            new_frame = SYLT(encoding=3, lang='eng', format=2, type=1, text=sylt_data)
                        elif choice.startswith('COMM'):
                            new_frame = COMM(encoding=3, lang='eng', desc='', text=[clean_data])
                        else:
                            new_frame = TextFrame(encoding=3, text=[clean_data])
                            new_frame.FrameID = choice.split('[')[0] if '[' in choice else choice
                        
                        audio.add(new_frame)
                        audio.save(v2_version=3)
                        print(f"{choice} updated.")
                        time.sleep(1)
                        break 

                elif action == "Rename tag":
                    new_id = prompt.text("New tag ID (e.g. TPE2, COMM[eng]):")
                    if new_id and new_id != choice:
                        old_frame = audio.pop(choice)
                        if perform_rename(audio, old_frame, new_id):
                            audio.save(v2_version=3)
                            print(f"Renamed {choice} to {new_id}")
                            time.sleep(1)
                            break 

                elif action == "Edit content":
                    # Smart editing based on data type
                    if choice.startswith('APIC'):
                        # APIC is handled by "Edit Image" action
                        continue
                    elif choice.startswith('SYLT'):
                        # SYLT is already edited via paste
                        new_content = prompt.text("New content (time-stamped lyrics):", default=full_text)
                        if new_content is not None:
                            lines = new_content.split('\n')
                            sylt_data = [(line.strip(), 0) for line in lines if line.strip()]
                            audio.delall(choice)
                            new_frame = SYLT(encoding=3, lang='eng', format=2, type=1, text=sylt_data)
                            audio.add(new_frame)
                            audio.save(v2_version=3)
                            print(f"{choice} updated.")
                            time.sleep(1)
                            break
                    else:
                        # TMCL/TIPL: people frames
                        if choice.startswith('TMCL') or choice.startswith('TIPL'):
                            edited = _edit_list_data(raw_val.people, choice)
                            if edited != raw_val.people:
                                from mutagen.id3 import TMCL as _TMCL, TIPL as _TIPL
                                audio.delall(choice)
                                cls = _TMCL if choice.startswith('TMCL') else _TIPL
                                audio.add(cls(encoding=3, people=edited))
                                audio.save(v2_version=3)
                                print(f"{choice} updated.")
                                time.sleep(1)
                            break

                        # Check if it's a list/2D list
                        is_list = isinstance(raw_val.text, (list, tuple)) if hasattr(raw_val, 'text') else False

                        if is_list and len(raw_val.text) > 0:
                            # Use list editor
                            edited_data = _edit_list_data(raw_val.text, choice)
                            if edited_data != raw_val.text:
                                audio.delall(choice)
                                if choice.startswith('USLT'):
                                    new_frame = USLT(encoding=3, lang='eng', desc='', text=edited_data)
                                elif choice.startswith('COMM'):
                                    new_frame = COMM(encoding=3, lang='eng', desc='', text=list(edited_data))
                                else:
                                    new_frame = TextFrame(encoding=3, text=list(edited_data))
                                    new_frame.FrameID = choice.split('[')[0] if '[' in choice else choice
                                audio.add(new_frame)
                                audio.save(v2_version=3)
                                print(f"{choice} updated.")
                                time.sleep(1)
                            break
                        else:
                            # Simple text edit
                            new_content = prompt.text("New content:", default=full_text)
                            if new_content is not None:
                                audio.delall(choice)
                                if choice.startswith('USLT'):
                                    new_frame = USLT(encoding=3, lang='eng', desc='', text=new_content)
                                elif choice.startswith('COMM'):
                                    new_frame = COMM(encoding=3, lang='eng', desc='', text=[new_content])
                                else:
                                    new_frame = TextFrame(encoding=3, text=[new_content])
                                    new_frame.FrameID = choice.split('[')[0] if '[' in choice else choice
                                audio.add(new_frame)
                                audio.save(v2_version=3)
                                print(f"{choice} updated.")
                                time.sleep(1)
                            break

                elif action == "Delete tag":
                    if prompt.confirm(f"Delete {choice}?"):
                        audio.pop(choice)
                        audio.save(v2_version=3)
                        break 

                elif action == "Back":
                    break

        except Exception as e:
            # Fallback for M4P files without ID3
            xml_data = library_metadata.get('xml_data') if library_metadata else None
            display_data = xml_data or library_metadata
            
            if display_data:
                ui_utils.clear_screen()
                print(f"INSPECTING: {os.path.basename(file_path)}")
                print("\nNo ID3 tags found (M4P or MP4 file)")
                print()
                ui_utils.display_xml_metadata({'xml_data': display_data})
                print("\nThis file uses Apple Music metadata from Library.xml")
                input("\nPress Enter to continue...")
            else:
                print(f"Error: {e}")
                input("Press Enter to continue...")
            break


def browse_metadata(library: list) -> None:
    """Browse library by artist/album and inspect track metadata."""
    while True:
        all_artists = sorted(set(s['artist'] for s in library))
        categories  = sorted(set(_sort_category(a) for a in all_artists))

        if "#" in categories:
            categories.append(categories.pop(categories.index("#")))

        cat_choice = prompt.select("Category:", choices=categories + ["Exit"])
        if not cat_choice or cat_choice == "Exit":
            break

        filtered_artists = [a for a in all_artists if _sort_category(a) == cat_choice]
        artist_choice = prompt.select("Artist:", choices=filtered_artists + [".. Back"])

        if not artist_choice or artist_choice == ".. Back":
            continue

        artist_songs = [s for s in library if s.get('artist') == artist_choice]
        albums = sorted(set(s.get('album', 'Unknown') for s in artist_songs))
        album_choice = prompt.select(
            "Album:", choices=["[Bulk Edit All Artist Tracks]"] + albums + [".. Back"]
        )

        if not album_choice or album_choice == ".. Back":
            continue

        if album_choice == "[Bulk Edit All Artist Tracks]":
            bulk_tag_manager(library, paths=[s['path'] for s in artist_songs])
            continue

        tracks = sorted(
            [s for s in artist_songs if s.get('album') == album_choice],
            key=lambda x: int(x.get('track', 0) or 0)
        )

        while True:
            track_choices = [
                prompt.Choice(f"{str(s.get('track', 0)).zfill(2)} — {s['title']}", s['path'])
                for s in tracks
            ]
            path = prompt.select(
                "Track:", choices=["[Bulk Edit This Album]"] + track_choices + [".. Back"]
            )

            if not path or path == ".. Back":
                break

            if path == "[Bulk Edit This Album]":
                bulk_tag_manager(library, paths=[s['path'] for s in tracks])
                continue

            track_metadata = next((s for s in tracks if s['path'] == path), None)
            inspect_tag_loop(path, library_metadata=track_metadata)