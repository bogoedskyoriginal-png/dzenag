"""Deterministic evidence matching; never fuzzy-match numbers, names or negation."""
import hashlib
import re
import unicodedata

from markdown_it import MarkdownIt

MARKDOWN = MarkdownIt("commonmark", {"html": False})


def plain(text):
    text = unicodedata.normalize("NFKC", text).casefold().replace("ё", "е")
    parts = []
    for token in MARKDOWN.parse(text):
        if token.type == "inline":
            parts.append("".join(" " if child.type in ("softbreak", "hardbreak") else child.content
                                 for child in token.children or [] if child.type in ("text", "code_inline", "softbreak", "hardbreak")))
        elif token.type in ("code_block", "fence"):
            parts.append(token.content)
    text = " ".join(parts)
    text = text.translate(str.maketrans({"«": '"', "»": '"', "“": '"', "”": '"', "’": "'", "–": "-", "—": "-"}))
    return re.sub(r"\s+", " ", text).strip()


def locate(body, anchor, *, contextual_year=True):
    needle = plain(anchor)
    if not needle:
        return None
    matches = []
    for index, block in enumerate(re.split(r"\n\s*\n", body)):
        haystack = plain(block)
        kind = "normalized"
        if needle not in haystack and contextual_year:
            # Resolve only an explicit grammatical reference to a single preceding year.
            # This changes the comparison view, never the original article.
            def resolve(match):
                years = set(re.findall(r"\b(?:18|19|20)\d{2}\b", haystack[:match.start()]))
                return next(iter(years)) + " года" if len(years) == 1 else match[0]
            haystack = re.sub(r"\bтого же года\b", resolve, haystack)
            kind = "explicit_context_year"
        if needle in haystack:
            matches.append({"block_index": index, "kind": kind,
                            "block_hash": hashlib.sha256(block.encode()).hexdigest(), "anchor": anchor})
    # Repeating the same supported phrase is not a different factual interpretation.
    return {**matches[0], "matching_blocks": len(matches)} if matches else None
