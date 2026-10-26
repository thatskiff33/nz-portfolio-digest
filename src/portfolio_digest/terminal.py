"""Display untrusted stored text without executing terminal control sequences."""

import unicodedata


def terminal_text(value: str) -> str:
    return "".join(
        ch.encode("unicode_escape").decode("ascii")
        if unicodedata.category(ch) in ("Cc", "Cs")
        else ch
        for ch in value
    )
