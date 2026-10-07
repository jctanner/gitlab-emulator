"""CI trace redaction helpers."""

import re


def masked_values_from_variables(variables: dict[str, object]) -> list[str]:
    values: list[str] = []
    for variable in variables.values():
        if not isinstance(variable, dict) or not variable.get("masked"):
            continue
        value = str(variable.get("value", ""))
        if value:
            values.append(value)
    return sorted(set(values), key=len, reverse=True)


def redact_trace_text(text: str, variables: dict[str, object]) -> str:
    redacted = text
    for value in masked_values_from_variables(variables):
        redacted = redacted.replace(value, "[MASKED]")
    return redacted


# A masked value is overwritten with NUL bytes of the same length in the stored
# trace bytes. The trace keeps the runner's byte offsets, which the runner
# relies on to resume an upload, and the secret is never stored. The display
# text shows each run of NULs as [MASKED].
_MASK_BYTE = b"\x00"


def mask_trace_bytes(raw: bytes, variables: dict[str, object]) -> bytes:
    for value in masked_values_from_variables(variables):
        needle = value.encode()
        raw = raw.replace(needle, _MASK_BYTE * len(needle))
    return raw


def trace_display_text(raw: bytes) -> str:
    """Decode stored trace bytes for display, showing masked runs as [MASKED].

    An incomplete UTF-8 sequence (a chunk that ends mid-character) shows as
    U+FFFD until the rest of the character arrives.
    """
    text = raw.decode("utf-8", errors="replace")
    return re.sub("\x00+", "[MASKED]", text)
