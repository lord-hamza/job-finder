import io
from unittest import mock

from django.test import SimpleTestCase
from docx import Document
from docx.opc.constants import RELATIONSHIP_TYPE
from docx.oxml import parse_xml
from pypdf import PdfReader, PdfWriter
from pypdf.generic import NameObject, NumberObject

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


class ContactDetailsTests(SimpleTestCase):
    def test_phone_and_linkedin_behind_pdf_links(self):
        data = minimal_pdf("Sam Taylor", links=["mailto:sam@x.com", "tel:92 333 0481822", "https://www.linkedin.com/in/sam-taylor/"])
        details = resume.contact_details("cv.pdf", data, "Sam Taylor\nAI engineer")
        self.assertEqual(
            (details["full_name"], details["phone"], details["linkedin"]),
            ("Sam Taylor", "+92 333 0481822", "https://www.linkedin.com/in/sam-taylor/"),
        )

    def test_a_linked_number_printed_in_a_way_the_reader_misses_beats_a_referees(self):
        # Their own number is printed without + or 0, so the only number the text reader sees is a referee's.
        data = minimal_pdf("x", links=["tel:92 333 0481822"])
        text = "Sam Taylor\n92 333 0481822\nReferences\nAli Khan, Cell: 0300-1234567"
        self.assertEqual(resume.contact_details("cv.pdf", data, text)["phone"], "+92 333 0481822")

    def test_a_printed_number_that_matches_the_link_keeps_its_spacing(self):
        for printed in ("+44 7700 900123", "+44 (0) 7700 900123", "0044 7700 900123"):
            with self.subTest(printed):
                data = minimal_pdf("x", links=["tel:+447700900123"])
                self.assertEqual(resume.contact_details("cv.pdf", data, f"Sam Taylor\n{printed}")["phone"], printed)

    def test_a_bracketed_country_code_counts(self):
        data = minimal_pdf("x", links=["tel:+923001234567"])
        self.assertEqual(resume.contact_details("cv.pdf", data, "Sam Taylor\n(+92) 300-1234567")["phone"], "(+92) 300-1234567")

    def test_a_national_number_takes_the_links_country_code(self):
        data = minimal_pdf("x", links=["tel:+923330481822"])
        self.assertEqual(resume.contact_details("cv.pdf", data, "Sam Taylor\n0333-0481822")["phone"], "+923330481822")

    def test_a_stale_phone_link_doesnt_beat_the_printed_number(self):
        # You changed the printed number, but the template kept the old link behind it.
        data = minimal_pdf("x", links=["tel:+923001234567"])
        self.assertEqual(resume.contact_details("cv.pdf", data, "Sam Taylor\n+92 321 7654321")["phone"], "+92 321 7654321")

    def test_with_several_phone_links_the_one_nearest_the_top_wins(self):
        data = minimal_pdf("x", links=["tel:+447911123456", "tel:+447700900123"])
        text = "Sam Taylor\n+44 7700 900123\nExperience\nReferences\nJane Doe +44 7911 123456"
        self.assertEqual(resume.contact_details("cv.pdf", data, text)["phone"], "+44 7700 900123")

    def test_spaces_in_details_are_tidied(self):
        data = minimal_pdf("x", links=["tel:+44%20%207700%20900123"])
        self.assertEqual(resume.contact_details("cv.pdf", data, "Sam Taylor\nDeveloper")["phone"], "+44 7700 900123")

    def test_a_linkedin_link_completes_an_address_cut_off_by_a_line_break(self):
        data = minimal_pdf("x", links=["https://www.linkedin.com/in/muhammad-hamza-a1b2c3d4"])
        text = "Sam Taylor\nlinkedin.com/in/muhammad-hamza-\na1b2c3d4"
        self.assertEqual(
            resume.contact_details("cv.pdf", data, text)["linkedin"], "https://www.linkedin.com/in/muhammad-hamza-a1b2c3d4"
        )

    def test_a_stale_linkedin_link_doesnt_replace_a_complete_printed_address(self):
        # You claimed a shorter address and changed the printed one, but the link still goes to the old one.
        data = minimal_pdf("x", links=["https://www.linkedin.com/in/muhammad-hamza-a1b2c3d4"])
        text = "Sam Taylor\nlinkedin.com/in/muhammad-hamza\nLahore"
        self.assertEqual(resume.contact_details("cv.pdf", data, text)["linkedin"], "linkedin.com/in/muhammad-hamza")

    def test_the_link_that_completes_a_cut_off_address_is_found_among_others(self):
        links = ["https://www.linkedin.com/in/ali-khan-77", "https://www.linkedin.com/in/muhammad-hamza-a1b2c3d4"]
        text = "Sam Taylor\nlinkedin.com/in/muhammad-hamza-\na1b2c3d4"
        self.assertEqual(
            resume.contact_details("cv.pdf", minimal_pdf("x", links=links), text)["linkedin"],
            "https://www.linkedin.com/in/muhammad-hamza-a1b2c3d4",
        )

    def test_a_printed_linkedin_beats_an_unrelated_link(self):
        data = minimal_pdf("x", links=["https://linkedin.com/in/template-name"])
        self.assertEqual(
            resume.contact_details("cv.pdf", data, "Sam Taylor\nlinkedin.com/in/sam")["linkedin"], "linkedin.com/in/sam"
        )

    def test_phone_links(self):
        cases = {
            "tel:+447700900123": "+447700900123",
            "tel:92 333 0481822": "+92 333 0481822",
            "tel:923330481822": "+923330481822",
            "tel:03330481822": "03330481822",
            "tel:0092 333 0481822": "0092 333 0481822",
            "tel:(0161)%20555%200100": "(0161) 555 0100",
            "TEL:+44-7700-900123;ext=1": "+44-7700-900123",
            "tel:4155551234": "4155551234",  # ten digits can't say which country, so it's left as it is
            "tel:123": "",
            "tel:call me": "",
            "tel:+44 7700 900123 (mobile)": "",
            "https://example.com/tel:+447700900123": "",
            "tel://923330481822": "+923330481822",
            "tel:/923330481822": "+923330481822",
            "tel://+447700900123": "+447700900123",
            "tel:-923330481822": "",
        }
        for link, phone in cases.items():
            with self.subTest(link):
                self.assertEqual(resume._tel_number(link), phone)

    def test_with_nothing_printed_the_first_usable_phone_link_wins(self):
        data = minimal_pdf("x", links=["tel:12", "tel:+44 7700 900123", "tel:+92 333 0481822"])
        self.assertEqual(resume.contact_details("cv.pdf", data, "Sam Taylor\nDeveloper")["phone"], "+44 7700 900123")

    def test_docx_links_in_the_body_and_the_header(self):
        document = Document()
        document.add_paragraph("Sam Taylor")
        document.part.relate_to("https://www.linkedin.com/in/sam-docx", RELATIONSHIP_TYPE.HYPERLINK, is_external=True)
        document.sections[0].header.part.relate_to("tel:+44 7700 900123", RELATIONSHIP_TYPE.HYPERLINK, is_external=True)
        buffer = io.BytesIO()
        document.save(buffer)
        data = buffer.getvalue()
        self.assertEqual(set(resume.links("cv.docx", data)), {"https://www.linkedin.com/in/sam-docx", "tel:+44 7700 900123"})
        details = resume.contact_details("cv.docx", data, "Sam Taylor\nDeveloper")
        self.assertEqual((details["phone"], details["linkedin"]), ("+44 7700 900123", "https://www.linkedin.com/in/sam-docx"))

    def test_files_that_cant_be_read_have_no_links(self):
        for name, data in (("cv.pdf", b"%PDF garbage"), ("cv.docx", b"PK garbage"), ("cv.pdf", b""), ("cv.txt", b"x")):
            with self.subTest(name=name, data=data):
                self.assertEqual(resume.links(name, data), [])
        self.assertEqual(resume.contact_details("cv.pdf", b"%PDF garbage", "Sam Taylor")["phone"], "")

    def test_links_that_arent_linkedin_profiles_are_ignored(self):
        data = minimal_pdf("x", links=["https://www.linkedin.com/company/acme", "https://github.com/sam"])
        self.assertEqual(resume.contact_details("cv.pdf", data, "Sam Taylor")["linkedin"], "")

    def test_odd_link_targets_are_skipped_not_fatal(self):
        with mock.patch("core.resume._docx_links", return_value=[None, "  ", "tel:+44 7700 900123"]):
            self.assertEqual(resume.links("cv.docx", b"PK"), ["tel:+44 7700 900123"])

    def test_a_broken_later_page_keeps_the_links_already_found(self):
        writer = PdfWriter(clone_from=io.BytesIO(minimal_pdf("x", links=["tel:+44 7700 900123"])))
        writer.add_blank_page()
        writer.pages[1][NameObject("/Annots")] = NumberObject(42)
        buffer = io.BytesIO()
        writer.write(buffer)
        self.assertEqual(resume.links("cv.pdf", buffer.getvalue()), ["tel:+44 7700 900123"])

    def test_links_are_capped(self):
        data = minimal_pdf("x", links=[f"https://example.com/{n}" for n in range(resume.MAX_LINKS + 50)])
        self.assertEqual(len(resume.links("cv.pdf", data)), resume.MAX_LINKS)
