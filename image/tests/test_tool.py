import binascii
import contextlib
import hashlib
import importlib.util
import io
import json
import os
import shutil
import struct
import sys
import tempfile
import unittest
import zlib
from io import BytesIO
from pathlib import Path

from docx import Document, __version__
from docx.enum.section import WD_SECTION
from docx.opc.constants import RELATIONSHIP_TYPE as RT
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Inches

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("foliorun_image_tool", ROOT / "image" / "tool.py")
tool = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = tool
spec.loader.exec_module(tool)

SYNTHETIC_PNG = (
    b"\x89PNG\r\n\x1a\n"
    + struct.pack(">I", 13)
    + b"IHDR"
    + struct.pack(">IIBBBBB", 1, 1, 8, 6, 0, 0, 0)
    + struct.pack(">I", binascii.crc32(b"IHDR" + struct.pack(">IIBBBBB", 1, 1, 8, 6, 0, 0, 0)))
    + struct.pack(">I", 13)
    + b"IDAT"
    + zlib.compress(b"\x00\xff\x00\x00\xff")
    + struct.pack(">I", binascii.crc32(b"IDAT" + zlib.compress(b"\x00\xff\x00\x00\xff")))
    + struct.pack(">I", 0)
    + b"IEND"
    + struct.pack(">I", binascii.crc32(b"IEND"))
)


def add_synthetic_hyperlink(paragraph):
    relationship_id = paragraph.part.relate_to(
        "https://fictional.invalid/synthetic",
        RT.HYPERLINK,
        is_external=True,
    )
    hyperlink = OxmlElement("w:hyperlink")
    hyperlink.set(qn("r:id"), relationship_id)
    run = OxmlElement("w:r")
    properties = OxmlElement("w:rPr")
    underline = OxmlElement("w:u")
    underline.set(qn("w:val"), "single")
    properties.append(underline)
    run.append(properties)
    text = OxmlElement("w:t")
    text.text = "Synthetic external link"
    run.append(text)
    hyperlink.append(run)
    paragraph._p.append(hyperlink)


def make_template(path):
    doc = Document()
    section = doc.sections[0]
    section.page_width = Inches(8.25)
    section.page_height = Inches(11.25)
    section.top_margin = Inches(0.8)
    section.bottom_margin = Inches(0.9)
    section.left_margin = Inches(0.75)
    section.right_margin = Inches(0.85)
    section.header.paragraphs[0].text = "Synthetic template header"
    section.footer.paragraphs[0].text = "Synthetic template footer"

    doc.add_heading("Synthetic body heading", level=1)

    merged_table = doc.add_table(rows=2, cols=2)
    merged_table.cell(0, 0).merge(merged_table.cell(0, 1)).text = "Synthetic merged cell"
    merged_table.cell(1, 0).text = "Synthetic table value A"
    merged_table.cell(1, 1).text = "Synthetic table value B"

    body = doc.add_paragraph()
    body.add_run("Synthetic body ")
    body.add_run("bold fragment").bold = True
    body.add_run(" italic fragment").italic = True
    body.add_run().add_picture(BytesIO(SYNTHETIC_PNG), width=Inches(0.1))
    add_synthetic_hyperlink(body)

    trailing_table = doc.add_table(rows=1, cols=1)
    trailing_table.cell(0, 0).text = "Synthetic trailing table"
    doc.add_paragraph("Synthetic trailing paragraph")
    doc.save(path)


class ImageToolTests(unittest.TestCase):
    def setUp(self):
        if __version__ != "1.2.0":
            raise RuntimeError(f"Expected locked python-docx 1.2.0, found {__version__}")
        self.temporary = tempfile.TemporaryDirectory(prefix="foliorun-image-test-")
        self.root = Path(self.temporary.name)
        self.documents = self.root / "documents"
        self.output = self.root / "output"
        self.documents.mkdir()
        self.output.mkdir()
        self.previous_output_root = tool.OUTPUT_ROOT
        self.previous_input_roots = tool.INPUT_ROOTS
        tool.OUTPUT_ROOT = self.output
        tool.INPUT_ROOTS = (
            self.documents,
            self.root / "materials",
            self.root / "previous",
            self.output,
        )
        self.source = self.documents / "synthetic-template.docx"
        make_template(self.source)
        self.calls = 0

    def tearDown(self):
        tool.OUTPUT_ROOT = self.previous_output_root
        tool.INPUT_ROOTS = self.previous_input_roots
        for path in sorted(self.root.rglob("*"), reverse=True):
            if path.is_dir():
                path.chmod(0o755)
            else:
                path.chmod(0o644)
        self.temporary.cleanup()

    def call(self, args):
        call_id = hashlib.sha256(f"{self.id()}:{self.calls}".encode()).hexdigest()[:16]
        self.calls += 1
        receipt_output = io.StringIO()
        with contextlib.redirect_stdout(receipt_output):
            tool.compute(args, call_id)
        receipt = json.loads(receipt_output.getvalue())
        self.assertEqual(len(receipt["files"]), 1)
        path = Path(receipt["files"][0])
        self.assertEqual(path.stat().st_mode & 0o777, 0o444)
        self.assertEqual(path.parent.stat().st_mode & 0o777, 0o555)
        self.assertEqual(
            receipt["sha256"][str(path)], hashlib.sha256(path.read_bytes()).hexdigest()
        )
        return path

    def export(self, name, source):
        artifact_dir = os.environ.get("FOLIORUN_DOCX_ARTIFACT_DIR")
        if artifact_dir:
            destination_dir = Path(artifact_dir)
            destination_dir.mkdir(parents=True, exist_ok=True)
            destination = destination_dir / name
            if destination.exists():
                raise FileExistsError(destination)
            shutil.copyfile(source, destination)

    def assert_template_properties(self, doc):
        section = doc.sections[0]
        self.assertEqual(section.page_width, Inches(8.25))
        self.assertEqual(section.page_height, Inches(11.25))
        self.assertEqual(section.top_margin, Inches(0.8))
        self.assertEqual(section.bottom_margin, Inches(0.9))
        self.assertEqual(section.left_margin, Inches(0.75))
        self.assertEqual(section.right_margin, Inches(0.85))
        self.assertEqual(section.header.paragraphs[0].text, "Synthetic template header")
        self.assertEqual(section.footer.paragraphs[0].text, "Synthetic template footer")

    def assert_document_relationships(self, doc):
        relationships = list(doc.part.rels.values())
        links = [rel for rel in relationships if rel.reltype == RT.HYPERLINK and rel.is_external]
        images = [rel for rel in relationships if rel.reltype == RT.IMAGE]
        self.assertTrue(
            any(rel.target_ref == "https://fictional.invalid/synthetic" for rel in links)
        )
        self.assertTrue(any(rel.target_part.blob == SYNTHETIC_PNG for rel in images))

    def assert_merged_table(self, table):
        grid_span = table._tbl.tr_lst[0].tc_lst[0].tcPr.find(qn("w:gridSpan"))
        self.assertIsNotNone(grid_span)
        self.assertEqual(grid_span.get(qn("w:val")), "2")

    def test_generate_contains_only_supplied_text(self):
        output = self.call({"operation": "generate", "text": "Entirely fictional body text"})
        generated = Document(output)
        self.assertEqual(
            [paragraph.text for paragraph in generated.paragraphs],
            ["Entirely fictional body text"],
        )
        self.assertEqual(generated.paragraphs[0].style.name, "Normal")
        self.export("generated.docx", output)

    def test_revise_appends_and_preserves_template_without_mutating_source(self):
        original = self.source.read_bytes()
        output = self.call(
            {"operation": "revise", "source": str(self.source), "text": "Synthetic appended text"}
        )
        revised = Document(output)
        self.assertEqual(self.source.read_bytes(), original)
        self.assertEqual(revised.paragraphs[-1].text, "Synthetic appended text")
        self.assertEqual(revised.paragraphs[0].style.name, "Heading 1")
        self.assert_merged_table(revised.tables[0])
        self.assert_template_properties(revised)
        self.assert_document_relationships(revised)
        self.export("synthetic-template.docx", self.source)
        self.export("revised.docx", output)

    def test_paragraph_slice_keeps_paragraph_xml_and_intervening_table(self):
        original = self.source.read_bytes()
        output = self.call(
            {"operation": "slice", "source": str(self.source), "start": 0, "end": 2}
        )
        sliced = Document(output)
        self.assertEqual(self.source.read_bytes(), original)
        self.assertEqual(
            [paragraph.text for paragraph in sliced.paragraphs],
            [
                "Synthetic body heading",
                "Synthetic body bold fragment italic fragmentSynthetic external link",
            ],
        )
        self.assertEqual(
            [paragraph.style.name for paragraph in sliced.paragraphs], ["Heading 1", "Normal"]
        )
        self.assertTrue(sliced.paragraphs[1].runs[1].bold)
        self.assertTrue(sliced.paragraphs[1].runs[2].italic)
        self.assertEqual(len(sliced.tables), 1)
        self.assert_merged_table(sliced.tables[0])
        self.assert_template_properties(sliced)
        self.assert_document_relationships(sliced)
        self.export("paragraph-slice.docx", output)

    def test_block_slice_can_select_table_only_and_range_endpoints(self):
        table_only = self.call(
            {
                "operation": "slice",
                "source": str(self.source),
                "start": 1,
                "end": 2,
                "unit": "block",
            }
        )
        table_doc = Document(table_only)
        self.assertEqual(table_doc.paragraphs, [])
        self.assertEqual(len(table_doc.tables), 1)
        self.assertEqual(table_doc.tables[0].cell(0, 0).text, "Synthetic merged cell")
        self.assert_merged_table(table_doc.tables[0])
        self.assert_template_properties(table_doc)
        self.assert_document_relationships(table_doc)
        self.export("block-slice-table-only.docx", table_only)

        first_block = self.call(
            {
                "operation": "slice",
                "source": str(self.source),
                "start": 0,
                "end": 1,
                "unit": "block",
            }
        )
        self.assertEqual(
            [p.text for p in Document(first_block).paragraphs], ["Synthetic body heading"]
        )
        last_block = self.call(
            {
                "operation": "slice",
                "source": str(self.source),
                "start": 4,
                "end": 5,
                "unit": "block",
            }
        )
        self.assertEqual(
            [p.text for p in Document(last_block).paragraphs], ["Synthetic trailing paragraph"]
        )

    def test_rejects_multisection_and_unsupported_body_nodes(self):
        multisection = Document()
        multisection.add_paragraph("Synthetic first section")
        multisection.add_section(WD_SECTION.NEW_PAGE)
        multisection.add_paragraph("Synthetic second section")
        multisection_path = self.documents / "synthetic-multisection.docx"
        multisection.save(multisection_path)
        with self.assertRaisesRegex(ValueError, "unsupported_sections"):
            self.call(
                {"operation": "slice", "source": str(multisection_path), "start": 0, "end": 1}
            )

        unsupported = Document()
        alt_chunk = OxmlElement("w:altChunk")
        alt_chunk.set(qn("r:id"), "rIdSynthetic")
        unsupported._element.body.insert(len(unsupported._element.body) - 1, alt_chunk)
        unsupported_path = self.documents / "synthetic-unsupported-body.docx"
        unsupported.save(unsupported_path)
        with self.assertRaisesRegex(ValueError, "unsupported_body_node"):
            self.call({"operation": "slice", "source": str(unsupported_path), "start": 0, "end": 1})

    def test_rejects_invalid_units_and_ranges(self):
        invalid = [
            {"start": 0, "end": 1, "unit": "page"},
            {"start": 0, "end": 1, "unit": True},
            {"start": 0, "end": 1, "unit": []},
            {"start": 0, "end": 0},
            {"start": -1, "end": 1},
            {"start": 0, "end": 4},
            {"start": 0.0, "end": 1},
            {"start": False, "end": 1},
            {"start": 0, "end": True},
        ]
        for fields in invalid:
            with self.subTest(fields=fields):
                args = {"operation": "slice", "source": str(self.source), **fields}
                with self.assertRaisesRegex(ValueError, "invalid_(unit|range)"):
                    self.call(args)


if __name__ == "__main__":
    unittest.main(verbosity=2)
