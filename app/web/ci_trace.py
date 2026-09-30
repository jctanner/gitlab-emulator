"""Render a GitLab runner job trace as HTML, keeping it a plain log.

The runner writes a trace containing more than text:

- ANSI SGR colour and style codes, and cursor codes such as ``ESC[0K``;
- ``section_start:<epoch>:<name>`` / ``section_end:<epoch>:<name>`` markers;
- with timestamps enabled, a ``<iso time> <stream><O|E><+| >`` prefix on every
  line, where ``+`` means the chunk continues the previous line.

The output stays the log as written: one line per line of output, in the
runner's own format with its timestamp prefix. Colours are kept as spans,
control sequences and section markers are removed, and everything is
HTML-escaped. Nothing is added: no line numbers, folding or summaries.
"""

from __future__ import annotations

import html
import re
from dataclasses import dataclass

from markupsafe import Markup

_STAMPED = re.compile(
    r"^(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d+)?Z) (\d\d)([OE])([+ ])(.*)$"
)
_SECTION = re.compile(r"section_(?:start|end):\d+:[\w.\-]+(?:\[[^\]\r\n]*\])?")
_CSI = re.compile(r"\x1b\[([0-9;:?]*)([@-~])")
_OTHER_ESCAPE = re.compile(
    r"\x1b(?:\][^\x07\x1b]*(?:\x07|\x1b\\)|[@-Z\\-_]|[()][0-9A-B])"
)
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")

_STANDARD_COLORS = 8


@dataclass
class _Style:
    fg: str | None = None
    bg: str | None = None
    bold: bool = False
    dim: bool = False
    italic: bool = False
    underline: bool = False

    def reset(self) -> None:
        self.fg = self.bg = None
        self.bold = self.dim = self.italic = self.underline = False

    def is_plain(self) -> bool:
        return not (
            self.fg or self.bg or self.bold or self.dim or self.italic
            or self.underline
        )


def _indexed_color(index: int) -> str | None:
    """A CSS value for an xterm 256-colour index, or a palette class name."""
    if 0 <= index < 16:
        return f"c{index}"
    if 16 <= index < 232:
        index -= 16
        levels = (0, 95, 135, 175, 215, 255)
        red, green, blue = levels[index // 36], levels[index // 6 % 6], levels[index % 6]
        return f"#{red:02x}{green:02x}{blue:02x}"
    if 232 <= index < 256:
        grey = 8 + (index - 232) * 10
        return f"#{grey:02x}{grey:02x}{grey:02x}"
    return None


def _extended_color(params: list[int], at: int) -> tuple[str | None, int]:
    """Parse the arguments of a 38/48 code; return (colour, params consumed)."""
    if at + 1 < len(params) and params[at + 1] == 5 and at + 2 < len(params):
        return _indexed_color(params[at + 2]), 3
    if at + 1 < len(params) and params[at + 1] == 2 and at + 4 < len(params):
        red, green, blue = params[at + 2 : at + 5]
        if all(0 <= value <= 255 for value in (red, green, blue)):
            return f"#{red:02x}{green:02x}{blue:02x}", 5
        return None, 5
    return None, 1


def _apply_sgr(style: _Style, raw_params: str) -> None:
    try:
        params = [int(p) if p else 0 for p in raw_params.replace(":", ";").split(";")]
    except ValueError:
        return
    if not params:
        params = [0]
    at = 0
    while at < len(params):
        code = params[at]
        consumed = 1
        if code == 0:
            style.reset()
        elif code == 1:
            style.bold = True
        elif code == 2:
            style.dim = True
        elif code == 3:
            style.italic = True
        elif code == 4:
            style.underline = True
        elif code == 22:
            style.bold = style.dim = False
        elif code == 23:
            style.italic = False
        elif code == 24:
            style.underline = False
        elif 30 <= code <= 37:
            style.fg = f"c{code - 30}"
        elif 90 <= code <= 97:
            style.fg = f"c{code - 90 + _STANDARD_COLORS}"
        elif code == 39:
            style.fg = None
        elif 40 <= code <= 47:
            style.bg = f"c{code - 40}"
        elif 100 <= code <= 107:
            style.bg = f"c{code - 100 + _STANDARD_COLORS}"
        elif code == 49:
            style.bg = None
        elif code in (38, 48):
            color, consumed = _extended_color(params, at)
            if code == 38:
                style.fg = color
            else:
                style.bg = color
        at += consumed


def _open_span(style: _Style) -> str:
    classes: list[str] = []
    inline: list[str] = []
    for prefix, value in (("fg", style.fg), ("bg", style.bg)):
        if not value:
            continue
        if value.startswith("#"):
            inline.append(f"{'color' if prefix == 'fg' else 'background-color'}:{value}")
        else:
            classes.append(f"ansi-{prefix}-{value[1:]}")
    if style.bold:
        classes.append("ansi-bold")
    if style.dim:
        classes.append("ansi-dim")
    if style.italic:
        classes.append("ansi-italic")
    if style.underline:
        classes.append("ansi-underline")
    attributes = f' class="{" ".join(classes)}"' if classes else ""
    if inline:
        attributes += f' style="{";".join(inline)}"'
    return f"<span{attributes}>"


def _strip_controls(text: str) -> str:
    text = _CSI.sub("", text)
    text = _OTHER_ESCAPE.sub("", text)
    return _CONTROL.sub("", text)


def _ansi_to_html(content: str) -> str:
    """Escape ``content`` and turn its SGR codes into styled spans."""
    # A carriage return rewinds the line, so the last visible segment wins
    # (progress bars redraw in place).
    segments = content.split("\r")
    visible = [s for s in segments if _strip_controls(s).strip()]
    content = visible[-1] if visible else segments[-1]

    style = _Style()
    out: list[str] = []
    position = 0

    def emit(text: str) -> None:
        text = _OTHER_ESCAPE.sub("", text)
        text = _CONTROL.sub("", text)
        if not text:
            return
        escaped = html.escape(text, quote=False)
        out.append(
            escaped if style.is_plain() else f"{_open_span(style)}{escaped}</span>"
        )

    for match in _CSI.finditer(content):
        emit(content[position : match.start()])
        if match.group(2) == "m":
            _apply_sgr(style, match.group(1))
        position = match.end()
    emit(content[position:])
    return "".join(out)


def _parse_entries(text: str) -> list[dict]:
    """Group raw trace lines into logical lines, joining ``+`` continuations."""
    entries: list[dict] = []
    lines = text.split("\n")
    if lines[-1] == "":
        lines.pop()  # the final newline ends the last line, it does not add one
    for raw in lines:
        raw = raw.rstrip("\r")
        match = _STAMPED.match(raw)
        if match:
            timestamp, stream_id, stream, append, content = match.groups()
            if append == "+" and entries:
                entries[-1]["content"] += content
            else:
                prefix = f"{timestamp} {stream_id}{stream}{append}"
                entries.append({"prefix": prefix, "content": content})
            continue
        # A line with no prefix that holds only control sequences is the tail
        # of a marker written as "\r ESC[0K"; it is not a line of output. Keep
        # the carriage return as a boundary so a marker that is continued on
        # the next chunk does not fuse with it ("...executor" + "section_...").
        if raw and not _strip_controls(raw).strip() and "\x1b" in raw:
            if entries:
                entries[-1]["content"] += "\r"
            continue
        entries.append({"prefix": "", "content": raw})
    return entries


def render_trace(text: str | None) -> Markup:
    """Return the trace as safe HTML; an empty trace renders as nothing."""
    if not text:
        return Markup("")
    lines: list[str] = []
    for entry in _parse_entries(text):
        content = entry["content"]
        remainder = _SECTION.sub("", content)
        if remainder != content and not _strip_controls(remainder).strip():
            continue  # a section marker on its own line is not output
        prefix = html.escape(entry["prefix"], quote=False)
        lines.append(f"{prefix}{_ansi_to_html(remainder)}")
    return Markup("\n".join(lines))
