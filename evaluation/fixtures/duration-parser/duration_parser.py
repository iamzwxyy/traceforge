def normalize_seconds(value: object) -> int:
    """Return a non-negative integer duration in seconds."""
    if not isinstance(value, int):
        raise TypeError("seconds must be an integer")
    if value < 0:
        raise ValueError("seconds must be non-negative")
    return value
