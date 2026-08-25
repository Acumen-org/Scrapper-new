"""Minimal native .xlsx writer. One sheet, a header row, data rows.

An .xlsx is a zip of a handful of XML parts, and everything Bellwether exports is
a flat table. Writing that directly keeps the dependency list at zero rather than
pulling in openpyxl (and its ~5MB) for a job this small. No formulas, no styling
beyond a bold frozen header, no merged cells: deliberately.

Values are written as inline strings or numbers. Anything that is not a number
becomes text, so a phone number with a leading zero or a CRD stays intact instead
of being mangled into a float, which is the whole reason people paste this into
Excel by hand today.
"""

from __future__ import annotations

import io
import zipfile
from datetime import datetime, timezone

CONTENT_TYPES = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
<Default Extension="xml" ContentType="application/xml"/>
<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>
<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>
<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>
</Types>"""

ROOT_RELS = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>
</Relationships>"""

WB_RELS = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>
<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>
</Relationships>"""

# One named cell style: bold, used for the header row only (style index 1).
STYLES = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
<fonts count="2"><font><sz val="11"/><name val="Calibri"/></font>
<font><b/><sz val="11"/><name val="Calibri"/></font></fonts>
<fills count="1"><fill><patternFill patternType="none"/></fill></fills>
<borders count="1"><border/></borders>
<cellStyleXfs count="1"><xf/></cellStyleXfs>
<cellXfs count="2"><xf/><xf fontId="1" applyFont="1"/></cellXfs>
<cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles>
</styleSheet>"""


def _esc(s: str) -> str:
    return (s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            .replace('"', "&quot;"))


def _col(n: int) -> str:
    """0-based column index to a spreadsheet letter (0->A, 26->AA)."""
    s = ""
    n += 1
    while n:
        n, r = divmod(n - 1, 26)
        s = chr(65 + r) + s
    return s


def _is_number(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _cell(col: int, row: int, value, style: int = 0) -> str:
    ref = f"{_col(col)}{row}"
    st = f' s="{style}"' if style else ""
    if value is None or value == "":
        return f'<c r="{ref}"{st}/>'
    if _is_number(value):
        return f'<c r="{ref}"{st}><v>{value}</v></c>'
    # Inline string: no shared-string table needed, and it keeps identifiers
    # like CRDs and phone numbers as text rather than coercing to a float.
    return (f'<c r="{ref}"{st} t="inlineStr"><is><t xml:space="preserve">'
            f'{_esc(str(value))}</t></is></c>')


def write_sheet(headers: list[str], rows, sheet_name: str = "Sheet1") -> bytes:
    """Return the bytes of an .xlsx with one sheet: bold frozen header + rows.

    `rows` is any iterable of sequences. Cells that look like numbers are stored
    as numbers; everything else is text.
    """
    out = io.StringIO()
    out.write('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
              '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">')
    out.write('<sheetViews><sheetView workbookViewId="0">'
              '<pane ySplit="1" topLeftCell="A2" activePane="bottomLeft" state="frozen"/>'
              '</sheetView></sheetViews>')
    out.write("<sheetData>")
    out.write("<row r=\"1\">")
    for ci, h in enumerate(headers):
        out.write(_cell(ci, 1, h, style=1))
    out.write("</row>")
    r = 2
    for record in rows:
        out.write(f'<row r="{r}">')
        for ci, val in enumerate(record):
            out.write(_cell(ci, r, val))
        out.write("</row>")
        r += 1
    out.write("</sheetData></worksheet>")
    sheet_xml = out.getvalue()

    workbook = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"'
                ' xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
                f'<sheets><sheet name="{_esc(sheet_name)[:31]}" sheetId="1" r:id="rId1"/>'
                '</sheets></workbook>')

    buf = io.BytesIO()
    # Fixed timestamp: a byte-identical export for identical data, and no reliance
    # on Date.now() which is unavailable in some run contexts.
    zi_date = (2026, 1, 1, 0, 0, 0)
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for name, data in (
            ("[Content_Types].xml", CONTENT_TYPES),
            ("_rels/.rels", ROOT_RELS),
            ("xl/workbook.xml", workbook),
            ("xl/_rels/workbook.xml.rels", WB_RELS),
            ("xl/styles.xml", STYLES),
            ("xl/worksheets/sheet1.xml", sheet_xml),
        ):
            zi = zipfile.ZipInfo(name, date_time=zi_date)
            z.writestr(zi, data)
    return buf.getvalue()
