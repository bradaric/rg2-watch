import html as _html
import json
import logging
import re
from datetime import datetime
from urllib.parse import urljoin

from .models import LABEL_TO_TYPE, Doc, Election, ListingPage

_ADDITIONAL = re.compile(r"additionalData = (\{.*?\});", re.S)
_H1 = re.compile(r"<h1[^>]*>(.*?)</h1>", re.S)
_LABEL = re.compile(r"<div class=''>\s*<span>(.*?)</span>", re.S)
_LINK = re.compile(r"<a\s[^>]*href=(['\"])((?:(?!\1)[^<>])*)\1[^>]*>(?:(?!</a>).)*</a>\s*<span>(.*?)</span>", re.S)
_PAGE = re.compile(r'data-page="(\d+)"')
_OPTION = re.compile(r'<option\s+value="([^"]*)"[^>]*>(.*?)</option>', re.S)
_HREF = re.compile(r"<a\s[^>]*href=", re.S)

log = logging.getLogger("rg2watch.rik_parse")
_warned: set = set()


def _warn_once(key, msg: str, *args) -> None:
    if key not in _warned:
        _warned.add(key)
        log.warning(msg, *args)


def _norm(text: str) -> str:
    return " ".join(_html.unescape(text).split())


def parse_election_page(html: str, base_url: str) -> Election:
    data = json.loads(_ADDITIONAL.search(html).group(1))
    title = _norm(re.sub(r"<[^>]+>", "", _H1.search(html).group(1)))
    return Election(
        parent=str(data["parent"]),
        round=str(data["election_round"]),
        election_type=str(data["property_election_type"]),
        title=title,
        base_url=base_url,
    )


def _iso(stamp: str) -> str:
    raw = _norm(stamp)
    try:
        return datetime.strptime(raw, "%d-%m-%Y %H:%Mh").strftime("%Y-%m-%dT%H:%M")
    except ValueError:
        _warn_once(("stamp", raw), "unparseable upload timestamp %r kept as-is", raw)
        return raw


def _parse_extfile(blob: str, municipality: str, station_no: str, station_name: str,
                   base_url: str, minority: str | None = None):
    labels = [(m.start(), _norm(m.group(1))) for m in _LABEL.finditer(blob)]
    links = list(_LINK.finditer(blob))
    hrefs = len(_HREF.findall(blob))
    if hrefs != len(links):
        _warn_once(("links", municipality, station_no, hrefs, len(links)),
                   "%s BM %s: %d links in extfile but %d document(s) parsed",
                   municipality, station_no, hrefs, len(links))
    for m in links:
        preceding = [lbl for pos, lbl in labels if pos < m.start()]
        label = preceding[-1] if preceding else ""
        yield Doc(
            municipality=municipality,
            station_no=station_no,
            station_name=station_name,
            label=label,
            doc_type=LABEL_TO_TYPE.get(label),
            url=urljoin(base_url, _html.unescape(m.group(2))),
            uploaded_at=_iso(m.group(3)),
            minority=minority,
        )


def parse_listing(payload: dict, base_url: str) -> ListingPage:
    records = payload["records"]
    docs: list[Doc] = []
    for rec in records:
        for ext in rec.get("extfiles", []):
            docs.extend(_parse_extfile(
                ext.get("extfile", ""),
                rec["municipality"],
                str(rec["election_station_number"]),
                rec.get("election_station_name", ""),
                base_url,
                rec.get("minority") or None,
            ))
    pages = [int(n) for n in _PAGE.findall(payload.get("pagination", ""))]
    max_page = max(pages) if pages else (1 if records else 0)
    return ListingPage(docs, len(records), max_page)


def parse_municipalities(payload: list[dict]) -> dict[str, str]:
    return {str(item["id"]): item["name"] for item in payload}


def parse_station_options(html: str) -> dict[str, str]:
    return {v: _norm(t) for v, t in _OPTION.findall(html) if v != "-1"}
