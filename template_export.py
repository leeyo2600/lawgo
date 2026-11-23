"""Fill the supplied workbook's result cells without rebuilding its layout."""
from __future__ import annotations

import re
import zipfile
from pathlib import Path
from xml.sax.saxutils import escape
from xml.etree import ElementTree as ET

from xlsx_input import XlsxReader, CELL_RE, col_to_index, index_to_col

CONTENT_HEADERS = {
    "법": "법(+조항) 내용", "시행령": "시행령(+조항) 내용",
    "시행규칙": "시행규칙(+조항) 내용", "조례": "조례 내용",
    "규칙": "규칙 내용", "훈령": "훈령 내용", "예규": "예규 내용",
    "고시 등": "고시 등 내용",
}


def header_key(value):
    return re.sub(r"\s+", "", str(value)).replace("(+조항)", "")


def result_columns(headers):
    columns = {header_key(value): i for i, value in enumerate(headers)}
    expected = {**CONTENT_HEADERS, "수정사항": "수정사항"}
    return {slot: columns[header_key(header)] for slot, header in expected.items()
            if header_key(header) in columns}


def plan_columns(headers, width):
    """Append missing outputs, never shift original cells or formula references."""
    columns = result_columns(headers)
    references = {header_key(value): i for i, value in enumerate(headers)}
    added = {}
    for slot, title in {**CONTENT_HEADERS, "수정사항": "수정사항"}.items():
        if slot not in columns and (slot == "수정사항" or header_key(slot) in references):
            columns[slot] = width + len(added)
            added[slot] = (title, references.get(header_key(slot), 0))
    if width + len(added) > 16384:
        raise ValueError("결과 열을 추가할 공간이 없습니다. Excel 최대 열 수를 초과합니다.")
    return columns, added


def extend_columns(xml, width, count):
    """Extend only column widths and used range; retain existing layout XML."""
    text = xml.decode('utf-8')
    prefix = re.search(r'<((?:\w+:)?)worksheet\b', text)[1]
    definitions = ''.join(f'<{prefix}col min="{i+1}" max="{i+1}" width="45" customWidth="1"/>'
                          for i in range(width, width + count))
    closing = f'</{prefix}cols>'
    if closing in text:
        text = text.replace(closing, definitions + closing, 1)
    else:
        text = text.replace(f'<{prefix}sheetData', f'<{prefix}cols>{definitions}</{prefix}cols><{prefix}sheetData', 1)
    def dimension(match):
        old = match[2].split(':')
        last = CELL_RE.fullmatch(old[-1])
        return match[1] + old[0] + ':' + index_to_col(width + count - 1) + last[2] + match[3]
    text = re.sub(r'(<(?:\w+:)?dimension\b[^>]*\bref=")([A-Z]+\d+(?::[A-Z]+\d+)?)(")', dimension, text, count=1)
    return text.encode('utf-8')


def cell_text(value):
    text = str(value or "")
    if re.search(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", text):
        text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", text)
    if len(text.encode("utf-16-le")) // 2 > 32767:
        raise ValueError("조회 본문이 Excel 한 셀의 글자 수 한도를 넘었습니다.")
    return text


def patch_cells(xml, changes, styles=None):
    """One pass over rows; retain all non-target XML bytes and cell attributes."""
    remaining = dict(changes)
    row_pattern = re.compile(r'<(?P<p>(?:[\w]+:)?)row\b(?P<a>[^>]*\br="(?P<n>\d+)"[^>]*?)(?:\s*/>|>(?P<body>.*?)</(?P=p)row>)', re.S)
    cell_pattern = re.compile(r'<(?P<p>(?:[\w]+:)?)c\b(?P<a>[^>]*\br="(?P<coord>[A-Z]+\d+)"[^>]*?)(?:\s*/>|>.*?</(?P=p)c>)', re.S)
    by_row = {}
    for coord, value in changes.items():
        match = CELL_RE.fullmatch(coord)
        if not match:
            raise ValueError("잘못된 결과 셀 주소입니다.")
        by_row.setdefault(match[2], {})[coord] = cell_text(value)

    def fill_row(row):
        pending = dict(by_row.get(row['n'], {}))
        if not pending:
            return row[0]
        prefix = row['p']
        body = row['body'] or ""
        def cell(coord, value, attrs=None, p=prefix):
            if attrs is None and styles and coord in styles:
                attrs = f' r="{coord}" s="{styles[coord]}"'
            attrs = re.sub(r'\s+t="[^"]*"', '', attrs) if attrs else f' r="{coord}"'
            return f'<{p}c{attrs} t="inlineStr"><{p}is><{p}t xml:space="preserve">{escape(value)}</{p}t></{p}is></{p}c>'
        def replace(match):
            coord = match['coord']
            if coord not in pending:
                return match[0]
            if re.search(r'<(?:\w+:)?f\b', match[0]):
                raise ValueError(f"{coord}: 결과 칸에 수식이 있어 덮어쓸 수 없습니다.")
            remaining.pop(coord)
            return cell(coord, pending.pop(coord), match['a'], match['p'])
        body = cell_pattern.sub(replace, body)
        for coord, value in sorted(pending.items(), key=lambda x: col_to_index(CELL_RE.fullmatch(x[0])[1])):
            column = col_to_index(CELL_RE.fullmatch(coord)[1])
            following = next((m for m in cell_pattern.finditer(body)
                              if col_to_index(CELL_RE.fullmatch(m['coord'])[1]) > column), None)
            offset = following.start() if following else len(body)
            body = body[:offset] + cell(coord, value) + body[offset:]
            remaining.pop(coord)
        return f'<{prefix}row{row["a"]}>{body}</{prefix}row>'

    result = row_pattern.sub(fill_row, xml.decode('utf-8')).encode('utf-8')
    if remaining:
        raise ValueError("원본 엑셀에서 결과를 넣을 행을 찾지 못했습니다.")
    ET.fromstring(result)
    return result


def write_template_result(source, destination, meta, completed):
    """Only populate empty content cells; append concise corrections to existing notes."""
    source, destination = Path(source), Path(destination)
    if source.resolve() == destination.resolve():
        raise ValueError("결과 파일은 업로드 원본과 별도로 저장해야 합니다.")
    reader = XlsxReader(source)
    sheet = reader.read_sheet(meta['sheet'])
    width = max(len(row) for row in sheet.rows)
    columns, added = plan_columns(sheet.rows[meta['header_row'] - 1], width)
    changes = {f'{index_to_col(columns[slot])}{meta["header_row"]}': title
               for slot, (title, _) in added.items()}
    def value_at(row, index):
        return row[index] if index < len(row) else ''
    for saved in completed:
        number = meta['source_rows'][saved['index']]
        row = sheet.rows[number - 1]
        notes = []
        for detail in saved['details']:
            slot = detail['세부구분']
            if slot not in CONTENT_HEADERS or slot not in columns or value_at(row, columns[slot]).strip():
                continue
            text = detail.get('추출내용') or ''
            if text:
                changes[f'{index_to_col(columns[slot])}{number}'] = f'[{slot}] {text}'
            correction = detail.get('LLM교정내역')
            if correction:
                notes.append(f'[{slot}] {correction}')
            # The compact workbook has no separate review sheet.
            import pipeline_core as pc
            reasons = [x['message'] for x in pc.issues_for_detail(detail) if x['severity'] in ('WARN', 'ERROR')]
            if detail.get('최종상태') not in ('OK', 'OFFLINE_API_SKIPPED') and not reasons:
                reasons.append('요청한 조문 본문을 확인하지 못했습니다.')
            if reasons:
                notes.append(f'[{slot}] 확인 필요: ' + ' '.join(dict.fromkeys(reasons)))
        if notes:
            old = value_at(row, columns['수정사항'])
            new = [note for note in dict.fromkeys(notes) if note not in old]
            if new:
                changes[f'{index_to_col(columns["수정사항"])}{number}'] = '\n'.join(x for x in [old, *new] if x)

    destination.parent.mkdir(parents=True, exist_ok=True)
    temp = destination.with_suffix('.tmp')
    try:
        with zipfile.ZipFile(source) as archive:
            member = dict(reader._sheets(archive))[meta['sheet']]
            original = archive.read(member)
            styles = {}
            if added:
                cells = {c.get('r'): c.get('s') for c in ET.fromstring(original).iter()
                         if c.tag.endswith('}c') and c.get('s') is not None}
                for coord in changes:
                    match = CELL_RE.fullmatch(coord)
                    for slot, (_, source_column) in added.items():
                        if match[1] == index_to_col(columns[slot]):
                            style = cells.get(f'{index_to_col(source_column)}{match[2]}')
                            if style is not None:
                                styles[coord] = style
                original = extend_columns(original, width, len(added))
            updated = patch_cells(original, changes, styles)
            with zipfile.ZipFile(temp, 'w') as target:
                target.comment = archive.comment
                for info in archive.infolist():
                    target.writestr(info, updated if info.filename == member else archive.read(info.filename))
        temp.replace(destination)
    finally:
        temp.unlink(missing_ok=True)
    return len(changes)
