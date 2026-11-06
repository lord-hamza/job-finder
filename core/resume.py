"""Reads the text out of an uploaded CV."""
import io
import re
from pathlib import Path

from docx import Document
from docx.oxml.ns import qn
from pypdf import PdfReader

MAX_BYTES = 10 * 1024 * 1024
MIN_TEXT = 200  # fewer characters than this is not a usable CV
MIMETYPES = {
    ".pdf": "application/pdf",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
}
_W_P, _W_T = qn("w:p"), qn("w:t")
_NOT_A_NAME = re.compile(
    r"curriculum|vitae|r[eé]sum[eé]|\bcv\b|profile|summary|statement|objective|experience|skills"
    r"|education|employment|contact|details|developer|engineer|manager|analyst|consultant|designer|kingdom",
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
        lines += [paragraph.text for paragraph in document.paragraphs]
        for table in document.tables:
            for row in table.rows:
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
    linkedin = re.search(r"(?:https?://)?(?:[a-z]{2,3}\.)?linkedin\.com/in/[\w%-]+/?", text, re.I)
    phone = ""
    for match in re.finditer(r"(?<![\d/.-])\(?(?:\+\d{1,3}|0)[\d ()-]{8,16}\d(?![\d/.])", text):
        if 10 <= sum(c.isdigit() for c in match.group()) <= 15:
            phone = match.group().strip()
            break
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
