"""Turns a job posting's text into something easy to read: headings, paragraphs and bullet lists.

Nothing here rewords anything. Pasted text is laid out as it is. A tidied copy from Gemini is only used if its
words come from the posting itself (see faithful), so it can drop page clutter but not change what the job says."""
import json
import re
import unicodedata

MAX_HEADING_WORDS = 8
_INVISIBLE = dict.fromkeys(map(ord, "​‌‍⁠﻿­️"))
_NUMBERED = re.compile(r"^(\d{1,2}|[a-z])[.)]\s+", re.I)
_MARKS = "•·‣⁃-*–—▪●◦→⇒➔"  # besides emoji and other pictures, which are all "other symbols" in Unicode
_MARKDOWN_HEADING = re.compile(r"^#{1,6}\s+")
_SENTENCE_END = re.compile(r"[.!?:;)\]]$")
_SECTION_STARTS = (
    "Page title: ", "Structured job data:\n", "Email links on this page:\n", "Job description from the page:\n",
)
# Headings job adverts use. Descriptions saved on one line get them back, with what follows as a list.
_LISTED = (
    r"what you'?ll do|what you will do|what you'?ll be doing|responsibilities|key responsibilities|your responsibilities"
    r"|requirements|qualifications|preferred qualifications|what we'?re looking for|what we are looking for"
    r"|who you are|about you|skills|nice to have|nice-to-have|bonus points|benefits|perks|what we offer|why join us"
)
_PLAIN = r"about the role|about the job|about us|the role|how to apply"
_ONE_LINE_HEADING = re.compile(rf"(?:^|(?<=[.!?] ))({_LISTED}|{_PLAIN})(?::)?\s+(?=[A-Z0-9])", re.I)


def normalize(text):
    """Plain letters for the fancy bold and italic ones LinkedIn posts use, and no invisible characters."""
    return unicodedata.normalize("NFKC", (text or "").translate(_INVISIBLE)).replace("\r\n", "\n").replace("\r", "\n")


def _fancy(line):
    """Most of the line's letters were written in a decorative alphabet, as LinkedIn posts do for headings."""
    letters = [char for char in line if char.isalpha()]
    return len(letters) >= 3 and sum("\U0001d400" <= char <= "\U0001d7ff" for char in letters) / len(letters) >= 0.6


def _bullet(line):
    """The item's text if the line starts with a mark rather than a word: "✅ Python", "• Python", "1. Python"."""
    numbered = _NUMBERED.match(line)
    if numbered:
        return line[numbered.end():].strip()
    first = line[:1]
    picture = bool(first) and unicodedata.category(first) == "So"  # ✅ 📍 ➤ ▶, but not $ or + or ^
    if picture or (first and first in _MARKS and line[1:2].isspace()):
        rest = line[1:].strip()
        while rest[:1] and unicodedata.category(rest[0]) == "So":  # ✅ ✅ or 📍📍
            rest = rest[1:].strip()
        return rest or None
    return None


def _heading(line, fancy):
    text = line.strip()
    if _MARKDOWN_HEADING.match(text):
        return _MARKDOWN_HEADING.sub("", text)
    if text.startswith("**") and text.endswith("**") and len(text) > 4:
        text = text.strip("*").strip()
        return text if len(text.split()) <= MAX_HEADING_WORDS * 2 else None
    words = text.split()
    if fancy and len(words) <= MAX_HEADING_WORDS * 2:
        return text.rstrip(":").strip()
    if len(words) <= MAX_HEADING_WORDS and text.endswith(":") and not re.search(r"[.!?]", text[:-1]):
        return text[:-1].strip()
    letters = [char for char in text if char.isalpha()]
    if 1 <= len(words) <= MAX_HEADING_WORDS and len(letters) >= 4 and all(char.isupper() for char in letters):
        return text.rstrip(":").strip()
    return None


def blocks(text):
    """The text as a list of {"kind": "heading" | "paragraph" | "list", "text": ..., "items": [...]}."""
    found = []
    for raw in (text or "").replace("\r\n", "\n").split("\n"):
        fancy = _fancy(raw)
        line = " ".join(normalize(raw).split())
        if not line:
            if found and found[-1]["kind"] == "paragraph":
                found[-1]["open"] = False  # a blank line ends the paragraph
            continue
        item = _bullet(line)
        heading = None if item is not None else _heading(line, fancy)
        if item:
            if found and found[-1]["kind"] == "list":
                found[-1]["items"].append(item)
            else:
                found.append({"kind": "list", "items": [item]})
        elif heading:
            found.append({"kind": "heading", "text": heading})
        else:
            last = found[-1] if found else None
            wrapped = last and last["kind"] == "paragraph" and last.get("open") and not _SENTENCE_END.search(last["text"])
            if wrapped and line[:1].islower():  # a line broken in the middle of a sentence
                last["text"] = f"{last['text']} {line}"
            else:
                found.append({"kind": "paragraph", "text": line, "open": True})
    for block in found:
        block.pop("open", None)
    return found


def markup(gemini_blocks):
    """Gemini's tidied description as stored text: "## " headings, "- " items and plain paragraphs."""
    lines = []
    for block in gemini_blocks if isinstance(gemini_blocks, list) else []:
        if not isinstance(block, dict):
            continue
        text = " ".join(str(block.get("text") or "").split())
        kind = str(block.get("kind") or "").lower()
        if not text:
            continue
        if kind == "heading":
            lines += ["", f"## {text.rstrip(':')}"]
        elif kind == "bullet":
            lines.append(f"- {text}")
        else:
            lines += ["", text]
    return "\n".join(lines).strip()


def _words(text):
    return re.findall(r"\w+", normalize(text).lower())


def faithful(tidied, original, share=0.85):
    """Whether a tidied description keeps to the posting's own words: almost every run of three words in it must
    appear in the original, and it can't have dropped most of the original's sentences (menus and buttons aside)."""
    source, mine = _words(original), _words(tidied)
    sentences = sum(len(words) for words in map(_words, (original or "").splitlines()) if len(words) >= 6)
    if len(mine) < min(40, sentences * 0.3):
        return False
    known = {tuple(source[i:i + 3]) for i in range(len(source) - 2)}
    runs = [tuple(mine[i:i + 3]) for i in range(len(mine) - 2)]
    return bool(runs) and sum(run in known for run in runs) / len(runs) >= share


_UI_WORDS = re.compile(
    r"^(home|jobs?|careers?|about( us)?|contact( us)?|blog|news|menu|search|log ?in|sign ?(in|up)|register|privacy"
    r"( policy)?|terms( of (use|service))?|cookies?( policy| settings)?|accept( all)?|reject( all)?|share|save( job)?"
    r"|apply( now)?|back|next|previous|more|close|skip to (main )?content|follow us|all rights reserved)\W*$",
    re.I,
)


def declutter(text):
    """A page's text without its menus and buttons: short lines that repeat, or are the usual site words."""
    lines = (text or "").splitlines()
    counts = {}
    for line in lines:
        key = " ".join(line.split()).lower()
        counts[key] = counts.get(key, 0) + 1
    kept = []
    for line in lines:
        key = " ".join(line.split()).lower()
        short = len(key.split()) <= 3 and not key.endswith(":")
        if not (short and (counts[key] > 1 or _UI_WORDS.match(key))):
            kept.append(line)
    return "\n".join(kept)


def noisy(text):
    """Text copied from a whole web page, not just the advert: mostly short lines such as menus and buttons."""
    lines = [line for line in (text or "").splitlines() if line.strip()]
    short = sum(len(line.split()) <= 3 and not line.rstrip().endswith(":") for line in lines)
    return len(lines) >= 30 and short / len(lines) >= 0.4


def page_parts(text):
    """What the crawler stored for a page: its title, the job data the page published, and the rest of its text."""
    title, postings, rest, own = "", [], [], ""
    for section in (text or "").split("\n\n"):
        if section.startswith(_SECTION_STARTS[0]):
            title = section[len(_SECTION_STARTS[0]):].strip()
        elif section.startswith(_SECTION_STARTS[3]):
            own = section[len(_SECTION_STARTS[3]):].strip()
        elif section.startswith(_SECTION_STARTS[1]):
            try:
                postings = json.loads(section[len(_SECTION_STARTS[1]):])
            except ValueError:
                rest.append(section)
        elif not section.startswith(_SECTION_STARTS[2]):
            rest.append(section)
    postings = [item for item in postings if isinstance(item, dict)] if isinstance(postings, list) else []
    if own and len(postings) == 1:
        postings[0]["description"] = own
    return title, postings, "\n\n".join(rest)


def unflatten(description):
    """A description saved on one line, as the crawler once stored them, laid out again: its usual section names
    become headings, and the sentences under list-like ones become items. No word changes."""
    if "\n" in description or not _ONE_LINE_HEADING.search(description):
        return description
    lines = []
    parts = _ONE_LINE_HEADING.split(description)
    if parts[0].strip():
        lines.append(parts[0].strip())
    for heading, body in zip(parts[1::2], parts[2::2]):
        lines.append(f"{heading.strip()}:")
        sentences = [part.strip() for part in re.split(r"(?<=[.!?])\s+(?=[A-Z0-9])", body) if part.strip()]
        listed = re.fullmatch(_LISTED, heading.strip(), re.I)
        lines += [f"- {sentence}" if listed else sentence for sentence in sentences]
    return "\n".join(lines)


def facts(posting):
    """Short facts from a page's job data: company, place, type of work, salary and dates."""
    def name(value):
        return value.get("name", "") if isinstance(value, dict) else value if isinstance(value, str) else ""

    def place(value):
        values = value if isinstance(value, list) else [value]
        found = []
        for item in values:
            address = item.get("address", {}) if isinstance(item, dict) else {}
            if isinstance(address, dict):
                parts = [address.get(key) for key in ("addressLocality", "addressRegion", "addressCountry")]
                found.append(", ".join(str(part) for part in parts if isinstance(part, str) and part))
        return "; ".join(part for part in found if part)

    found = [name(posting.get("hiringOrganization")), place(posting.get("jobLocation"))]
    kind = posting.get("employmentType")
    found.append(", ".join(kind) if isinstance(kind, list) else kind if isinstance(kind, str) else "")
    if posting.get("jobLocationType") == "TELECOMMUTE":
        found.append("Remote")
    for key, label in (("datePosted", "Posted"), ("validThrough", "Closes")):
        if isinstance(posting.get(key), str) and posting[key]:
            found.append(f"{label} {posting[key][:10]}")
    return [" ".join(str(fact).replace("_", " ").split()) for fact in found if fact]


def readable(app):
    """The job description to show for an application: Gemini's tidied copy if there is one, else the page's own
    job data for a crawled page, else the text as it is. Returns (title, facts, blocks)."""
    if app.description:
        return "", [], blocks(app.description)
    if app.source != "crawl":
        return "", [], blocks(declutter(app.jd_text) if noisy(app.jd_text) else app.jd_text)
    title, postings, rest = page_parts(app.jd_text)
    posting = next((item for item in postings if isinstance(item.get("description"), str) and item["description"]), None)
    if posting:
        description = unflatten(posting["description"])
        return posting.get("title") if isinstance(posting.get("title"), str) else title, facts(posting), blocks(description)
    return title, [], blocks(declutter(rest))
