"""Reads the text out of an uploaded CV."""
import io
import re
from pathlib import Path
from urllib.parse import unquote

from docx import Document
from docx.opc.constants import RELATIONSHIP_TYPE
from docx.oxml.ns import qn
from docx.table import Table
from docx.text.paragraph import Paragraph
from pypdf import PdfReader

from . import countries, emails

MAX_BYTES = 10 * 1024 * 1024
MIN_TEXT = 200  # fewer characters than this is not a usable CV
MAX_LINKS = 200
MAX_CV_LINKS = 20  # links kept for a CV, besides LinkedIn
HEADER_LINES = 12
MIMETYPES = {
    ".pdf": "application/pdf",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
}
_W_P, _W_T, _W_TBL = qn("w:p"), qn("w:t"), qn("w:tbl")
_NOT_A_NAME = re.compile(
    r"curriculum|vitae|r[eé]sum[eé]|\bcv\b|profile|summary|statement|objective|experience|skills"
    r"|education|employment|contact|details|developer|engineer|manager|analyst|consultant|designer|kingdom",
    re.I,
)
_LINKEDIN = re.compile(r"(?:https?://)?(?:[a-z]{2,3}\.)?linkedin\.com/in/[\w%-]+/?", re.I)
_PHONE = re.compile(r"(?<![\d/.-])\(?(?:\+\d{1,3}|0)[\d ()-]{8,16}\d(?![\d/.])")
# North America writes numbers without + or 0: (650) 555-0123, 650-555-0123, 1-650-555-0123.
_NANP_PHONE = re.compile(r"(?<![\d/.+-])(?:1[ .-])?\(?[2-9]\d{2}\)?[ .-]?[2-9]\d{2}[ .-]\d{4}(?![\d/.])")
# Where the contact details end: the first heading of the CV's body.
_SECTION = re.compile(
    r"^\W*(profile|summary|professional summary|personal profile|career summary|about|about me|objective"
    r"|career objective|personal statement|overview|introduction|work experience|professional experience"
    r"|relevant experience|employment|employment history|work history|career history|key skills|technical skills"
    r"|core skills|certifications|achievements|key achievements|publications|languages|interests|awards"
    r"|personal details|personal information|contact|contact details|contact information"
    r"|(?:experience|education|skills|projects)(?:\s*(?:&|and)\s*[\w ]{1,30})?)\W*$",
    re.I,
)
_SEPARATORS = re.compile(r"\s*(?:[|•·◦▪●∙✉☎📞📍]|\s[-–—]\s|\t|\s{3,})\s*")
_SOMEONE_ELSE = re.compile(
    r"\b(clients?|customers?|compan(y|ies)|teams?|users?|business(es)?|partners?|offices?|employers?|startups?|firms?"
    r"|brands?|agenc(y|ies)|organi[sz]ations?)\b[^.\n]*$",
    re.I,
)
_STATED_PLACE = re.compile(r"\b(?i:based|located|living|residing|resident) in (?:the )?([A-Z][^.,;:|()\n]{1,40})")
# Printed links: anything with http(s):// or www., and profile sites written without either: a profile path such as
# github.com/you, or your own subdomain such as you.github.io. A bare "x.com" in a sentence isn't a link of yours.
_PRINTED_LINK = re.compile(
    r"(?<![\w@./-])(?:(?:https?://|www\.)[^\s<>\"'|,;()]+"
    r"|(?:[a-z0-9-]+\.)+(?:github\.io|vercel\.app|netlify\.app|hashnode\.dev|substack\.com)(?:/[^\s<>\"'|,;()]*)?"
    r"|(?:github\.com|gitlab\.com|bitbucket\.org|kaggle\.com|huggingface\.co|medium\.com|dev\.to|stackoverflow\.com"
    r"|behance\.net|dribbble\.com|x\.com|twitter\.com|leetcode\.com)/[^\s<>\"'|,;()]+)",
    re.I,
)
_REFERENCES = re.compile(r"^\W*((?:professional|character|work|employment|personal)\s+)?(references?|referees?)\b.*$", re.I | re.M)
_GITHUB_PROFILE = re.compile(r"^https?://(?:www\.)?github\.com/([A-Za-z0-9](?:[A-Za-z0-9]|-(?=[A-Za-z0-9])){0,38})/?$", re.I)
# Header pieces that name a place without saying where you are: an employer, a degree, a job line or a wish.
_NOT_WHERE_YOU_ARE = re.compile(
    r"\b(at|university|college|institute|school|academy|ltd|limited|llc|inc|corp|corporation|company|technologies"
    r"|instruments|solutions|systems|labs|bank|group|bsc|msc|ba|ma|mba|phd|beng|meng|bs|ms|engineer|developer|manager"
    r"|analyst|designer|consultant|intern|scientist|open to|seeking|looking for|relocat\w*|roles?|opportunit\w*)\b",
    re.I,
)


class ResumeError(ValueError):
    """Message is shown to the user."""


def is_pdf(name):
    return Path(name or "").suffix.lower() == ".pdf"


def mimetype(name):
    return MIMETYPES.get(Path(name or "").suffix.lower(), "application/octet-stream")


def extract_text(name, data):
    suffix = Path(name or "").suffix.lower()
    if suffix not in MIMETYPES:
        raise ResumeError("Please upload your CV as a PDF or a Word .docx file.")
    if not data:
        raise ResumeError("That file is empty.")
    if len(data) > MAX_BYTES:
        raise ResumeError("That file is over 10 MB, which is too big to attach to an application.")
    text = _read_pdf(data) if suffix == ".pdf" else _read_docx(data)
    return re.sub(r"[ \t]+", " ", text.replace("\x00", "")).strip()


def _read_pdf(data):
    if b"%PDF" not in data[:1024]:
        raise ResumeError("That file isn't a valid PDF.")
    try:
        reader = PdfReader(io.BytesIO(data))
        if reader.is_encrypted and not reader.decrypt(""):
            raise ResumeError("That PDF is password-protected. Employers couldn't open it either.")
        return "\n".join(page.extract_text() or "" for page in reader.pages)
    except ResumeError:
        raise
    except Exception as exc:
        raise ResumeError("Couldn't read that PDF. Try exporting it again.") from exc


def _own_text(paragraph):
    """A paragraph's text, leaving out any paragraphs nested inside it, such as text boxes."""
    return "".join(t.text or "" for t in paragraph.iter(_W_T) if next(t.iterancestors(_W_P)) is paragraph)


def _read_docx(data):
    try:
        document = Document(io.BytesIO(data))
        # Page headers come first: CV templates often keep the name there.
        headers = [section.header._element for section in document.sections]
        lines = [_own_text(p) for root in headers for p in root.iter(_W_P)]
        for block in document.element.body.iterchildren():  # in page order: a contact table at the top comes first
            if block.tag == _W_P:
                lines.append(Paragraph(block, document._body).text)
            elif block.tag == _W_TBL:
                for row in Table(block, document._body).rows:
                    lines.append(" | ".join(cell.text.strip() for cell in row.cells))
        # Text boxes and nested tables aren't in any of the lists above.
        known = "\n".join(lines)
        for paragraph in document.element.body.iter(_W_P):
            text = _own_text(paragraph)
            if text.strip() and text not in known:
                known += "\n" + text
                lines.append(text)
    except Exception as exc:
        raise ResumeError("Couldn't read that Word file. Try saving it as a PDF.") from exc
    return "\n".join(line for line in lines if line.strip())


def letter_as_docx(text):
    """The tailored message as a file, for forms that only take a cover letter upload."""
    document = Document()
    for paragraph in text.split("\n\n"):
        document.add_paragraph(paragraph)
    buffer = io.BytesIO()
    document.save(buffer)
    return buffer.getvalue()


def guess_contact(text):
    """Best effort without the LLM. The user can correct all of it on the dashboard."""
    linkedin = _LINKEDIN.search(text)
    phone = next(_printed_phones(text), "")
    name = ""
    for line in [line.strip() for line in text.splitlines() if line.strip()][:5]:
        candidate = re.split(r"\s*[|•·,]\s*|\s+[-–—]\s+", line)[0]  # "Sam Taylor | Developer", "Sam Taylor, MBA"
        if re.fullmatch(r"[^\W\d_]+(?:[ '.-]+[^\W\d_]+){1,3}", candidate) and not _NOT_A_NAME.search(candidate):
            name = candidate
            break
    return {
        "full_name": name.title() if name.isupper() else name,
        "phone": phone,
        "linkedin": linkedin.group() if linkedin else "",
    }


def contact_details(name, data, text):
    """Who a CV says you are: name, email, phone, LinkedIn, other links and the country it's for, from what it
    prints and where its links go. A link only wins where the text backs it up, so a stale link left behind in a
    template doesn't, and nothing after a references heading counts, so a referee's details never become yours.
    "links" is one per line."""
    text = _REFERENCES.split(text or "", maxsplit=1)[0]
    found = guess_contact(text)
    targets = links(name, data)
    found["phone"] = _choose_phone(text, [number for number in map(_tel_number, targets) if number])
    found["linkedin"] = _choose_linkedin(text, [match.group() for match in map(_LINKEDIN.search, targets) if match])
    found["email"] = _choose_email(text, targets)
    found = {key: " ".join(value.split()) for key, value in found.items()}
    found["country"] = guess_country(text, found["phone"], found["full_name"])
    found["links"] = "\n".join(_web_links(text, targets))
    return found


def header(text, name=""):
    """The top of a CV, above its first heading, where it keeps contact details and location. Only short pieces count,
    and none of your name, email addresses, links or numbers, so "clients in the US" in a sentence doesn't."""
    pieces = []
    for line in [line.strip() for line in (text or "").splitlines() if line.strip()][:HEADER_LINES]:
        if _SECTION.match(line):
            break
        for piece in _SEPARATORS.split(line):
            for pattern in (_PHONE, _NANP_PHONE, emails.EMAIL_RE, _LINKEDIN, _PRINTED_LINK):
                piece = pattern.sub(" ", piece)
            piece = piece.strip(" ,;:")
            if name and piece.casefold().startswith(name.casefold()):
                piece = piece[len(name):].strip()  # what follows your name: "Jane Doe, MD" isn't Maryland, but a city is
            if piece and len(piece) <= 50 and len(piece.split()) <= 5 and not _NOT_WHERE_YOU_ARE.search(piece):
                pieces.append(piece)
    return "\n".join(pieces)


def guess_country(text, phone, name=""):
    """The country a CV is for: where its header says you are (3 votes), where its summary says you're based (2), and
    its phone number's country code (2, or 1 for a local format such as 07... in the UK). Ties go to the phone's
    country. Under 2 votes, it's left blank."""
    votes, order = {}, {}
    top = header(text, name)
    for position, country in countries.places(top, hints=False):
        votes[country] = 3
        order.setdefault(country, position)
    for position, country in countries.hinted(top):  # "Austin, TX": a hint the phone can outweigh
        votes.setdefault(country, 2)
        order.setdefault(country, position)
    for stated in _STATED_PLACE.finditer((text or "")[:1500]):
        if _SOMEONE_ELSE.search(text[max(0, stated.start() - 30): stated.start()]):
            continue  # "clients based in the UK" is about them, not you
        for _, country in countries.places(stated.group(1)):
            votes.setdefault(country, 2)
            order.setdefault(country, len(text) + stated.start())
        break
    by_phone, weight = countries.from_phone(phone)
    if by_phone:
        votes[by_phone] = votes.get(by_phone, 0) + weight
    if not votes or max(votes.values()) < 2:
        return ""
    best = max(votes.values())
    tied = [country for country, count in votes.items() if count == best]
    if by_phone in tied:
        return by_phone
    return min(tied, key=lambda country: order.get(country, len(text or "") + 1))


def github_profile(links_text):
    """The first GitHub profile link, such as https://github.com/you, not a repository's."""
    for link in (links_text or "").split():
        match = _GITHUB_PROFILE.match(link)
        if match:
            return f"https://github.com/{match.group(1)}"
    return ""


def clean_links(text):
    """Links one per line, each a full https:// address, without duplicates or LinkedIn (it has its own box)."""
    seen, kept = set(), []
    for link in (text or "").split():
        link = link.strip(".,;:)]}>\"'")
        if not link or "." not in link or len(link) > 300 or re.match(r"(mailto|tel|javascript|data|file):", link, re.I):
            continue
        link = link if re.match(r"https?://", link, re.I) else f"https://{link}"
        if _LINKEDIN.search(link) or _link_key(link) in seen:
            continue
        seen.add(_link_key(link))
        kept.append(link)
    return kept[:MAX_CV_LINKS]


def _link_key(link):
    return re.sub(r"^https?://(www\.)?|/+$", "", link.strip(), flags=re.I).lower()


def _web_links(text, targets):
    """Links the CV prints, and where its clickable links go: those in the header first, then the clickable ones,
    then any others the body mentions, so your own GitHub comes before a project you name. A printed one that a line
    break cut off gives way to the clickable one it starts."""
    web = [target for target in targets if re.match(r"https?://", target, re.I)]
    top = _header_end(text)
    used, first, last = set(), [], []
    for match in _PRINTED_LINK.finditer(text or ""):
        key = _link_key(match.group().rstrip(".,;:"))
        longer = next((target for target in web if _link_key(target).startswith(key) and target not in used), None)
        if longer:
            used.add(longer)
        (first if match.start() < top else last).append(longer or match.group())
    return clean_links("\n".join([*first, *(target for target in web if target not in used), *last]))


def _header_end(text):
    """Where the header ends in the text: at the first section heading, or after the first lines."""
    position, count = 0, 0
    for line in (text or "").splitlines(keepends=True):
        if line.strip():
            if _SECTION.match(line.strip()) or count >= HEADER_LINES:
                return position
            count += 1
        position += len(line)
    return position


def _choose_email(text, targets):
    """The address in the CV's header, else its first email link, else the first printed above any references,
    so a referee's address never stands in for yours."""
    lines = [line for line in (text or "").splitlines() if line.strip()]
    found = emails.all_addresses("\n".join(lines[:HEADER_LINES]))
    for target in targets:
        if not found and target[:7].lower() == "mailto:":
            found = emails.all_addresses(unquote(target[7:]).split("?")[0])
    if not found:
        found = emails.all_addresses(_REFERENCES.split(text or "", maxsplit=1)[0])
    return found[0] if found else ""


def _choose_phone(text, linked):
    """The number nearest the top that a phone link backs up, as printed if the print keeps its country code.
    Without one, the first printed number, or failing that the first phone link."""
    printed = list(_numbers(text))
    backed = []
    for link in linked:
        same = next(((start, number) for start, number in printed if _digits(number)[-9:] == _digits(link)[-9:]), None)
        if same:
            start, number = same
            international = number.lstrip("(").startswith(("+", "00"))
            backed.append((start, link if link.startswith("+") and not international else number))
            continue
        shown = re.search(r"[\s().-]*".join(_digits(link)[-9:]), text)  # printed in a way the reader misses
        if shown:
            backed.append((shown.start(), link))
    if backed:
        return min(backed)[1]
    return printed[0][1] if printed else linked[0] if linked else ""


def _choose_linkedin(text, linked):
    """The printed address, unless a line break cut it off and a link carries on exactly where it stops."""
    printed = _LINKEDIN.search(text)
    if not printed:
        return linked[0] if linked else ""
    slug = _slug(printed.group())
    after = re.sub(r"\s+", "", text[printed.end(): printed.end() + 200]).lower()
    for link in linked:
        rest = _slug(link)[len(slug):] if _slug(link).startswith(slug) else ""
        if rest and after.startswith(rest):
            return link
    return printed.group()


def _printed_phones(text):
    for _, number in _numbers(text):
        yield number


def _numbers(text):
    """(position, number) for each phone number printed, in order: international, national with a leading 0, or
    North American. The same digits found two ways count once."""
    found = sorted(
        ((match.start(), match.end(), match.group().strip()) for pattern in (_PHONE, _NANP_PHONE) for match in pattern.finditer(text or "")),
        key=lambda item: (item[0], -item[1]),
    )
    end = -1
    for start, stop, number in found:
        if start >= end and 10 <= len(_digits(number)) <= 15:
            end = stop
            yield start, number


def _digits(text):
    return re.sub(r"\D", "", text)


def _slug(linkedin):
    return re.sub(r"^.*?linkedin\.com/in/", "", linkedin, flags=re.I).strip("/").lower()


def links(name, data):
    """Where the file's links go, such as a LinkedIn icon or a phone number you can tap. Best effort."""
    try:
        if is_pdf(name):  # a file that doesn't start like a PDF has already been turned away
            found = _pdf_links(data) if re.search(rb"%PDF-\d", data[:1024]) else []
        else:
            found = _docx_links(data)
    except Exception:
        return []
    return [link.strip() for link in found if isinstance(link, str) and link.strip()][:MAX_LINKS]


def _pdf_links(data):
    reader = PdfReader(io.BytesIO(data))
    if reader.is_encrypted and not reader.decrypt(""):
        return []
    found = []
    for page in reader.pages[:10]:
        try:
            annotations = page["/Annots"] if "/Annots" in page else []
            for annotation in annotations:
                if len(found) >= MAX_LINKS:
                    return found
                try:
                    uri = annotation.get_object()["/A"]["/URI"]
                except Exception:  # not a web or phone link
                    continue
                found.append(uri.decode("utf-8", "ignore") if isinstance(uri, bytes) else str(uri))
        except Exception:  # a broken page doesn't lose the links already found
            continue
    return found


def _docx_links(data):
    document = Document(io.BytesIO(data))
    return [  # headers and footers have their own links, and CV templates often keep contact details there
        rel.target_ref
        for part in document.part.package.iter_parts()
        for rel in part.rels.values()
        if rel.reltype == RELATIONSHIP_TYPE.HYPERLINK and rel.is_external
    ]


def _tel_number(target):
    """The number behind a phone link. A long one without + or a leading 0 starts with its country code."""
    if target[:4].lower() != "tel:":
        return ""
    number = unquote(target[4:]).split(";")[0].strip().lstrip("/").strip()
    digits = len(_digits(number))
    if not (10 <= digits <= 15 and re.fullmatch(r"\+?\(?\d[\d ()./-]*", number)):
        return ""
    if digits >= 11 and not re.match(r"\+|\(?0", number):
        number = f"+{number}"
    return number[:40]
