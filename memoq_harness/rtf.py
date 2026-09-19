"""Minimal RTF -> plain text extraction.

The operator authors the rulebook, the vibe and the prompt templates in an RTF
editor. Rather than take a dependency for a one-shot import, this module
implements just enough of the RTF spec to recover the text those files carry:

* control words and groups, including nested ones;
* ``\\'hh`` byte escapes decoded through the document's ``\\ansicpg`` codepage,
  which is how Cyrillic survives a Word/WordPad save;
* ``\\uN`` unicode escapes with the ``\\ucN`` skip count honoured, which is how
  Cyrillic survives a TextEdit/LibreOffice save;
* skipped binary/metadata destinations (font tables, colour tables, stylesheets,
  embedded pictures) that would otherwise pour hex noise into the output.

It is a *reader*, not a writer or a round-tripper.
"""

from __future__ import annotations

import re
from typing import List, Optional

# Destinations whose entire contents are metadata, never body text.
_SKIP_DESTINATIONS = frozenset("""
    author buptim colorschememapping colortbl comment company creatim datastore
    do doccomm docvar fonttbl footer footerf footerl footerr footnote
    formfield ftncn ftnsep ftnsepc g generator header headerf headerl headerr
    info keywords latentstyles listoverridetable listtable mmathPr manager
    nonshppict objalias objclass objdata objname objsect objtime operator
    panose pict printim private propname protend protstart revtbl rsidtbl
    shpinst shppict stylesheet subject template themedata title xmlnstbl
    xmlopen
""".split())

# Control words that stand for a literal character.
_SPECIAL_CHARS = {
    "par": "\n",
    "sect": "\n",
    "page": "\n",
    "line": "\n",
    "softline": "\n",
    "tab": "\t",
    "emdash": "\u2014",
    "endash": "\u2013",
    "emspace": " ",
    "enspace": " ",
    "qmspace": " ",
    "bullet": "\u2022",
    "lquote": "\u2018",
    "rquote": "\u2019",
    "ldblquote": "\u201c",
    "rdblquote": "\u201d",
    "ltrmark": "\u200e",
    "rtlmark": "\u200f",
    "zwnj": "\u200c",
    "zwj": "\u200d",
}

_TOKEN_RE = re.compile(
    r"\\([a-zA-Z]{1,32})(-?\d{1,10})?[ ]?"   # 1,2 control word + optional arg
    r"|\\'([0-9a-fA-F]{2})"                  # 3   hex byte escape
    r"|\\(?:\r\n|\r|\n)"                     #     backslash-newline = \par
    r"|\\([\\{}~_\-*:|])"                    # 4   escaped literal
    r"|([{}])"                               # 5   group open/close
    r"|(\r\n|\r|\n)"                         # 6   raw newline (not text)
    r"|(.)",                                 # 7   plain character
    re.DOTALL,
)

# ``\`` immediately followed by a line break is a paragraph mark. macOS
# TextEdit writes paragraphs this way instead of ``\par``; treating it as an
# escaped literal turns every break into a stray backslash.
_ESCAPED_NEWLINE_RE = re.compile(r"\\(?:\r\n|\r|\n)")

_CODEPAGES = {
    "ansicpg1251": "cp1251",
    "ansicpg1252": "cp1252",
    "ansicpg1250": "cp1250",
    "ansicpg65001": "utf-8",
    "ansicpg10000": "mac_roman",
}


class _GroupState:
    __slots__ = ("uc", "ignore")

    def __init__(self, uc: int = 1, ignore: bool = False):
        self.uc = uc
        self.ignore = ignore

    def copy(self) -> "_GroupState":
        return _GroupState(self.uc, self.ignore)


def looks_like_rtf(data: bytes | str) -> bool:
    head = data[:16] if isinstance(data, bytes) else data[:16].encode("latin-1", "ignore")
    return head.lstrip().startswith(b"{\\rtf")


def rtf_to_text(data: bytes | str, default_encoding: str = "cp1251") -> str:
    """Extract readable text from an RTF document.

    ``default_encoding`` is used for ``\\'hh`` escapes when the file declares no
    ``\\ansicpg``; cp1251 is the sensible default for Russian-language sources.
    """
    if isinstance(data, bytes):
        # RTF is 7-bit ASCII at the transport level; non-ASCII bytes are only
        # meaningful via escapes, so latin-1 preserves them positionally.
        text = data.decode("latin-1")
    else:
        text = data

    if not looks_like_rtf(text):
        # Not RTF at all - hand it back unchanged rather than mangling it.
        return text if isinstance(data, str) else data.decode("utf-8", "replace")

    encoding = default_encoding
    cpmatch = re.search(r"\\(ansicpg\d+)", text)
    if cpmatch:
        encoding = _CODEPAGES.get(cpmatch.group(1), default_encoding)

    out: List[str] = []
    pending_bytes = bytearray()
    stack: List[_GroupState] = []
    state = _GroupState()
    skip_chars = 0          # characters to swallow after a \uN escape
    awaiting_destination = False

    def flush_bytes() -> None:
        if pending_bytes:
            out.append(bytes(pending_bytes).decode(encoding, errors="replace"))
            pending_bytes.clear()

    for match in _TOKEN_RE.finditer(text):
        word, arg, hexbyte, literal, brace, _newline, char = match.groups()

        # ``\`` + line break: a paragraph mark with no capture group of its own.
        if match.group(0) and _ESCAPED_NEWLINE_RE.fullmatch(match.group(0)):
            skip_chars = 0
            if not state.ignore:
                flush_bytes()
                out.append("\n")
            continue

        if brace:
            flush_bytes()
            if brace == "{":
                stack.append(state.copy())
                state = state.copy()
                awaiting_destination = False
            else:
                state = stack.pop() if stack else _GroupState()
                skip_chars = 0
            continue

        if word is not None:
            # A control word ends any pending \uN substitution run.
            if word != "u":
                skip_chars = 0

            if word == "*":
                awaiting_destination = True
                continue

            if awaiting_destination:
                awaiting_destination = False
                # {\*\foo ...} - an optional destination we do not understand.
                if word not in ("shppict",):
                    state.ignore = True
                    continue

            if word in _SKIP_DESTINATIONS:
                state.ignore = True
                continue

            if state.ignore:
                continue

            if word == "uc":
                state.uc = max(0, int(arg or 0))
                continue

            if word == "u":
                flush_bytes()
                code = int(arg or 0)
                if code < 0:
                    code += 65536
                if 0 <= code <= 0x10FFFF:
                    out.append(chr(code))
                skip_chars = state.uc
                continue

            if word in _SPECIAL_CHARS:
                flush_bytes()
                out.append(_SPECIAL_CHARS[word])
                continue

            # Any other control word is formatting; it carries no text.
            continue

        if state.ignore:
            continue

        if hexbyte is not None:
            if skip_chars > 0:
                skip_chars -= 1
                continue
            pending_bytes.append(int(hexbyte, 16))
            continue

        if literal is not None:
            # ``\*`` marks the *next* control word as an optional destination.
            # It is never literal text; emitting it leaks "*" plus whatever the
            # unrecognised destination contained.
            if literal == "*":
                awaiting_destination = True
                continue
            if skip_chars > 0:
                skip_chars -= 1
                continue
            if state.ignore:
                continue
            flush_bytes()
            out.append(" " if literal == "~" else ("-" if literal == "_" else literal))
            continue

        if char is not None:
            if skip_chars > 0:
                skip_chars -= 1
                continue
            flush_bytes()
            out.append(char)

    flush_bytes()
    return _tidy("".join(out))


def _tidy(text: str) -> str:
    """Normalise whitespace an RTF editor leaves behind.

    Deliberately conservative: it only touches line endings, trailing spaces and
    runs of three or more blank lines. Punctuation and letters are never
    rewritten - the model is responsible for those, not this importer.
    """
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = "\n".join(line.rstrip() for line in text.split("\n"))
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip("\n") + "\n"


def read_rtf_file(path: str) -> str:
    with open(path, "rb") as handle:
        return rtf_to_text(handle.read())


def find_rtf(directory: str, *name_fragments: str) -> Optional[str]:
    """First ``.rtf`` in ``directory`` whose name contains all fragments."""
    import os

    try:
        entries = sorted(os.listdir(directory))
    except OSError:
        return None
    lowered = [f.lower() for f in name_fragments]
    for name in entries:
        if not name.lower().endswith(".rtf"):
            continue
        stem = name.lower()
        if all(fragment in stem for fragment in lowered):
            return os.path.join(directory, name)
    return None
