"""Read public ELIS current ordinances; never infer a successor or scrape images."""
from __future__ import annotations

import hashlib
import json
import re
import time
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlencode

import requests
from http_transport import create_session
import lawgo_core as core

BASE = "https://www.elis.go.kr"


class Node:
    def __init__(self, tag="", attrs=()):
        self.tag, self.attrs, self.children = tag, dict(attrs), []

    def walk(self):
        yield self
        for child in self.children:
            if isinstance(child, Node):
                yield from child.walk()

    def text(self):
        if self.tag in ("script", "style"):
            return ""
        if self.tag in ("img", "svg", "object", "embed", "iframe"):
            return f"<{self.tag}>"  # Preserve evidence of unreadable content.
        prefix = "\n" if self.tag in ("br", "p", "div", "li") else ""
        return prefix + "".join(c.text() if isinstance(c, Node) else c for c in self.children)


class Document(HTMLParser):
    def __init__(self, text):
        super().__init__(convert_charrefs=True)
        self.root = Node()
        self.stack = [self.root]
        self.feed(text)

    def handle_starttag(self, tag, attrs):
        node = Node(tag, attrs)
        self.stack[-1].children.append(node)
        if tag not in ("area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"):
            self.stack.append(node)

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        self.handle_endtag(tag)

    def handle_endtag(self, tag):
        for i in range(len(self.stack)-1, 0, -1):
            if self.stack[i].tag == tag:
                del self.stack[i:]
                break

    def handle_data(self, data):
        self.stack[-1].children.append(data)


class ElisUnavailable(ValueError):
    pass


def parse_list(html):
    entries = {}
    for node in Document(html).root.walk():
        match = re.search(r"lawPopup\([^)]*?'(\d+)'\s*,\s*'(\d+)'\s*\)", node.attrs.get("onclick", ""))
        if node.tag == "a" and match:
            entry = {"title": core.norm_space(node.text()), "alrNo": match[1], "histNo": match[2]}
            entries[(match[1], match[2])] = entry
    return list(entries.values())


def parse_detail(html):
    nodes = list(Document(html).root.walk())
    area = next((n for n in nodes if "title-area" in n.attrs.get("class", "").split()), None)
    if area is None:
        raise ElisUnavailable("ELIS_PAGE_FORMAT_CHANGED")
    title = next((core.norm_space(n.text()) for n in area.walk() if "tit" in n.attrs.get("class", "").split()), "")
    heading = core.norm_space(area.text())
    kind = re.search(r"(조례|규칙|훈령|예규|고시)\s*제\s*\d", heading)
    articles = []
    for node in nodes:
        if node.tag != "div" or "txt" not in node.attrs.get("class", "").split():
            continue
        # The public detail has separate numeric IDs for articles, BUC IDs for addenda.
        if not re.fullmatch(r"\d+", node.attrs.get("id", "")):
            continue
        text = node.text().strip()
        match = re.match(r"제\s*(\d+)\s*조(?:\s*의\s*(\d+))?", text)
        if match:
            articles.append({"조번호": core.encode6(int(match[1]), int(match[2] or 0)), "조내용": text})
    if not title or not kind or not articles:
        raise ElisUnavailable("ELIS_BODY_NOT_VERIFIABLE")
    return {"title": title, "kind": kind[1], "heading": heading, "body": {"조문": articles}}


def province_name(value):
    aliases = {"강원특별자치도": "강원", "강원도": "강원", "전북특별자치도": "전북", "전라북도": "전북", "전라남도": "전남", "경상북도": "경북", "경상남도": "경남", "충청북도": "충북", "충청남도": "충남"}
    value = core.norm_space(value)
    return aliases.get(value, re.sub(r"(특별자치도|특별자치시|특별시|광역시|도)$", "", value))


class ElisClient:
    def __init__(self, cache_dir, check_cancel=None, timeout=20):
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.session = create_session()
        self.session.headers["User-Agent"] = "lawgo-public-ordinance-reader/1.0"
        self.check_cancel = check_cancel or (lambda: None)
        self.timeout = timeout
        self.calls = self.cache_hits = 0

    def close(self):
        self.session.close()

    def request(self, path, params=None, data=None, headers=None, cache=True):
        key = hashlib.sha256(json.dumps([path, params, data], sort_keys=True).encode()).hexdigest()
        file = self.cache_dir / (key + ".json")
        self.check_cancel()
        if cache and file.exists() and time.time()-file.stat().st_mtime < 86400:
            self.cache_hits += 1
            return json.loads(file.read_text(encoding="utf-8"))
        for attempt in range(3):
            self.check_cancel()
            try:
                self.calls += 1
                with self.session.request("POST" if data is not None else "GET", BASE+path, params=params, data=data, headers=headers, timeout=self.timeout) as response:
                    response.raise_for_status()
                    response.encoding = "utf-8"
                    text = response.text
                if cache:
                    tmp = file.with_suffix(".tmp")
                    tmp.write_text(json.dumps(text, ensure_ascii=False), encoding="utf-8")
                    tmp.replace(file)
                return text
            except requests.RequestException as exc:
                status = exc.response.status_code if exc.response is not None else None
                if attempt == 2 or (status is not None and status != 429 and status < 500):
                    raise
                for _ in range(10 * (attempt+1)):
                    self.check_cancel()
                    time.sleep(.1)

    def region(self, municipality):
        parts = core.norm_space(municipality).split()
        if len(parts) != 2:
            raise ElisUnavailable("ELIS_REGION_UNRESOLVED")
        # Normal webpage session + CSRF, not an undocumented API key or guessed region code.
        html = self.request("/locgovalr/locgovClAlrList", cache=False)
        nodes = list(Document(html).root.walk())
        select = next((n for n in nodes if n.attrs.get("id") == "cboCtpvCd"), None)
        options = [n for n in select.walk() if n.tag == "option" and province_name(n.text()) == province_name(parts[0])] if select else []
        if len(options) != 1:
            raise ElisUnavailable("ELIS_REGION_UNRESOLVED")
        province = options[0].attrs.get("value")
        meta = {n.attrs.get("name"): n.attrs.get("content") for n in nodes if n.tag == "meta"}
        if not meta.get("_csrf") or not meta.get("_csrf_header"):
            raise ElisUnavailable("ELIS_PAGE_FORMAT_CHANGED")
        data = json.loads(self.request("/allalr/selectSggList", data={"ctpvCd": province}, headers={meta["_csrf_header"]: meta["_csrf"]}))
        matches = [d for d in core.iter_dicts(data) if d.get("sggNm") == parts[1] and d.get("sggCd")]
        if len(matches) != 1:
            raise ElisUnavailable("ELIS_REGION_UNRESOLVED")
        return {"ctpvCd": province, "sggCd": str(matches[0]["sggCd"])}

    def find_current(self, title, municipality, slot):
        if not hasattr(self, "_regions"):
            self._regions = {}
        if municipality not in self._regions:
            self._regions[municipality] = self.region(municipality)
        region = self._regions[municipality]
        html = self.request("/alrpop/locgovAlrPopup", params=region)
        entries = parse_list(html)
        if not entries:
            raise ElisUnavailable("ELIS_LIST_NOT_VERIFIABLE")
        exact = [e for e in entries if core.norm_name(e["title"]) == core.norm_name(title)]
        if len(exact) != 1:
            return None, "ELIS_CURRENT_NAME_NOT_FOUND" if not exact else "ELIS_MULTIPLE_EXACT_MATCHES"
        ids = {k: exact[0][k] for k in ("alrNo", "histNo")}
        detail = parse_detail(self.request("/alrpop/alrDtlsPop", params=ids))
        if core.norm_name(detail["title"]) != core.norm_name(title) or not core.ordinance_type_match(slot, detail["kind"]):
            return None, "ELIS_TITLE_OR_KIND_MISMATCH"
        detail.update(url=BASE+"/alrpop/alrDtlsPop?"+urlencode(ids), region=region, **ids)
        return detail, "ELIS_CURRENT_EXACT"
