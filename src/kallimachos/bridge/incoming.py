"""``_Incoming/`` identification helpers: identities from file name and first pages,
and the title check that must pass before a user-dropped PDF is placed.

Identities are record keys (``doi:``, ``arxiv:`` without version, ``eprint:``) so that they can be
intersected with ``CatalogEntry.keys`` / ``UnreachableEntry.keys`` directly.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from collections.abc import Iterable
from pathlib import Path

from kallimachos.records.normalize import match_key

_DOI_RE = re.compile(r"\b(10\.\d{4,9}/[^\s\"<>]+)", re.IGNORECASE)
_ARXIV_NEW_RE = re.compile(r"(?<![\d.])(\d{4}\.\d{4,5})(?:v\d+)?(?![\d])")
_ARXIV_OLD_RE = re.compile(r"\b([a-z][a-z-]*(?:\.[A-Z]{2})?/\d{7})(?:v\d+)?\b")
_ARXIV_LABEL_RE = re.compile(r"arXiv:\s*(\d{4}\.\d{4,5}|[a-z][a-z-]*(?:\.[A-Z]{2})?/\d{7})", re.I)
_EPRINT_TEXT_RE = re.compile(r"\beprint\.iacr\.org/(\d{4})/(\d{3,5})\b", re.IGNORECASE)
_DOI_FILENAME_RE = re.compile(r"^(10\.\d{4,9})[_@](.+)$")
_EPRINT_FILENAME_RE = re.compile(r"^(\d{4})-(\d{3,5})$")
_COPY_SUFFIX_RE = re.compile(r"(?:[ _-]\(\d+\)|-\d|\s+copy(?:\s+\d+)?)$", re.IGNORECASE)
_DOI_TRAILING = ".,;)]}>"

#: arXiv's own DOI prefix: such DOIs are arXiv ids.
_ARXIV_DOI_PREFIX = "10.48550/arxiv."


def _doi_key(raw: str) -> str:
    doi = raw.strip().rstrip(_DOI_TRAILING).lower()
    if doi.startswith(_ARXIV_DOI_PREFIX):
        return "arxiv:" + doi[len(_ARXIV_DOI_PREFIX) :]
    return "doi:" + doi


def identities_from_text(text: str) -> set[str]:
    flat = re.sub(r"-\n", "", text)
    keys = {_doi_key(m.group(1)) for m in _DOI_RE.finditer(flat)}
    keys |= {"arxiv:" + m.group(1) for m in _ARXIV_LABEL_RE.finditer(flat)}
    keys |= {f"eprint:{m.group(1)}/{m.group(2)}" for m in _EPRINT_TEXT_RE.finditer(flat)}
    return keys


def identities_from_filename(name: str) -> set[str]:
    """Record keys hidden in a file name: the archive's publisher patterns
    (``data/publisher_filename_patterns.json`` through ``archive.existing.filename_idents``) plus
    the bridge's own ``10.0000_x`` / ``YYYY-NNN`` spellings and copy suffixes."""
    from kallimachos.archive.existing import filename_idents

    stem = re.sub(r"(\.pdf)+$", "", Path(name).name, flags=re.IGNORECASE)
    keys: set[str] = set()
    for candidate in {stem, _COPY_SUFFIX_RE.sub("", stem)}:
        found = filename_idents(candidate + ".pdf")
        if found.get("doi"):
            keys.add(_doi_key(found["doi"]))
        if found.get("arxiv"):
            keys.add("arxiv:" + found["arxiv"])
        m = _DOI_FILENAME_RE.match(candidate)
        if m:
            keys.add(_doi_key(f"{m.group(1)}/{m.group(2).replace('@', '/')}"))
        keys |= {"arxiv:" + a for a in _ARXIV_NEW_RE.findall(candidate)}
        keys |= {"arxiv:" + a for a in _ARXIV_OLD_RE.findall(candidate.replace("_", "/"))}
        e = _EPRINT_FILENAME_RE.match(candidate)
        if e:
            keys.add(f"eprint:{e.group(1)}/{e.group(2)}")
    return keys


def keys_intersect(entry_keys: Iterable[str], found: set[str]) -> bool:
    """Version-less comparison for arXiv keys; everything else exact (keys are lower-case)."""
    normal = {_strip_version(k.lower()) for k in entry_keys}
    return bool(normal & {_strip_version(k) for k in found})


def _strip_version(key: str) -> str:
    if key.startswith("arxiv:"):
        return re.sub(r"v\d+$", "", key)
    return key


def title_matches(title: str, text: str) -> bool:
    """The catalog title appears in the first pages: the first five title tokens in sequence, or
    >= 85% of the tokens of a title with >= 4 tokens, or (letter-spaced headings) the title's
    letters run together inside the text's letters."""
    tokens = match_key(title).split()
    if not tokens:
        return False
    hay_tokens = match_key(re.sub(r"-\n", "", text)).split()
    hay = " " + " ".join(hay_tokens) + " "
    if " " + " ".join(tokens[:5]) + " " in hay:
        return True
    present = set(hay_tokens)
    if len(tokens) >= 4 and sum(t in present for t in tokens) / len(tokens) >= 0.85:
        return True
    joined = "".join(tokens)
    return len(joined) >= 20 and joined in "".join(hay_tokens)


def pdftotext_first_pages(path: Path) -> str:
    """Text of pages 1-2 through poppler's ``pdftotext`` when installed, else ``""``."""
    exe = shutil.which("pdftotext")
    if exe is None:
        return ""
    try:
        proc = subprocess.run(
            [exe, "-f", "1", "-l", "2", "-enc", "UTF-8", str(path), "-"],
            capture_output=True,
            timeout=60,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return proc.stdout.decode("utf-8", "replace")
