import io
import zipfile

from wcag.analyzers.docx_analyzer import DocxAnalyzer


def run_doc(xml: str, name: str) -> None:
    bio = io.BytesIO()
    with zipfile.ZipFile(bio, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("word/document.xml", xml)

    fact_sheet = DocxAnalyzer(bio.getvalue(), name).analyze()
    findings = (fact_sheet.confirmed_findings or []) + (fact_sheet.possible_findings or [])

    print(f"--- {name} ---")
    print(f"finding_count={len(findings)}")
    for f in findings:
      print(f"{f.criterion_id}: {f.issue}")


XML_FORM = """<?xml version=\"1.0\" encoding=\"UTF-8\" standalone=\"yes\"?>
<w:document xmlns:w=\"http://schemas.openxmlformats.org/wordprocessingml/2006/main\">
  <w:body>
    <w:sdt>
      <w:sdtPr>
        <w:text/>
      </w:sdtPr>
      <w:sdtContent>
        <w:p><w:r><w:t>value</w:t></w:r></w:p>
      </w:sdtContent>
    </w:sdt>
    <w:p><w:r><w:t>End</w:t></w:r></w:p>
  </w:body>
</w:document>
"""

XML_TABLE = """<?xml version=\"1.0\" encoding=\"UTF-8\" standalone=\"yes\"?>
<w:document xmlns:w=\"http://schemas.openxmlformats.org/wordprocessingml/2006/main\">
  <w:body>
    <w:tbl>
      <w:tr>
        <w:trPr><w:tblHeader/></w:trPr>
        <w:tc>
          <w:tcPr><w:gridSpan w:val=\"2\"/></w:tcPr>
          <w:p><w:r><w:t>Group A</w:t></w:r></w:p>
        </w:tc>
        <w:tc><w:p><w:r><w:t>Tail</w:t></w:r></w:p></w:tc>
      </w:tr>
      <w:tr>
        <w:tc><w:p><w:r><w:t>H1</w:t></w:r></w:p></w:tc>
        <w:tc><w:p><w:r><w:t>H2</w:t></w:r></w:p></w:tc>
        <w:tc><w:p><w:r><w:t>H3</w:t></w:r></w:p></w:tc>
      </w:tr>
      <w:tr>
        <w:tc><w:p><w:r><w:t>a</w:t></w:r></w:p></w:tc>
        <w:tc><w:p><w:r><w:t>b</w:t></w:r></w:p></w:tc>
        <w:tc><w:p><w:r><w:t>c</w:t></w:r></w:p></w:tc>
      </w:tr>
      <w:tr>
        <w:tc><w:p><w:r><w:t>d</w:t></w:r></w:p></w:tc>
        <w:tc><w:p><w:r><w:t>e</w:t></w:r></w:p></w:tc>
        <w:tc><w:p><w:r><w:t>f</w:t></w:r></w:p></w:tc>
      </w:tr>
    </w:tbl>
  </w:body>
</w:document>
"""


if __name__ == "__main__":
    run_doc(XML_FORM, "synthetic_form.docx")
    run_doc(XML_TABLE, "synthetic_complex_table.docx")
