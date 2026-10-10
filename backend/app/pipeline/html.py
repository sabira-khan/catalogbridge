from __future__ import annotations

import re
from html.parser import HTMLParser

BLOCK_TAGS = frozenset(
    {
        "address", "article", "blockquote", "br", "div", "h1", "h2", "h3", "h4", "h5", "h6",
        "hr", "li", "ol", "p", "section", "table", "td", "th", "tr", "ul",
    }
)
SKIP_TAGS = frozenset({"noscript", "script", "style"})
INLINE_WHITESPACE = re.compile(r"[ \t\r\f\v]+")
BLANK_LINES = re.compile(r"\n{3,}")


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in SKIP_TAGS:
            self._skip_depth += 1
        elif tag in BLOCK_TAGS:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in SKIP_TAGS and self._skip_depth > 0:
            self._skip_depth -= 1
        elif tag in BLOCK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self._skip_depth == 0:
            self.parts.append(data)


def html_to_text(raw: str | None) -> str | None:
    if not raw:
        return None
    extractor = _TextExtractor()
    extractor.feed(raw)
    extractor.close()
    text = INLINE_WHITESPACE.sub(" ", "".join(extractor.parts))
    text = "\n".join(line.strip() for line in text.splitlines())
    text = BLANK_LINES.sub("\n\n", text).strip()
    return text or None
