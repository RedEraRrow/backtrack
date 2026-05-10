"""
Listening history management.

Tracks and manages user's listening history.
"""

import os
import datetime


HISTORY_FILE = os.path.join(os.path.dirname(__file__), "../data/history.log")


def log_listening_history(file_path: str, start_time: float, end_time: float) -> None:
    """
    Log a completed listening session.
    
    Args:
        file_path: Path to the audio file
        start_time: Session start timestamp
        end_time: Session end timestamp
    """
    duration_listened = int(end_time - start_time)
    timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    
    try:
        with open(HISTORY_FILE, "a", encoding="utf-8") as f:
            f.write(f"{timestamp} | {duration_listened}s | {file_path}\n")
    except Exception:
        pass  # Silently fail if history can't be logged


def get_recent_paths() -> set:
    """Get set of recently played file paths."""
    if not os.path.exists(HISTORY_FILE):
        return set()
    
    paths = set()
    try:
        with open(HISTORY_FILE, "r", encoding="utf-8") as f:
            for line in f:
                parts = line.strip().split(" | ")
                if len(parts) == 3:
                    paths.add(parts[2])
    except Exception:
        pass
    
    return paths


def clear_history() -> bool:
    """Clear the history log."""
    try:
        if os.path.exists(HISTORY_FILE):
            os.remove(HISTORY_FILE)
        return True
    except Exception:
        return False


def get_history(limit: int = 30) -> list:
    """
    Get recent history entries.
    
    Args:
        limit: Maximum number of entries to return
        
    Returns:
        List of (timestamp, duration, path) tuples
    """
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
    except Exception:
        pass
    
    return entries
