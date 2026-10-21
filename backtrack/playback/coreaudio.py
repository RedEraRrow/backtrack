"""The few things takeover needs from macOS's CoreAudio that mpv doesn't give:
the output played through (by its UID, the end of mpv's "coreaudio/<UID>"
name; the default when there's none), its own volume, its sample rate,
whether it's Bluetooth, and putting a rate back that a crash left changed
(mpv puts it back itself on a clean exit).
Elsewhere, and whenever CoreAudio won't answer, these do nothing."""
from __future__ import annotations

import ctypes
import ctypes.util
import json
import struct
import sys

from backbone.log import log

_SYSTEM = 1                                            # kAudioObjectSystemObject


def _code(s: str) -> int:
    return struct.unpack('>I', s.encode())[0]


class _Address(ctypes.Structure):
    _fields_ = [('selector', ctypes.c_uint32), ('scope', ctypes.c_uint32), ('element', ctypes.c_uint32)]


def _frameworks():
    if sys.platform != 'darwin':
        return None, None
    try:
        return (ctypes.CDLL(ctypes.util.find_library('CoreAudio')),
                ctypes.CDLL(ctypes.util.find_library('CoreFoundation')))
    except OSError as exc:
        log.warning("CoreAudio unavailable: %s", exc)
        return None, None


_ca, _cf = _frameworks()


def _get(obj: int, selector: str, ctype, count: int = 1, scope: str = 'glob', element: int = 0) -> list:
    """A property of a CoreAudio object as a list of `ctype`; [] when it has none."""
    if _ca is None:
        return []
    addr = _Address(_code(selector), _code(scope), element)
    size, buf = ctypes.c_uint32(ctypes.sizeof(ctype) * count), (ctype * count)()
    if _ca.AudioObjectGetPropertyData(obj, ctypes.byref(addr), 0, None, ctypes.byref(size), buf):
        return []
    return list(buf)[: size.value // ctypes.sizeof(ctype)]


def _uid(device: int) -> str:
    ref = _get(device, 'uid ', ctypes.c_void_p)
    if not ref or not ref[0]:
        return ""
    buf = ctypes.create_string_buffer(512)
    _cf.CFStringGetCString(ctypes.c_void_p(ref[0]), buf, 512, 0x08000100)      # UTF-8
    return buf.value.decode()


def output(uid: str = "") -> int | None:
    """The device with `uid`, or the default output when `uid` is ''."""
    if uid:
        return next((d for d in _get(_SYSTEM, 'dev#', ctypes.c_uint32, 64) if _uid(d) == uid), None)
    found = _get(_SYSTEM, 'dOut', ctypes.c_uint32)
    return found[0] if found else None


def _volume_elements(device: int) -> list[int]:
    """Where the device's own output volume can be set: its main control, else
    each channel's (left, right); [] when it has none."""
    def settable(element: int) -> bool:
        addr, ok = _Address(_code('volm'), _code('outp'), element), ctypes.c_ubyte(0)
        return (bool(_ca.AudioObjectHasProperty(device, ctypes.byref(addr)))
                and not _ca.AudioObjectIsPropertySettable(device, ctypes.byref(addr), ctypes.byref(ok)) and bool(ok.value))
    if _ca is None or device is None:
        return []
    return [0] if settable(0) else [e for e in (1, 2) if settable(e)]


def volume(device: int | None) -> float | None:
    """The device's own output volume, 0-1 (the scale the Mac's volume keys
    move), or None when it has none to set."""
    levels = [v for e in _volume_elements(device) for v in _get(device, 'volm', ctypes.c_float, 1, 'outp', e)]
    return sum(levels) / len(levels) if levels else None


def set_volume(device: int | None, level: float) -> bool:
    """Set the device's own output volume (0-1); whether it has one."""
    elements = _volume_elements(device)
    value = ctypes.c_float(max(0.0, min(1.0, level)))
    for e in elements:
        addr = _Address(_code('volm'), _code('outp'), e)
        if _ca.AudioObjectSetPropertyData(device, ctypes.byref(addr), 0, None, 4, ctypes.byref(value)):
            log.warning("couldn't set the output's volume (element %d)", e)
    return bool(elements)


def is_bluetooth(device: int | None) -> bool:
    return device is not None and _get(device, 'tran', ctypes.c_uint32)[:1] in ([_code('blue')], [_code('blea')])


def _rate(device: int) -> float:
    found = _get(device, 'nsrt', ctypes.c_double)
    return found[0] if found else 0.0


def remember_rate(state_file, uid: str = "") -> None:
    """Note the output (output()) and its rate, before takeover first changes
    it (once: a note already there is the rate to go back to)."""
    device = output(uid)
    if device is None or state_file.exists():
        return
    try:
        state_file.write_text(json.dumps({'uid': _uid(device), 'rate': _rate(device)}), encoding='utf-8')
    except OSError as exc:
        log.warning("couldn't note the output's rate before takeover: %s", exc)


def restore_rate(state_file) -> None:
    """Put back the rate remember_rate noted, if a run ended without mpv doing
    it (a crash), and drop the note."""
    try:
        saved = json.loads(state_file.read_text(encoding='utf-8'))
    except FileNotFoundError:
        return
    except (OSError, ValueError) as exc:
        log.warning("takeover's note of the output rate unreadable: %s", exc)
        saved = {}
    device = output(saved['uid']) if saved.get('uid') else None
    left = _rate(device) if device is not None else 0.0
    if left and saved.get('rate') and left != saved['rate']:
        addr, rate = _Address(_code('nsrt'), _code('glob'), 0), ctypes.c_double(saved['rate'])
        err = _ca.AudioObjectSetPropertyData(device, ctypes.byref(addr), 0, None, 8, ctypes.byref(rate))
        log.warning("output %s left at %g Hz by a run that ended early: %s %g Hz", saved['uid'], left,
                    "couldn't put back" if err else "put back to", saved['rate'])
    state_file.unlink(missing_ok=True)
