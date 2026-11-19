"""Identity helpers for WiZ devices discovered with different MAC formats."""


def mac_identity(mac: str | None) -> str | None:
    """Compare valid MAC addresses independent of case and separators."""
    if not isinstance(mac, str) or not mac.strip():
        return None
    value = mac.strip().lower()
    compact = value.replace(":", "").replace("-", "").replace(".", "")
    if len(compact) == 12 and all(c in "0123456789abcdef" for c in compact):
        return compact
    return value
