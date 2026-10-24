from pathlib import Path
import re

from docx import Document
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.opc.constants import RELATIONSHIP_TYPE as RT
from docx.shared import Cm, Pt, RGBColor

base = Path(__file__).resolve().parent
source = base / "README.md"
out = base / "2026-11-01_坪頂古圳健行.docx"
doc = Document()
section = doc.sections[0]
section.top_margin = section.bottom_margin = Cm(1.7)
section.left_margin = section.right_margin = Cm(1.8)

for name in ("Normal", "Title", "Heading 1", "Heading 2", "List Bullet", "List Number"):
    style = doc.styles[name]
    style.font.name = "Microsoft JhengHei"
    style.element.get_or_add_rPr().rFonts.set(qn("w:eastAsia"), "Microsoft JhengHei")
doc.styles["Normal"].font.size = Pt(10)
doc.styles["Normal"].paragraph_format.space_after = Pt(5)
doc.styles["Heading 1"].font.color.rgb = RGBColor(36, 92, 66)


def add_inline(paragraph, text):
    pattern = re.compile(r"\[([^\]]+)\]\(([^)]+)\)|(\*\*.+?\*\*)")
    position = 0
    for match in pattern.finditer(text):
        paragraph.add_run(text[position:match.start()])
        if match.group(1):
            hyperlink = OxmlElement("w:hyperlink")
            hyperlink.set(qn("r:id"), paragraph.part.relate_to(match.group(2), RT.HYPERLINK, is_external=True))
            run = OxmlElement("w:r")
            props = OxmlElement("w:rPr")
            color = OxmlElement("w:color")
            color.set(qn("w:val"), "0563C1")
            props.append(color)
            run.append(props)
            value = OxmlElement("w:t")
            value.text = match.group(1)
            run.append(value)
            hyperlink.append(run)
            paragraph._p.append(hyperlink)
        else:
            paragraph.add_run(match.group(3).strip("*")).bold = True
        position = match.end()
    paragraph.add_run(text[position:])


for line in source.read_text(encoding="utf-8").splitlines():
    if not line.strip():
        continue
    image = re.fullmatch(r"!\[([^\]]*)\]\(([^)]+)\)", line)
    if image:
        image_path = base / image.group(2)
        paragraph = doc.add_paragraph()
        paragraph.alignment = WD_ALIGN_PARAGRAPH.CENTER
        paragraph.add_run().add_picture(str(image_path), width=Cm(16))
    elif line.startswith("# "):
        doc.add_heading(line[2:], 0)
    elif line.startswith("## "):
        doc.add_heading(line[3:], 1)
    elif line.startswith("### "):
        doc.add_heading(line[4:], 2)
    elif re.match(r"^\d+\. ", line):
        paragraph = doc.add_paragraph(style="List Number")
        add_inline(paragraph, re.sub(r"^\d+\. ", "", line))
    elif line.startswith("- "):
        paragraph = doc.add_paragraph(style="List Bullet")
        add_inline(paragraph, line[2:])
    else:
        add_inline(doc.add_paragraph(), line)

doc.save(out)
check = Document(out)
assert any("坪頂古圳健行" in p.text for p in check.paragraphs)
assert len(check.inline_shapes) == 1
assert any(rel.reltype == RT.HYPERLINK and "maps.app.goo.gl" in rel.target_ref for rel in check.part.rels.values())
print(f"已產生並驗證：{out} ({out.stat().st_size:,} bytes)")
