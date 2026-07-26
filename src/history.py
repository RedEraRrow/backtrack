"""Listening-history log: append plays and read/clear recent entries."""
from __future__ import annotations
import os
import datetime
from src.config import CONFIG_DIR

HISTORY_FILE = CONFIG_DIR / 'history.log'


def log_listening_history(file_path: str, start_time: float, end_time: float) -> None:
    """Append one play entry (timestamp, duration, path) to the history log."""
    duration_listened = int(end_time - start_time)
    timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    try:
        with open(HISTORY_FILE, "a", encoding="utf-8") as f:
            f.write(f"{timestamp} | {duration_listened}s | {file_path}\n")
    except OSError:
        pass  # Silently fail if history can't be logged


def get_recent_paths() -> set:
    """Every distinct file path that appears in the history log."""
    if not os.path.exists(HISTORY_FILE):
        return set()

    paths = set()
    try:
        with open(HISTORY_FILE, "r", encoding="utf-8") as f:
            for line in f:
                parts = line.strip().split(" | ")
                if len(parts) == 3:
                    paths.add(parts[2])
    except OSError:
        pass

    return paths


def clear_history() -> bool:
    """Delete the history log file. Returns True on success."""
    try:
        if os.path.exists(HISTORY_FILE):
            os.remove(HISTORY_FILE)
        return True
    except OSError:
        return False


def get_history(limit: int = 30) -> list:
    """The most recent history entries, newest first, as (timestamp, duration, path) tuples."""
    if not os.path.exists(HISTORY_FILE):
        return []

    entries = []
    try:
        with open(HISTORY_FILE, "r", encoding="utf-8") as f:
            lines = f.readlines()[::-1]  # Reverse for most recent first

        for line in lines[:limit]:
            parts = line.strip().split(" | ")
            if len(parts) == 3:
                entries.append(tuple(parts))
    except OSError:
        pass

    return entries
