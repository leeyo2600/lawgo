# -*- coding: utf-8 -*-
"""Tiny read-only XLSX reader using only the Python standard library.

Purpose: read the original workbook values without altering the source file.
The result writer is XlsxWriter in run_full.py.
"""
from __future__ import annotations
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator
import re
import zipfile
import xml.etree.ElementTree as ET

MAIN_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
REL_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
PKG_REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
NS = {"m": MAIN_NS, "r": REL_NS}
CELL_RE = re.compile(r"([A-Z]+)(\d+)")


def col_to_index(col: str) -> int:
    n = 0
    for ch in col:
        n = n * 26 + ord(ch) - 64
    return n - 1


def index_to_col(index: int) -> str:
    n = index + 1
    out = []
    while n:
        n, rem = divmod(n - 1, 26)
        out.append(chr(65 + rem))
    return "".join(reversed(out))


def _cell_value(c: ET.Element, shared: list[str]) -> str:
    typ = c.attrib.get("t")
    if typ == "inlineStr":
        isel = c.find(f"{{{MAIN_NS}}}is")
        if isel is None:
            return ""
        return "".join(t.text or "" for t in isel.iter(f"{{{MAIN_NS}}}t"))
    v = c.find(f"{{{MAIN_NS}}}v")
    if v is None or v.text is None:
        return ""
    raw = v.text
    if typ == "s":
        try:
            return shared[int(raw)]
        except Exception:
            return raw
    if typ == "b":
        return "TRUE" if raw == "1" else "FALSE"
    return raw


@dataclass
class XlsxSheetData:
    name: str
    rows: list[list[str]]
    max_cols: int
    max_rows: int
    merge_ranges: list[str]


class XlsxReader:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        if self.path.suffix.lower() != ".xlsx":
            raise ValueError("입력 파일은 .xlsx 형식이어야 합니다.")

    def _shared_strings(self, z: zipfile.ZipFile) -> list[str]:
        if "xl/sharedStrings.xml" not in z.namelist():
            return []
        root = ET.fromstring(z.read("xl/sharedStrings.xml"))
        out = []
        for si in root.findall(f"{{{MAIN_NS}}}si"):
            out.append("".join(t.text or "" for t in si.iter(f"{{{MAIN_NS}}}t")))
        return out

    def _sheets(self, z: zipfile.ZipFile) -> list[tuple[str, str]]:
        wb = ET.fromstring(z.read("xl/workbook.xml"))
        rels = ET.fromstring(z.read("xl/_rels/workbook.xml.rels"))
        relmap = {x.attrib["Id"]: x.attrib["Target"] for x in rels}
        out = []
        for s in wb.find("m:sheets", NS):
            rid = s.attrib[f"{{{REL_NS}}}id"]
            target = relmap[rid]
            if target.startswith("/"):
                sheet_path = target.lstrip("/")
            elif target.startswith("xl/"):
                sheet_path = target
            else:
                sheet_path = "xl/" + target
            out.append((s.attrib["name"], sheet_path))
        return out

    def read_sheet(self, sheet_name: str | None = None) -> XlsxSheetData:
        with zipfile.ZipFile(self.path) as z:
            shared = self._shared_strings(z)
            sheets = self._sheets(z)
            if not sheets:
                raise ValueError("워크북에 시트가 없습니다.")
            if sheet_name:
                matches = [x for x in sheets if x[0] == sheet_name]
                if not matches:
                    raise ValueError(f"시트를 찾을 수 없습니다: {sheet_name}")
                name, sheet_path = matches[0]
            else:
                preferred = [x for x in sheets if x[0] == "규제목록"]
                name, sheet_path = (preferred[0] if preferred else sheets[0])

            # First pass for dimension and merges.
            root = ET.fromstring(z.read(sheet_path))
            dim = root.find("m:dimension", NS)
            max_rows = 0
            max_cols = 0
            if dim is not None:
                end = dim.attrib.get("ref", "A1").split(":")[-1]
                m = CELL_RE.fullmatch(end)
                if m:
                    max_cols = col_to_index(m.group(1)) + 1
                    max_rows = int(m.group(2))
            merge_node = root.find("m:mergeCells", NS)
            merges = [m.attrib["ref"] for m in merge_node] if merge_node is not None else []

            rows: list[list[str]] = []
            sheet_data = root.find("m:sheetData", NS)
            if sheet_data is None:
                return XlsxSheetData(name, [], max_cols, max_rows, merges)
            for row_el in sheet_data.findall("m:row", NS):
                row_num = int(row_el.attrib.get("r", len(rows) + 1))
                while len(rows) < row_num - 1:
                    rows.append([""] * max_cols)
                vals = [""] * max_cols
                for c in row_el.findall("m:c", NS):
                    ref = c.attrib.get("r", "")
                    m = CELL_RE.fullmatch(ref)
                    if not m:
                        continue
                    ci = col_to_index(m.group(1))
                    if ci >= len(vals):
                        vals.extend([""] * (ci + 1 - len(vals)))
                        max_cols = max(max_cols, ci + 1)
                    vals[ci] = _cell_value(c, shared)
                rows.append(vals)
            while len(rows) < max_rows:
                rows.append([""] * max_cols)
            return XlsxSheetData(name, rows, max_cols, max_rows, merges)
