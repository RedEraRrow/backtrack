"""ID3 frame creation, value prompts, and bulk-operation helpers."""
from __future__ import annotations
from typing import Any, Optional
from mutagen.id3 import ID3
from mutagen.id3._frames import SYLT, USLT, TMCL, TIPL, TXXX, WXXX, COMM  # type: ignore[reportPrivateImportUsage]
from mutagen.id3._frames import APIC, EQU2, RVA2

from src.id3.tag_registry import (TAG_REGISTRY, TagInfo, parse_composite_tag_id, get_tag_info, get_tag_category, get_preferred_tag_name)
from src.music_library import refresh_library_entry

import mutagen.id3
import os
import time
from src.utils import prompt, ui_utils



_EXT_TO_MIME: dict[str, str] = {
    '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg',
    '.png': 'image/png', '.gif': 'image/gif',
    '.bmp': 'image/bmp', '.webp': 'image/webp',
}


def _prompt_for_image_file() -> bytes | None:
    img_path = prompt.path("Path to image:")
    if not img_path or not os.path.isfile(img_path):
        return None

    try:
        with open(img_path, 'rb') as f:
            return f.read()
    except Exception:
        return None


def _get_mime_type(data: bytes) -> str:
    if data.startswith(b'\xFF\xD8\xFF'):
        return 'image/jpeg'
    elif data.startswith(b'\x89PNG'):
        return 'image/png'
    elif data.startswith(b'GIF8'):
        return 'image/gif'
    elif data.startswith(b'BM'):
        return 'image/bmp'
    elif data.startswith(b'RIFF') and b'WEBP' in data[:12]:
        return 'image/webp'
    return 'image/jpeg'


def _prompt_for_image_metadata() -> tuple[int, str] | None:
    """
    Prompt for picture type and description.
    Returns (pic_type, description) or None if cancelled.
    """
    pic_type = prompt.select(
        "Picture type:",
        choices=[
            prompt.Choice("Cover (front) [3]", 3),
            prompt.Choice("Cover (back)  [4]", 4),
            prompt.Choice("Artist        [8]", 8),
            prompt.Choice("Other         [0]", 0),
        ]
    )
    if not isinstance(pic_type, int):
        return None

    desc = prompt.text("Description (leave blank for none):") or ''
    return pic_type, desc


def create_frame(tag_id: str, value: Any) -> APIC | SYLT | USLT | TMCL | TIPL | EQU2 | RVA2 | None:
    """Create the correct mutagen frame for tag_id from value, or None on failure."""
    if value is None:
        return None

    info = get_tag_info(tag_id)
    if not info:
        return None

    parsed_base, parsed_desc, parsed_lang = parse_composite_tag_id(tag_id)

    try:
        # Audio-adjustment frames carry structured payloads from their own
        # editors (see _prompt_for_equalisation / _prompt_for_rva2).
        if parsed_base == 'EQU2':
            if isinstance(value, dict) and value.get('__eq__'):
                return EQU2(method=0, desc='', adjustments=list(value.get('adjustments', [])))
            return None

        if parsed_base == 'RVA2':
            if isinstance(value, dict) and value.get('__rva2__'):
                return RVA2(desc='', channel=1, gain=float(value['gain']), peak=0.0)
            return None

        # APIC is a BINARY frame but its UI category is 'image'. (The old
        # `frame_type == 'IMAGE'` check never matched, so APICs never saved.)
        if info.ui_category == 'image':
            if isinstance(value, dict) and value.get('__image__'):
                data = value.get('data') or b''
                if not isinstance(data, bytes) or not data:
                    return None
                return APIC(encoding=3, mime=_get_mime_type(data),
                            type=int(value.get('type', 3)), desc=str(value.get('desc', '')),
                            data=data)
            if isinstance(value, bytes) and len(value) > 0:
                return APIC(encoding=3, mime=_get_mime_type(value), type=3, desc='', data=value)
            return None

        if info.frame_type == 'TEXT':
            text_val = str(value).strip()
            if not text_val:
                return None

            frame_class = info.mutagen_class

            if frame_class in [TXXX, WXXX]:
                return frame_class(encoding=3, desc=parsed_desc, text=text_val)

            if frame_class in [COMM, USLT]:
                clean_lang = str(parsed_lang).strip() if parsed_lang else 'eng'
                if len(clean_lang) != 3:
                    clean_lang = 'eng'
                return frame_class(encoding=3, lang=clean_lang, desc=parsed_desc, text=text_val)

            return frame_class(encoding=3, text=[text_val])

        elif info.format_spec == 'ISO8601':
            date_val = str(value).strip()
            if not date_val or not any(c.isdigit() for c in date_val):
                return None
            return info.mutagen_class(encoding=3, text=[date_val])

        elif info.frame_type == 'FRACTIONAL':
            frac_val = str(value).strip()
            if not frac_val or not any(c.isdigit() for c in frac_val):
                return None
            return info.mutagen_class(encoding=3, text=[frac_val])

        elif info.frame_type == 'NUMERIC':
            num_val = str(value).strip()
            if not num_val or not any(c.isdigit() for c in num_val):
                return None
            return info.mutagen_class(encoding=3, text=[num_val])

        elif info.frame_type == 'LIST':
            if isinstance(value, str):
                # Plain string (e.g. genre, language) — wrap in a text list frame.
                return info.mutagen_class(encoding=3, text=[value])
            if isinstance(value, list) and value:
                people_list = []
                for item in value:
                    if isinstance(item, (tuple, list)) and len(item) == 2:
                        people_list.append((str(item[0]).strip(), str(item[1]).strip()))

                if not people_list:
                    return None

                return info.mutagen_class(encoding=3, people=people_list)
            return None

        elif info.frame_type == 'DATE':
            val = str(value).strip()
            if '-' in val:
                parts = val.split('-')
                if len(parts) == 3:
                    val = parts[2].zfill(2) + parts[1].zfill(2)
            if not val or not any(c.isdigit() for c in val):
                return None
            return info.mutagen_class(encoding=3, text=[val])

        elif info.frame_type == 'YEAR':
            val = str(value).strip()
            if '-' in val:
                val = val.split('-')[0]
            if not val or not any(c.isdigit() for c in val):
                return None
            return info.mutagen_class(encoding=3, text=[val])

        elif info.frame_type == 'TIME':
            val = str(value).strip()
            if ':' in val:
                parts = val.split(':')
                val = parts[0].zfill(2) + (parts[1].zfill(2) if len(parts) > 1 else "00")
            if not val or not any(c.isdigit() for c in val):
                return None
            return info.mutagen_class(encoding=3, text=[val])

        elif tag_id.startswith('SYLT'):
            return None

        return None

    except (ValueError, TypeError, AttributeError):
        return None


def create_apic_frame(data: bytes, mime: str = '', pic_type: int = 3, desc: str = '') -> APIC | None:
    """
    Create APIC (album art) frame with explicit metadata.
    Falls back to magic-byte detection if mime not provided.
    """
    if not isinstance(data, bytes) or len(data) == 0:
        return None

    if not mime:
        mime = _get_mime_type(data)

    if not isinstance(pic_type, int):
        pic_type = 3

    try:
        return APIC(encoding=3, mime=mime, type=pic_type, desc=desc, data=data)
    except Exception:
        return None


def rename_frame(audio_obj: ID3, old_frame, new_id: str) -> bool:
    """Rename a frame while preserving type and value."""
    old_id = None
    for key in audio_obj.keys():
        if audio_obj[key] is old_frame:
            old_id = key
            break

    if not old_id:
        return False

    old_info = get_tag_info(old_id)
    new_info = get_tag_info(new_id)
    if not old_info or not new_info or old_info.ui_category != new_info.ui_category:
        return False

    if old_info.ui_category == 'image':
        return False

    if hasattr(old_frame, 'people'):
        value = old_frame.people
    elif hasattr(old_frame, 'text'):
        value = old_frame.text[0] if old_frame.text else None
    else:
        value = str(old_frame)

    if value is None:
        return False

    new_frame = create_frame(new_id, value)
    if not new_frame:
        return False

    try:
        audio_obj.pop(old_id)
        audio_obj.add(new_frame)
        return True
    except (AttributeError, KeyError):
        return False


def _prompt_for_equalisation(current_value: Any) -> dict | None:
    """Interactive graphic equaliser for an EQU2 frame."""
    existing: list[tuple[float, float]] = []
    if current_value is not None and hasattr(current_value, 'adjustments'):
        existing = [(float(freq), float(gain)) for freq, gain in current_value.adjustments]

    adjustments = prompt.equaliser_edit("Equalisation — boost/cut per frequency band:", existing)
    if adjustments is None:
        return None
    return {'__eq__': True, 'adjustments': sorted(adjustments)}


def _prompt_for_rva2(current_value: Any) -> dict | None:
    """Single master-channel volume adjustment (dB) for an RVA2 frame."""
    cur = f"{current_value.gain:g}" if (current_value is not None and hasattr(current_value, 'gain')) else ""
    val = prompt.text("Volume adjustment (dB, +boost / −cut, master channel):", default=cur)
    if val is None:
        return None
    val = val.strip().lstrip('+')
    if not val:
        return None
    try:
        gain = float(val)
    except ValueError:
        ui_utils.show_status("Enter a number in decibels, e.g. 3 or -2.5")
        return None
    return {'__rva2__': True, 'gain': gain}


def prompt_for_value(tag_id: str, current_value: Any = None, initial_people: list | None = None) -> Any | None:
    info = get_tag_info(tag_id)
    if not info:
        return None

    label = get_preferred_tag_name(tag_id)
    ui_cat = info.ui_category
    fmt = info.format_spec
    base_id, _, _ = parse_composite_tag_id(tag_id)

    # Structured audio-adjustment editors (binary frames).
    if base_id == 'EQU2':
        return _prompt_for_equalisation(current_value)
    if base_id == 'RVA2':
        return _prompt_for_rva2(current_value)

    # Extract editor-ready defaults from whatever current_value is.
    # It may be a raw mutagen frame (single-file edit), a summary string
    # (bulk edit path), or None (new tag).
    if current_value is None:
        default_val = ""
    elif hasattr(current_value, 'people'):
        # People frame (TIPL, TMCL)
        if initial_people is None:
            initial_people = list(current_value.people) if current_value.people else []
        default_val = ""
    elif hasattr(current_value, 'text'):
        default_val = str(current_value.text[0]) if current_value.text else ""
    else:
        # Already a plain string (bulk edit summary)
        default_val = str(current_value)

    # Structural types dispatched on ui_category
    if ui_cat == 'image':
        img_data = _prompt_for_image_file()
        if not img_data:
            ui_utils.show_status("Cancelled")
            return None
        meta = _prompt_for_image_metadata()
        if not meta:
            ui_utils.show_status("Cancelled")
            return None
        pic_type, desc = meta
        return {'__image__': True, 'data': img_data, 'type': pic_type, 'desc': desc}

    if ui_cat == 'lyrics':
        ui_utils.show_status("Use lyric sync tool for SYLT")
        return None

    if ui_cat == 'multiline text':
        return prompt.system_editor_edit(initial_text=default_val)

    if ui_cat == 'people':
        return prompt.list_edit(f"{label}:", initial_people or [], ("ROLE", "NAME"))

    # Format-spec-driven dispatch for all data types
    if fmt == 'ISO8601':
        return prompt.datetime_edit(f"{label}:", initial=default_val)

    if fmt == 'DDMM':
        cal_init = default_val
        if len(default_val) == 4 and default_val.isdigit():
            year = time.localtime().tm_year
            cal_init = f"{year}-{default_val[2:]}-{default_val[:2]}"
        return prompt.calendar_select(f"{label}:", initial=cal_init)

    if fmt == 'YYYY':
        cal_init = f"{default_val}-01-01" if len(default_val) == 4 and default_val.isdigit() else default_val
        result = prompt.calendar_select(f"{label}:", initial=cal_init)
        return result[:4] if result else None

    if fmt == 'HHMM':
        init = f"{default_val[:2]}:{default_val[2:]}:00" if len(default_val) == 4 and default_val.isdigit() else default_val
        return prompt.time_edit(f"{label}:", initial=init or "00:00:00")

    if fmt == 'FRACTIONAL':
        base_id, _, _ = parse_composite_tag_id(tag_id)
        result = prompt.fraction_edit(f"Edit {label}:", tag=base_id, value=default_val)
        if result is None:
            return None
        curr = result.get('current', '').strip()
        tot = result.get('total', '').strip()
        if curr and tot:
            return f"{curr}/{tot}"
        return curr or None

    if fmt == 'INT_BIG':
        hint = " (milliseconds)" if ui_cat == 'duration' else ""
        return prompt.text(f"{label}{hint}:", default=default_val)

    # Default: plain text (TEXT_UTF8, URL, LIST_STRING, etc.)
    return prompt.text(f"{label}:", default=default_val)


def display_tag_id(tag_id: str) -> str:
    """Human-facing form of a frame key: drop a trailing ':' from an empty
    descriptor (e.g. mutagen's "APIC:" / "TXXX:" key → "APIC" / "TXXX")."""
    return tag_id[:-1] if tag_id.endswith(':') else tag_id


def summarize_tag_value(tag_id: str, raw_frame) -> str:
    info = get_tag_info(tag_id)
    if not info:
        return str(raw_frame)

    # PEOPLE
    if info.ui_category == 'people':
        people = getattr(raw_frame, 'people', [])
        return f"{len(people)} people"

    # IMAGE
    if info.ui_category == 'image':
        img_data = getattr(raw_frame, 'data', b'')
        mime = getattr(raw_frame, 'mime', '').split("/")[-1].upper()
        b = len(img_data)
        return f"image [{mime}] ({b / 1024:.0f} KB)"

    # LYRICS (SYLT)
    if info.ui_category == 'lyrics':
        sylt_data = getattr(raw_frame, 'text', [])
        return f"{len(sylt_data)} lines"

    # AUDIO ADJUSTMENT (EQU2 / RVA2)
    if hasattr(raw_frame, 'adjustments'):
        bands = getattr(raw_frame, 'adjustments', [])
        return f"{len(bands)} band(s)"
    if hasattr(raw_frame, 'gain') and hasattr(raw_frame, 'channel'):
        return f"{getattr(raw_frame, 'gain', 0):+g} dB"

    # Generic text
    if hasattr(raw_frame, 'text'):
        text = "".join(str(t).replace("\n", "\\") for t in raw_frame.text)
        return text[:100]

    return str(raw_frame)[:100]


def collect_tag_data(paths: list[str]) -> tuple[dict, dict, dict]:
    tag_counts = {}
    tag_values = {}
    people_tags = {}

    for path in paths:
        try:
            audio = ID3(path)
            for tag_id in audio.keys():
                tag_counts[tag_id] = tag_counts.get(tag_id, 0) + 1

                frame = audio[tag_id]
                info = get_tag_info(tag_id)

                if info and info.ui_category == 'people':
                    people = getattr(frame, 'people', [])
                    if people:
                        if tag_id not in people_tags:
                            people_tags[tag_id] = set()
                        people_tags[tag_id].update(people)
                else:
                    if tag_id not in tag_values:
                        tag_values[tag_id] = []

                    val = summarize_tag_value(tag_id, frame)
                    if val not in tag_values[tag_id]:
                        tag_values[tag_id].append(val)
        except (mutagen.id3.ID3NoHeaderError, OSError, IOError):  # type: ignore[reportPrivateImportUsage]
            # skip unreadable files
            pass

    people_tags_list = {k: list(v) for k, v in people_tags.items()}

    return tag_counts, tag_values, people_tags_list


def apply_bulk_edit(
    audio: ID3,
    tag_id: str,
    operation: str,
    new_value: Any = None,
    new_tag_id: str | None = None
) -> bool:
    try:
        if operation == 'set':
            if new_value is None:
                return False
            audio.delall(tag_id)
            new_frame = create_frame(tag_id, new_value)
            if not new_frame:
                return False
            audio.add(new_frame)
            return True

        elif operation == 'rename':
            if not new_tag_id or new_tag_id == tag_id:
                return False

            old_info = get_tag_info(tag_id)
            new_info = get_tag_info(new_tag_id)
            if not old_info or not new_info or old_info.ui_category != new_info.ui_category:
                return False

            if tag_id not in audio:
                return False

            old_frame = audio.pop(tag_id)
            if not rename_frame(audio, old_frame, new_tag_id):
                audio.add(old_frame)
                return False
            return True

        elif operation == 'delete':
            audio.delall(tag_id)
            return True

        return False

    except (KeyError, AttributeError, ValueError):
        return False


def apply_bulk_operation_to_files(
    file_paths: list[str],
    operation: str,
    tag_ids: list[str],
    target_value: Any = None,
    library: list | None = None
) -> tuple[int, int]:
    success_count = 0
    fail_count = 0

    for path in file_paths:
        try:
            audio = ID3(path)
            changed = False

            for tag_id in tag_ids:
                if operation == 'set':
                    if apply_bulk_edit(audio, tag_id, 'set', target_value):
                        changed = True
                        success_count += 1
                    else:
                        fail_count += 1
                elif operation == 'rename':
                    if apply_bulk_edit(audio, tag_id, 'rename', new_tag_id=target_value):
                        changed = True
                        success_count += 1
                    else:
                        fail_count += 1
                elif operation == 'delete':
                    if apply_bulk_edit(audio, tag_id, 'delete'):
                        changed = True
                        success_count += 1
                    else:
                        fail_count += 1

            if changed:
                audio.save(v2_version=3)
                if library is not None:
                    try:
                        refresh_library_entry(library, path)
                    except Exception:
                        pass
        except (mutagen.id3.ID3NoHeaderError, OSError, IOError):  # type: ignore[reportPrivateImportUsage]
            fail_count += len(tag_ids)

    return success_count, fail_count
