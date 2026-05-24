"""
Metadata browser for inspecting and managing track metadata.
"""
from __future__ import annotations
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
from mutagen.id3 import ID3, USLT, COMM, SYLT, TextFrame, APIC, TXXX

from src import ui_utils
from src.album_art import get_ascii
from src.music_library import refresh_library_entry, select_from_alpha_list, get_group_sort_key


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
            frame_cls = Frames.get(base_id, TextFrame)
            new_frame = frame_cls(encoding=3, text=getattr(old_frame, 'text', [''])[0])

        audio_obj.add(new_frame)
        return True
    except Exception as e:
        print(f"Rename failed: {e}")
        return False


def _get_image_from_apic(apic_frame: APIC) -> tuple:
    """Extract image data and mime type from APIC frame. Returns (image_array, mime_type)."""
    try:
        img_data = getattr(apic_frame, 'data', b"")
        mime_type = getattr(apic_frame, 'mime', "image/jpeg")
        nparr = np.frombuffer(img_data, np.uint8)
        image = cv2.imdecode(nparr, cv2.IMREAD_COLOR)
        return image, mime_type
    except Exception as e:
        print(f"Error extracting image: {e}")
        return None, None


def _convert_ascii_from_apic(apic_frame: APIC, width: int = 80) -> str:
    """Convert APIC tag data to ASCII art string."""
    image, _ = _get_image_from_apic(apic_frame)
    if image is None:
        return "Error: Could not decode image data."
    return get_ascii(image, width)


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
            img_data = getattr(apic_frame, 'data', b"")
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
    def _apic_header() -> list[str]:
        lines = [f"EDITING: {tag_name} (APIC - Album Art)", ""]
        if view_mode == "ascii":
            art = _convert_ascii_from_apic(apic_frame, width=_get_ascii_width())
            lines.extend(art.splitlines())
        elif view_mode == "raw":
            lines.append(repr(apic_frame))
        elif view_mode == "info":
            image, mime = _get_image_from_apic(apic_frame)
            if image is not None:
                h, w = image.shape[:2]
                channels = image.shape[2] if len(image.shape) == 3 else 1
                colour_mode = {1: "Greyscale", 3: "RGB", 4: "RGBA"}.get(channels, f"{channels}ch")
                size_kb = len(getattr(apic_frame, 'data', b"")) / 1024
                lines += [
                    f"  MIME type   : {mime}",
                    f"  Description : {getattr(apic_frame, 'desc', '') or '(none)'}",
                    f"  Dimensions  : {w} × {h} px",
                    f"  Colour mode : {colour_mode}",
                    f"  File size   : {size_kb:.1f} KB ({len(getattr(apic_frame, 'data', b'')):,} bytes)",
                    f"  Encoding    : {getattr(apic_frame, 'encoding', 3)}",
                ]
            else:
                lines.append("Could not read image information.")
        lines.append("")
        return lines

    while True:
        actions = []
        if view_mode != "ascii":
            actions.append("View as ASCII Art")
        if view_mode != "raw":
            actions.append("View as Raw Data")
        if view_mode != "info":
            actions.append("View as Info")
        actions.extend(["Open in Preview", "Replace Image", "Edit Description", "Back"])

        action = prompt.select("Action:", choices=actions, header=_apic_header)

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
        elif action == "Back":
            audio_obj.save(v2_version=3)
            return True

    return True

def _edit_text_in_editor(initial_text: str) -> str | None:
    """Open system editor to edit long text strings."""
    with tempfile.NamedTemporaryFile(suffix=".txt", mode='w+', encoding='utf-8', delete=False) as tf:
        tf.write(initial_text)
        temp_path = tf.name

    try:
        # Use environment variable for editor, fallback to nano or vi
        editor = os.environ.get('EDITOR', 'vim')
        subprocess.run([editor, temp_path], check=True)

        with open(temp_path, 'r', encoding='utf-8') as f:
            return f.read().strip()
    except Exception as e:
        print(f"Error launching editor: {e}")
        return None
    finally:
        if os.path.exists(temp_path):
            os.remove(temp_path)

def _create_frame(frame_id: str, value: str):
    """Create a new ID3 frame for the given ID and value."""
    from mutagen.id3 import COMM, USLT, TextFrame, Frames

    parts = frame_id.split(':')
    base_id = parts[0].upper()

    desc = parts[1] if len(parts) > 1 else ''
    lang = parts[2] if (len(parts) > 2 and parts[2]) else 'eng'

    if base_id == 'COMM':
        return COMM(encoding=3, lang=lang, desc=desc, text=[value])
    elif base_id == 'USLT':
        return USLT(encoding=3, lang=lang, desc=desc, text=value)
    elif base_id == 'TXXX':
        return TXXX(encoding=3, desc=desc, text=[value])
    else:
        frame_cls = Frames.get(base_id, TextFrame)
        frame = frame_cls(encoding=3, text=[value])
        if frame_cls is TextFrame:
            setattr(frame, 'FrameID', base_id)
        return frame

def _is_people_frame(tag_name: str) -> bool:
    """True for TMCL/TIPL which store [[role, name], ...] pairs."""
    return tag_name.startswith(('TMCL', 'TIPL'))


def _edit_list_data(tag_data, tag_name: str):
    """Edit list or 2D list data. TMCL/TIPL get a role/name pair editor."""

    # ── TMCL / TIPL: [[role, name], ...] people frames ───────────────────
    if _is_people_frame(tag_name):
        # tag_data here is frame.people which is a list of [role, name]
        rows = [list(r) for r in tag_data]

        while True:
            def _tmcl_header() -> list[str]:
                c = ui_utils.get_terminal_width()
                cw = max(20, (c - 8) // 2)
                lines = [
                    f"EDITING: {tag_name}  ({len(rows)} entries)",
                    "─" * c,
                    f"  {'ROLE':<{cw}}  NAME",
                    "  " + "─" * (c - 4),
                ]
                for i, (role, name) in enumerate(rows):
                    r_disp = role[:cw - 2] if len(role) > cw else role
                    lines.append(f"  {i:>2}. {r_disp:<{cw}}  {name}")
                lines.append("")
                return lines

            action = prompt.select(
                "Action:",
                choices=["Add entry", "Edit entry", "Remove entry", "Clear all", "Done"],
                header=_tmcl_header,
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
        def _list_header() -> list[str]:
            lines = [f"EDITING: {tag_name} (List)", ""]
            for i, item in enumerate(tag_data):
                lines.append(f"  [{i}] {repr(item)}")
            lines.append("")
            return lines

        action = prompt.select(
            "Action:", choices=["Add item", "Remove item", "Edit item", "Clear all", "Back"],
            header=_list_header,
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
    def _2d_header() -> list[str]:
        lines = [f"EDITING: {tag_name} (2D List)", ""]
        for i, row in enumerate(tag_data):
            lines.append(f"  Row {i}: {row}")
        lines.append("")
        return lines

    action = prompt.select(
        "Action:", choices=["Add row", "Remove row", "Edit row", "Edit cell", "Clear all", "Back"],
        header=_2d_header,
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
    library is updated in-place and the cache is saved after changes.
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

    from src.music_library import TAG_MAP as _TAG_MAP
    import re as _re
    _cols = ui_utils.get_terminal_width()
    C     = ui_utils.Colours

    label = album_name or f"{len(album_tracks)} tracks"
    print(f"Scanning {len(album_tracks)} tracks…")
    all_tag_counts: Counter = Counter()
    tag_values: dict = {}

    for path in album_tracks:
        try:
            audio = ID3(path)
            all_tag_counts.update(audio.keys())
            for k in audio.keys():
                raw = audio[k]
                if k.startswith(('APIC', 'SYLT')):
                    val = k
                elif k.startswith(('TMCL', 'TIPL')):
                    val = f"{len(raw.people)} people"
                elif hasattr(raw, 'text'):
                    val = " / ".join(str(t) for t in raw.text)
                else:
                    val = str(raw)
                tag_values.setdefault(k, []).append(val)
        except Exception:
            continue

    def _bulk_header() -> list[str]:
        inner = max(20, _cols - 4)
        suffix = f"  {C.DIM}{label}  ·  {len(album_tracks)} tracks{C.RESET}"
        return [
            f"{C.DIM}╭{'─' * inner}╮{C.RESET}",
            f"{C.DIM}│{C.RESET}  {C.BOLD}Bulk edit{C.RESET}{suffix}{C.DIM}│{C.RESET}",
            f"{C.DIM}╰{'─' * inner}╯{C.RESET}",
            "",
        ]

    operation = prompt.select(
        "Operation:",
        choices=["Set value", "Delete tags", "Rename tags", "Add new tag", "Replace artwork", ".. Back"],
        header=_bulk_header,
    )

    if not operation or operation == ".. Back":
        return

    # Map friendly names back to internal operation ids used below
    _op_map = {
        "Set value":       "Set Common Value",
        "Delete tags":     "Delete Tags",
        "Rename tags":     "Rename Tags",
        "Add new tag":     "Add New Tag",
        "Replace artwork": "Replace Artwork",
    }
    operation = _op_map.get(operation, operation)

    if not all_tag_counts and operation not in ("Add New Tag", "Replace Artwork"):
        print("No tags found.")
        return

    # Column formatting logic
    ALIAS_MAX = 22
    TAG_MAX   = 28
    VAL_MAX   = 30

    def _balias(tag):
        return _TAG_MAP.get(tag.split(':')[0].split('[')[0], '')

    def _value_summary(tag) -> str:
        if tag.startswith('APIC'): return "‹image›"
        if tag.startswith(('TMCL', 'TIPL')):
            vals = tag_values.get(tag, [])
            unique_counts = set(vals)
            return f"‹{vals[0]}›" if len(unique_counts) == 1 else "‹varies›"
        if tag.startswith('SYLT'): return "‹synced lyrics›"

        vals = tag_values.get(tag, [])
        if not vals: return ""
        unique = set(vals)
        if len(unique) == 1:
            v = vals[0]
            return v if len(v) <= VAL_MAX else v[:VAL_MAX - 1] + "…"

        # Find longest common prefix
        prefix = vals[0]
        for v in vals[1:]:
            while not v.startswith(prefix):
                prefix = prefix[:-1]
                if not prefix: break

        n_vary = len(unique)
        if prefix:
            stub = prefix if len(prefix) <= VAL_MAX - 12 else prefix[:VAL_MAX - 13] + "…"
            return f"{stub}{{{n_vary} values}}"
        return f"{{{n_vary} values}}"

    bulk_tag_col   = min(TAG_MAX, max((len(t) for t in all_tag_counts), default=6))
    bulk_alias_col = min(ALIAS_MAX, max((len(_balias(t)) + 3 for t in all_tag_counts), default=0))
    _fixed = bulk_tag_col + bulk_alias_col + 20
    bulk_val_col = min(VAL_MAX, max(_cols - _fixed, 10))

    def _tag_option_title(tag, count):
        alias = _balias(tag)
        tag_disp = tag if len(tag) <= bulk_tag_col else tag[:bulk_tag_col - 1] + "…"

        if alias:
            a = f"({alias})"
            if len(a) > bulk_alias_col - 1:
                a = a[:bulk_alias_col - 3] + "…)"
            alias_str = f" {a}"
        else:
            alias_str = ""

        val_disp = _value_summary(tag)
        val_disp = val_disp if len(val_disp) <= bulk_val_col else val_disp[:bulk_val_col - 1] + "…"
        count_str = f"{count}/{len(album_tracks)}"
        return f"{tag_disp:<{bulk_tag_col}} {alias_str:<{bulk_alias_col + 1}} | {val_disp:<{bulk_val_col}}  {count_str}"

    selected_tags = []
    target_tag_id = None
    target_val = None

    if operation == "Replace Artwork":
        # Fast path: bulk replace all APIC tags across tracks
        img_path = prompt.path("Path to new artwork image:")
        if not img_path or not os.path.isfile(img_path):
            print("File not found.")
            time.sleep(1)
            return
        ext  = os.path.splitext(img_path)[1].lower()
        mime = {'.jpg': 'image/jpeg', '.jpeg': 'image/jpeg', '.png': 'image/png', '.gif': 'image/gif', '.webp': 'image/webp'}.get(ext, 'image/jpeg')
        pic_type_choice = prompt.select(
            "Picture type:",
            choices=[
                prompt.Choice("Cover (front)  [3]", value=3),
                prompt.Choice("Cover (back)   [4]", value=4),
                prompt.Choice("Artist         [8]", value=8),
                prompt.Choice("Other          [0]", value=0),
            ],
        )
        pic_type = pic_type_choice if isinstance(pic_type_choice, int) else 3
        desc = prompt.text("Description (leave blank for none):") or ''
        with open(img_path, 'rb') as f:
            _new_apic = APIC(encoding=3, mime=mime, type=pic_type, desc=desc, data=f.read())
        if not prompt.confirm(f"Replace artwork in all {len(album_tracks)} tracks?"):
            return
        count_modified = 0
        for path in album_tracks:
            try:
                audio = ID3(path)
                audio.delall('APIC')
                audio.add(_new_apic)
                audio.save(v2_version=3)
                refresh_library_entry(library, path)
                count_modified += 1
            except Exception as e:
                print(f"Error on {os.path.basename(path)}: {e}")
        print(f"Artwork updated on {count_modified} tracks.")
        time.sleep(1.5)
        return

    if operation == "Add New Tag":
        target_tag_id = prompt.text("New Tag ID (e.g. TSO2, COMM[eng]):")
        if not target_tag_id: return
        target_val = prompt.text(f"Value for {target_tag_id}:")
        if target_val is None: return
    else:
        tag_options = [prompt.Choice(title=_tag_option_title(t, c), value=t)
                       for t, c in sorted(all_tag_counts.items())]
        selected_tags = prompt.checkbox(f"Select tags to {operation.lower()}:", choices=tag_options)
        if not selected_tags: return

        if operation == "Rename Tags":
            target_val = prompt.text("New tag ID (e.g. TPE2, COMM[eng]):")
        elif operation == "Set Common Value":
            # ── Smart type-aware input ────────────────────────────────────────
            # Infer type from tag ID
            _YEAR_TAGS   = {'TDRC', 'TYER', 'TDRL', 'TDOR'}
            _NUM_TAGS    = {'TBPM', 'TLEN', 'TRCK', 'TPOS', 'TSRC'}
            _base_tag    = selected_tags[0].split('[')[0].split(':')[0] if selected_tags else ''

            if _base_tag in _YEAR_TAGS:
                # Year: show numeric prompt with current year as hint
                import datetime as _dt
                _cur_y = str(_dt.date.today().year)
                target_val = prompt.text(f"Year (e.g. {_cur_y}):")
                if target_val and not target_val.strip().isdigit():
                    print("Invalid year — must be a number.")
                    time.sleep(1)
                    return

            elif _base_tag in _NUM_TAGS:
                target_val = prompt.text(f"Value for {_base_tag} (number):")
                if target_val and not target_val.strip().replace('/', '').isdigit():
                    print("Expected a numeric value (e.g. 7 or 7/12).")
                    time.sleep(1)
                    return

            else:
                # Pattern detection for strings
                for tag in selected_tags:
                    vals = tag_values.get(tag, [])
                    if len(vals) > 1:
                        patterns = [_re.sub(r'\d', 'X', v) for v in vals]
                        if len(set(patterns)) == 1:
                            print(f"\nPattern in {tag}: {patterns[0]}")
                            if prompt.confirm("Edit using pattern template?"):
                                target_val = prompt.text("Template (X = digit):", default=vals[0])
                                break
                if target_val is None:
                    target_val = prompt.text("Common value for selected tags:")

    if not target_val and operation not in ["Delete Tags", "Add New Tag"]:
        return

    # Handle APIC separately
    apic_tags = [t for t in selected_tags if t.startswith('APIC')]
    non_apic_tags = [t for t in selected_tags if not t.startswith('APIC')]
    new_apic_frame = None
    new_apic_desc = None

    if apic_tags and operation != "Delete Tags":
        apic_action = prompt.select(
            f"Bulk APIC action ({len(apic_tags)} tags):",
            choices=["Replace Image", "Edit Description", "Edit Picture Type", "Skip APIC"]
        )
        if apic_action == "Replace Image":
            img_path = prompt.path("Path to new image:")
            if img_path and os.path.isfile(img_path):
                ext = os.path.splitext(img_path)[1].lower()
                mime = {'.jpg': 'image/jpeg', '.jpeg': 'image/jpeg',
                        '.png': 'image/png', '.gif': 'image/gif',
                        '.bmp': 'image/bmp', '.webp': 'image/webp'}.get(ext, 'image/jpeg')
                pic_type_choice = prompt.select(
                    "Picture type:",
                    choices=[
                        prompt.Choice("Cover (front) [3]", value=3),
                        prompt.Choice("Cover (back)  [4]", value=4),
                        prompt.Choice("Artist        [8]", value=8),
                        prompt.Choice("Other         [0]", value=0),
                    ]
                )
                pic_type = pic_type_choice if isinstance(pic_type_choice, int) else 3
                desc = prompt.text("Description (leave blank for none):") or ''
                with open(img_path, 'rb') as f:
                    new_apic_frame = APIC(encoding=3, mime=mime, type=pic_type, desc=desc, data=f.read())
            else:
                print("File not found, skipping APIC.")
                apic_tags = []
        elif apic_action == "Edit Description":
            new_apic_desc = prompt.text("New description for all APIC tags:")
            if new_apic_desc is None:
                apic_tags = []
        elif apic_action == "Edit Picture Type":
            pic_type_choice = prompt.select(
                "New picture type:",
                choices=[
                    prompt.Choice("Cover (front) [3]", value=3),
                    prompt.Choice("Cover (back)  [4]", value=4),
                    prompt.Choice("Artist        [8]", value=8),
                    prompt.Choice("Other         [0]", value=0),
                ]
            )
            new_apic_frame = pic_type_choice if isinstance(pic_type_choice, int) else None
            if new_apic_frame is None:
                apic_tags = []
        else:
            apic_tags = []

    if not prompt.confirm(f"Apply {operation} to {len(album_tracks)} tracks?"):
        return

    count_modified = 0
    for path in album_tracks:
        try:
            audio = ID3(path)
            changed = False

            if operation == "Add New Tag":
                from mutagen.id3 import Frames
                frame_cls = Frames.get(target_tag_id.split('[')[0], TextFrame)
                new_frame = frame_cls(encoding=3, text=[target_val])
                if frame_cls is TextFrame:
                    setattr(new_frame, 'FrameID', target_tag_id.split('[')[0])
                audio.add(new_frame)
                changed = True

            # APIC Processing
            for tag in apic_tags:
                if tag in audio:
                    if operation == "Delete Tags":
                        audio.pop(tag)
                        changed = True
                    elif new_apic_desc is not None:
                        # Description-only edit: update in place
                        audio[tag].desc = new_apic_desc
                        changed = True
                    elif isinstance(new_apic_frame, int):
                        # Picture type-only edit
                        audio[tag].type = new_apic_frame
                        changed = True
                    elif isinstance(new_apic_frame, APIC):
                        # Full image replacement
                        audio.delall(tag)
                        audio.add(new_apic_frame)
                        changed = True

            # Standard Tag Processing
            for tag in non_apic_tags:
                if tag in audio:
                    if operation == "Delete Tags":
                        audio.pop(tag)
                    elif operation == "Rename Tags":
                        old_frame = audio.pop(tag)
                        perform_rename(audio, old_frame, target_val)
                    elif operation == "Set Common Value":
                        audio[tag].text = [target_val]
                    changed = True

            if changed:
                audio.save(v2_version=3)
                count_modified += 1
                refresh_library_entry(library, path)
        except Exception as e:
            print(f"Error on {os.path.basename(path)}: {e}")

    print(f"Successfully processed {count_modified} files.")
    time.sleep(1.5)


def inspect_tag_loop(file_path: str, library_metadata: dict | None = None, library: list | None = None) -> None:
    """Browse and edit ID3 tags for a single file.

    library, if provided, is updated in-place and the cache saved after any tag change.
    """
    from src.ui_utils import format_time

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
        C    = ui_utils.Colours
        cols = ui_utils.get_terminal_width()
        name = os.path.basename(file_path)
        ext  = os.path.splitext(file_path)[1].upper().lstrip('.')
        size_str = ""
        try:
            size_str = f"  {os.path.getsize(file_path) / (1024*1024):.1f} MB"
        except OSError:
            pass
        inner = max(20, cols - 4)
        lines = [
            f"{C.DIM}╭{'─' * inner}╮{C.RESET}",
            f"{C.DIM}│{C.RESET}  {C.BOLD}{ui_utils.truncate_text(name, inner - 16)}{C.RESET}{C.DIM}  [{ext}]{size_str}{' ' * max(0, inner - len(name) - len(ext) - len(size_str) - 7)}│{C.RESET}",
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

                def _tag_header() -> list[str]:
                    C     = ui_utils.Colours
                    c     = ui_utils.get_terminal_width()
                    inner = max(10, c - 4)
                    alias = _TAG_MAP.get(choice.split(':')[0].split('[')[0], '')
                    alias_str = f"  {C.DIM}({alias}){C.RESET}" if alias else ""

                    # ── APIC: re-render art at current width ──────────────────
                    if choice.startswith('APIC'):
                        art_w  = max(20, min(c - 6, 100))
                        art    = _convert_ascii_from_apic(raw_val, width=art_w)
                        image, mime = _get_image_from_apic(raw_val)
                        h = w = 0
                        if image is not None:
                            h, w = image.shape[:2]
                        kb = len(getattr(raw_val, 'data', b'')) / 1024
                        info = f"{w}×{h}px  {mime}  {kb:.0f} KB"
                        return [
                            f"{C.BOLD}{choice}{C.RESET}{alias_str}",
                            f"{C.DIM}{info}{C.RESET}",
                            f"{C.DIM}{'─' * c}{C.RESET}",
                            *art.splitlines(),
                            f"{C.DIM}{'─' * c}{C.RESET}",
                        ]

                    # ── TMCL / TIPL: people table ─────────────────────────────
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

                    # ── SYLT ──────────────────────────────────────────────────
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

                    # ── Generic text ──────────────────────────────────────────
                    body    = repr(full_text) if not choice.startswith('USLT') else full_text
                    wrapped = ui_utils.wrap_text(body, max_width=c, margin=4)
                    return [
                        f"{C.BOLD}{choice}{C.RESET}{alias_str}",
                        f"{C.DIM}╭{'─' * inner}╮{C.RESET}",
                        *[f"{C.DIM}│{C.RESET} {ln:<{inner - 1}}{C.DIM}│{C.RESET}" for ln in wrapped[:12]],
                        f"{C.DIM}╰{'─' * inner}╯{C.RESET}",
                    ]

                actions = ["Copy to clipboard", "Paste from clipboard", "Edit content", "Rename tag", "Delete tag"]
                if choice.startswith('SYLT'):
                    actions.insert(0, "Export to LRC file")
                if choice.startswith('APIC'):
                    actions.insert(0, "Edit Image")
                actions.append("Back")

                action = prompt.select("Action:", choices=actions, header=_tag_header)

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
                            lines = clean_data.split('\r')
                            sylt_data = [(line.strip(), 0) for line in lines if line.strip()]
                            new_frame = SYLT(encoding=3, lang='eng', format=2, type=1, text=sylt_data)
                        elif choice.startswith('COMM'):
                            new_frame = COMM(encoding=3, lang='eng', desc='', text=[clean_data])
                        else:
                            frame_id = choice.split('[')[0] if '[' in choice else choice
                            new_frame = TextFrame(encoding=3, text=[clean_data])
                            setattr(new_frame, 'FrameID', frame_id)

                        audio.add(new_frame)
                        _save(audio)
                        print(f"{choice} updated.")
                        time.sleep(1)
                        break

                elif action == "Rename tag":
                    new_id = prompt.text("New tag ID (e.g. TPE2, COMM[eng]):")
                    if new_id and new_id != choice:
                        old_frame = audio.pop(choice)
                        # Type narrowing for Pylance:
                        assert isinstance(new_id, str)
                        if perform_rename(audio, old_frame, new_id):
                            _save(audio)
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
                            _save(audio)
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
                                _save(audio)
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
                                    frame_id = choice.split('[')[0] if '[' in choice else choice
                                    new_frame = TextFrame(encoding=3, text=[clean_data])
                                    setattr(new_frame, 'FrameID', frame_id)
                                audio.add(new_frame)
                                _save(audio)
                                print(f"{choice} updated.")
                                time.sleep(1)
                            break
                        else:
                            # Simple text edit
                            new_content = _edit_text_in_editor(full_text)

                            if new_content is not None:
                                audio.delall(choice)
                                if choice.startswith('USLT'):
                                    new_frame = USLT(encoding=3, lang='eng', desc='', text=new_content)
                                elif choice.startswith('COMM'):
                                    new_frame = COMM(encoding=3, lang='eng', desc='', text=[new_content])
                                else:
                                    frame_id = choice.split('[')[0] if '[' in choice else choice
                                    # This fixes the 'clean_data' reference error in your original code
                                    new_frame = TextFrame(encoding=3, text=[new_content])
                                    setattr(new_frame, 'FrameID', frame_id)

                                audio.add(new_frame)
                                _save(audio)
                                print(f"{choice} updated successfully.")
                                time.sleep(1)
                                break

                elif action == "Delete tag":
                    if prompt.confirm(f"Delete {choice}?"):
                        audio.pop(choice)
                        _save(audio)
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

    # Build artist list — prefer album_artist, fall back to artist
    def _display_artist(song: dict) -> str:
        return song.get('album_artist') or song.get('artist') or 'Unknown Artist'

    # Sort key for an artist name — uses sort tags from any song in their group
    def _artist_sort_key(name: str, artist_songs: list) -> str:
        return get_group_sort_key(name, artist_songs, 'artist')

    while True:
        # Group songs by display artist
        artist_groups: dict[str, list] = {}
        for s in library:
            artist_groups.setdefault(_display_artist(s), []).append(s)

        # Sort artists by sort key
        all_artists = sorted(
            artist_groups.keys(),
            key=lambda a: _artist_sort_key(a, artist_groups[a])
        )

        filtered_artists = select_from_alpha_list(
            all_artists,
            sort_key_fn=lambda a: _artist_sort_key(a, artist_groups[a]),
            message="Select Artist:",
        )
        if filtered_artists is None:
            break

        artist_choice = prompt.select("Artist:", choices=filtered_artists + [".. Back"])
        if not artist_choice or artist_choice == ".. Back":
            continue

        artist_songs = artist_groups[artist_choice]

        # Group artist songs by album, sort by Album Sort Order then album name
        album_groups: dict[str, list] = {}
        for s in artist_songs:
            album_groups.setdefault(s.get('album', 'Unknown'), []).append(s)

        albums_sorted = sorted(
            album_groups.keys(),
            key=lambda a: get_group_sort_key(a, album_groups[a], 'album')
        )

        album_choice = prompt.select(
            "Album:", choices=["Edit all artist tracks"] + albums_sorted + [".. Back"]
        )

        if not album_choice or album_choice == ".. Back":
            continue

        if album_choice == "Edit all artist tracks":
            bulk_tag_manager(library, paths=[s['path'] for s in artist_songs])
            continue

        tracks = sorted(
            album_groups[album_choice],
            key=lambda x: (int(x.get('disc', 1) or 1), int(x.get('track', 0) or 0))
        )

        while True:
            track_choices = [
                prompt.Choice(f"{str(s.get('track', 0)).zfill(2)} — {s['title']}", s['path'])
                for s in tracks
            ]
            path = prompt.select(
                "Track:", choices=["Edit all album tracks"] + track_choices + [".. Back"]
            )

            if not path or path == ".. Back":
                break

            if path == "Edit all album tracks":
                bulk_tag_manager(library, paths=[s['path'] for s in tracks])
                continue

            track_metadata = next((s for s in tracks if s['path'] == path), None)
            inspect_tag_loop(path, library_metadata=track_metadata, library=library)
