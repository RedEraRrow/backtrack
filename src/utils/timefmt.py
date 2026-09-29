"""Timestamps as text: the editors' mm:ss.mmm clock and SRT's HH:MM:SS,mmm.
Both round to whole milliseconds first, so 59.9996 s reads 01:00.000, never
00:60.000."""

CLOCK_UNTIMED = "──:──.───"


def _ms(t: float) -> int:
    return round(max(0.0, float(t)) * 1000)


def clock(t: float | None) -> str:
    """Seconds as MM:SS.mmm (minutes grow past 99), or a dashed placeholder
    when there is no time."""
    if t is None:
        return CLOCK_UNTIMED
    m, ms = divmod(_ms(t), 60000)
    return f"{m:02d}:{ms // 1000:02d}.{ms % 1000:03d}"


def srt(t: float) -> str:
    """Seconds as an SRT timestamp, HH:MM:SS,mmm."""
    h, ms = divmod(_ms(t), 3600000)
    return f"{h:02d}:{ms // 60000:02d}:{ms // 1000 % 60:02d},{ms % 1000:03d}"


if __name__ == "__main__":
    assert clock(59.9996) == "01:00.000"
    assert clock(125.5) == "02:05.500"
    assert clock(None) == CLOCK_UNTIMED
    assert clock(-3) == "00:00.000"
    assert srt(3599.9996) == "01:00:00,000"
    assert srt(3723.042) == "01:02:03,042"
    print("ok")
