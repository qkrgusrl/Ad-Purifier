"""수집 원문을 수정하지 않는 AI 공통 검사 범위와 분할 위치 결합."""
import html
import re
from html.parser import HTMLParser

from .textnorm import normalize

_VOID = frozenset('area base br col embed hr img input link meta param source track wbr'.split())
_CODE = frozenset(('script', 'style', 'noscript', 'template'))
_ATTR = re.compile(r'''\s+([^\s"'<>/=]+)(?:\s*=\s*(?:"([^"]*)"|'([^']*)'))?''')


class _Source(HTMLParser):
    def __init__(self, raw):
        super().__init__(convert_charrefs=False)
        self.raw, self.stack, self.nodes = raw, [], []
        self.root, self.opening, self.closed = None, '', False
        self.lines = [0] + [m.end() for m in re.finditer('\n', raw)]

    def char_offset(self):
        line, col = self.getpos()
        return self.lines[line - 1] + col

    def handle_starttag(self, tag, attrs):
        if not self.stack:
            if self.root is not None:
                raise ValueError('여러 루트의 원문')
            self.root, self.opening = tag, self.get_starttag_text()
            self.root_start = self.char_offset()
        if tag not in _VOID:
            self.stack.append(tag)
        elif not self.stack:
            self.closed = True

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in _VOID:
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        if not self.stack or self.stack[-1] != tag:
            raise ValueError('불명확한 태그 소유 관계')
        self.stack.pop()
        if not self.stack:
            self.closed = True

    def add(self, raw):
        if not self.stack:
            if raw.strip():
                raise ValueError('소유 요소 밖의 문구')
        elif not any(t in _CODE for t in self.stack):
            start = self.char_offset()
            self.nodes.append((start, start + len(raw), len(self.stack)))

    def handle_data(self, data):
        self.add(data)

    def handle_entityref(self, name):
        token = '&' + name
        if self.raw.startswith(token + ';', self.char_offset()):
            token += ';'
        self.add(token)

    def handle_charref(self, name):
        self.handle_entityref('#' + name)

    def attr_span(self, name):
        pos, found = len(self.root) + 1, []
        while match := _ATTR.match(self.opening, pos):
            if match[1].lower() == name:
                group = 2 if match[2] is not None else 3
                if match[group] is None:
                    return []
                found.append((self.root_start + match.start(group), self.root_start + match.end(group)))
            pos = match.end()
        return found if len(found) == 1 and self.opening[pos:].strip() in ('>', '/>') else []


def owner_spans(row, mode=None):
    """수집기의 ownText/textOf/속성 중 실제 수집 문구와 일치하는 소유 범위만 반환."""
    raw = row.get('source_html', '')
    parser = _Source(raw)
    parser.feed(raw)
    parser.close()
    if not parser.closed or parser.stack or parser.root in _CODE:
        raise ValueError('완결된 텍스트 소유 요소 없음')
    if row.get('tag') and parser.root != row['tag'].lower():
        raise ValueError('수집 태그 불일치')
    attr = row.get('attr')
    if attr and attr not in ('alt', 'title', 'aria-label'):
        raise ValueError('DOM 원문에 없는 가상 문구')
    choices = ([('attr:' + attr, parser.attr_span(attr))] if attr else
               [('attr:content', parser.attr_span('content'))] if parser.root == 'meta' else
               [('own', [(s, e) for s, e, depth in parser.nodes if depth == 1]),
                ('text', [(s, e) for s, e, depth in parser.nodes])])
    expected = normalize(row.get('evidence_text', row.get('text', '')))
    for kind, spans in choices:
        if mode is not None and kind != mode:
            continue
        spans = [(s, e) for s, e in spans if s < e]
        # 인접한 data/entity 토큰은 하나의 연속 원문으로 보존한다.
        merged = []
        for s, e in spans:
            if merged and merged[-1][1] == s:
                merged[-1] = (merged[-1][0], e)
            else:
                merged.append((s, e))
        value = ''.join(html.unescape(raw[s:e]) for s, e in merged)
        if merged and (mode is not None or (expected and normalize(value) == expected)):
            return kind, merged, value
    raise ValueError('수집 문구와 원문 소유 범위가 일치하지 않음')


def project(row):
    mode, spans, text = owner_spans(row)
    return {'mode': mode, 'spans': spans, 'text': text}


def _atoms(raw, spans):
    """표시 문자 → raw 문자 위치. 엔티티 한 개를 분할하거나 재작성하지 않는다."""
    position = 0
    for start, end in spans:
        value = raw[start:end]
        for match in re.finditer(r'&(?:#[xX][0-9a-fA-F]+;?|#\d+;?|[A-Za-z][A-Za-z0-9]*;?)|[^&]+|&', value):
            token = match[0]
            decoded = html.unescape(token)
            if decoded == token:
                for i in range(len(token)):
                    yield position, position + 1, start + match.start() + i, start + match.start() + i + 1
                    position += 1
            else:
                yield position, position + len(decoded), start + match.start(), start + match.end()
                position += len(decoded)


def split_row(row, projection, segments):
    """계획된 표시 문자 범위를 원문 범위로 변환. 조각 경계 엔티티는 양쪽에 보존."""
    text, raw = projection['text'], row['source_html']
    if not segments or [(x['start_char'], x['end_char']) for x in segments][0][0] != 0:
        raise ValueError('분할 시작 위치 누락')
    previous = 0
    for seg in segments:
        s, e = seg['start_char'], seg['end_char']
        if type(s) is not int or type(e) is not int or s != previous or not s < e <= len(text):
            raise ValueError('분할 범위의 누락/중복/역순')
        previous = e
    if previous != len(text):
        raise ValueError('분할 후 미검사 꼬리 문구')
    atoms = list(_atoms(raw, projection['spans']))
    result, empty_ranges = [], []
    for i, seg in enumerate(segments):
        selected = [(rs, re_) for ds, de, rs, re_ in atoms
                    if ds < seg['end_char'] and de > seg['start_char']]
        spans = []
        for s, e in selected:
            if spans and spans[-1][1] == s:
                spans[-1][1] = e
            else:
                spans.append([s, e])
        value = ''.join(html.unescape(raw[s:e]) for s, e in spans)
        if not normalize(value):
            # 정규화 결과가 비어 있는 범위도 누락과 구분해 기록한다. 모델에는 보내지 않는다.
            empty_ranges.append([seg['start_char'], seg['end_char']])
            continue
        sid = row.get('source_id', 'source')
        unit = {**row, 'source_id': sid if len(segments) == 1 else f'{sid}#unit-{i+1}',
                'text': value, 'raw': value,
                'inspection': {'mode': projection['mode'], 'target_spans': spans,
                               'parent_source_id': sid, 'unit_index': i + 1,
                               'unit_count': len(segments), 'start_char': seg['start_char'],
                               'end_char': seg['end_char'], 'parent_chars': len(text),
                               'planned_input_tokens': seg.get('input_tokens')}}
        if 'evidence_text' in row:
            unit['evidence_text'] = value
        result.append(unit)
    if not result:
        raise ValueError('검사할 원문 문구 없음')
    if empty_ranges:
        result[0]['inspection']['normalization_empty_ranges'] = empty_ranges
    return result


def matches(binding, row):
    """명시적으로 투영한 동일 단위 안에서 선택한 원문 ID만 허용."""
    inspection = row.get('inspection') or {}
    spans = inspection.get('target_spans')
    if not isinstance(spans, list) or not spans or binding.get('source_field') != 'html':
        return False
    try:
        _, allowed, _ = owner_spans(row, inspection['mode'])
        raw, previous = row['source_html'], -1
        for s, e in spans:
            if type(s) is not int or type(e) is not int or s < previous or not s < e <= len(raw):
                return False
            if not any(a <= s < e <= b for a, b in allowed):
                return False
            previous = e
        expected = normalize(row.get('evidence_text', row.get('text', '')))
        return (normalize(''.join(html.unescape(raw[s:e]) for s, e in spans)) == expected
                and [binding.get('start_char'), binding.get('end_char')] in spans)
    except (KeyError, TypeError, ValueError):
        return False
