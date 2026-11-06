import io

from django.test import SimpleTestCase
from docx import Document
from docx.oxml import parse_xml
from pypdf import PdfReader, PdfWriter

from core import resume
from core.tests.base import minimal_pdf


def docx_bytes(*paragraphs, table=None):
    document = Document()
    for paragraph in paragraphs:
        document.add_paragraph(paragraph)
    if table:
        grid = document.add_table(rows=1, cols=len(table))
        for cell, value in zip(grid.rows[0].cells, table):
            cell.text = value
    buffer = io.BytesIO()
    document.save(buffer)
    return buffer.getvalue()


class ExtractTextTests(SimpleTestCase):
    def test_pdf(self):
        self.assertIn("Python developer", resume.extract_text("cv.pdf", minimal_pdf("Python developer")))

    def test_uppercase_extension(self):
        self.assertIn("Python", resume.extract_text("CV.PDF", minimal_pdf("Python")))

    def test_docx_paragraphs_and_tables(self):
        text = resume.extract_text("cv.docx", docx_bytes("Sam Taylor", table=["Skills", "Django"]))
        self.assertIn("Sam Taylor", text)
        self.assertIn("Skills | Django", text)

    def test_docx_page_header_text_box_and_nested_table(self):
        document = Document()
        document.sections[0].header.paragraphs[0].text = "Sam Taylor"
        document.add_paragraph("Experience")
        box = (
            '<w:p xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main" '
            'xmlns:v="urn:schemas-microsoft-com:vml"><w:r><w:pict><v:shape><v:textbox><w:txbxContent>'
            "<w:p><w:r><w:t>Sidebar skills: Django</w:t></w:r></w:p>"
            "</w:txbxContent></v:textbox></v:shape></w:pict></w:r></w:p>"
        )
        document.element.body.insert(0, parse_xml(box))
        cell = document.add_table(rows=1, cols=1).rows[0].cells[0]
        cell.add_table(rows=1, cols=1).rows[0].cells[0].text = "Nested award"
        buffer = io.BytesIO()
        document.save(buffer)
        text = resume.extract_text("cv.docx", buffer.getvalue())
        self.assertTrue(text.startswith("Sam Taylor"))
        self.assertEqual(text.count("Sidebar skills: Django"), 1)
        self.assertIn("Nested award", text)

    def test_unsupported_types_rejected(self):
        for name in ("cv.doc", "cv.txt", "cv.exe", "cv", "", None):
            with self.subTest(name), self.assertRaises(resume.ResumeError):
                resume.extract_text(name, b"data")

    def test_empty_file_rejected(self):
        with self.assertRaisesMessage(resume.ResumeError, "empty"):
            resume.extract_text("cv.pdf", b"")

    def test_too_big_rejected(self):
        with self.assertRaisesMessage(resume.ResumeError, "10 MB"):
            resume.extract_text("cv.pdf", b"%PDF" + b"0" * resume.MAX_BYTES)

    def test_renamed_file_rejected(self):
        with self.assertRaisesMessage(resume.ResumeError, "valid PDF"):
            resume.extract_text("cv.pdf", b"MZ this is really an exe")
        with self.assertRaisesMessage(resume.ResumeError, "Word"):
            resume.extract_text("cv.docx", b"not a zip file")

    def test_corrupt_pdf_rejected(self):
        with self.assertRaises(resume.ResumeError):
            resume.extract_text("cv.pdf", b"%PDF-1.4 garbage with no structure")

    def test_password_protected_pdf_rejected(self):
        writer = PdfWriter(clone_from=PdfReader(io.BytesIO(minimal_pdf("Secret"))))
        writer.encrypt("hunter2", algorithm="RC4-128")
        buffer = io.BytesIO()
        writer.write(buffer)
        with self.assertRaisesMessage(resume.ResumeError, "password-protected"):
            resume.extract_text("cv.pdf", buffer.getvalue())


class GuessContactTests(SimpleTestCase):
    def test_finds_all_three(self):
        details = resume.guess_contact("Sam O'Neil\n+44 7700 900123\nhttps://www.linkedin.com/in/sam-oneil/\nDeveloper")
        self.assertEqual(details["full_name"], "Sam O'Neil")
        self.assertEqual(details["phone"], "+44 7700 900123")
        self.assertIn("linkedin.com/in/sam-oneil", details["linkedin"])

    def test_capitalised_name_tidied(self):
        self.assertEqual(resume.guess_contact("SAM TAYLOR\nDeveloper")["full_name"], "Sam Taylor")

    def test_heading_is_not_a_name(self):
        for heading in ("Curriculum vitae of Sam: 2024", "Senior Python Developer", "Professional Summary", "Work Experience"):
            with self.subTest(heading):
                self.assertEqual(resume.guess_contact(f"{heading}\nDeveloper")["full_name"], "")

    def test_title_line_above_the_name_skipped(self):
        for title in ("Curriculum Vitae", "CV", "RÉSUMÉ"):
            with self.subTest(title):
                self.assertEqual(resume.guess_contact(f"{title}\nSam Taylor\nDeveloper")["full_name"], "Sam Taylor")

    def test_name_found_in_common_header_layouts(self):
        for header in ("Sam Taylor | Senior Python Developer", "Sam Taylor, MBA", "Sam Taylor - Developer", "Sam Taylor • London",
                       "Contact details\nSam Taylor", "07700 900123\nsam@acme.co.uk\nSam Taylor"):
            with self.subTest(header):
                self.assertEqual(resume.guess_contact(f"{header}\nExperience")["full_name"], "Sam Taylor")

    def test_bracketed_phone_numbers_kept_whole(self):
        for phone in ("(0161) 555 0100", "(020) 7946 0958", "(+44) 7700 900123", "+44 (0) 7700 900123", "07700 900123"):
            with self.subTest(phone):
                self.assertEqual(resume.guess_contact(f"Sam Taylor\n{phone}")["phone"], phone)

    def test_date_ranges_are_not_phone_numbers(self):
        self.assertEqual(resume.guess_contact("Sam Taylor\n2018 - 2021 2022 at Acme\n01/2018 - 03/2021")["phone"], "")

    def test_mimetype(self):
        self.assertEqual(resume.mimetype("cv.PDF"), "application/pdf")
        self.assertIn("wordprocessingml", resume.mimetype("cv.docx"))
