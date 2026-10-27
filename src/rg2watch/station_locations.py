"""Optional polling-place location enrichment from a locally cached public dataset."""

import json
from functools import lru_cache
from pathlib import Path

from .names import to_cyrillic


DATA = Path(__file__).parent / "static" / "station_locations.json"
_ALIASES = {
    "БЕОГРАД-ПАЛИЛУЛА": "ПАЛИЛУЛА (БЕОГРАД)",
    "НИШ-ПАЛИЛУЛА": "ПАЛИЛУЛА (НИШ)",
    "ВРАЊЕ-ВРАЊСКА БАЊА": "ВРАЊЕ",
    "УПРАВА ЗА ИЗВРШЕЊЕ ЗАВОДСКИХ САНКЦИЈА": "ЗАВОДИ ЗА ИЗВРШЕЊЕ КРИВИЧНИХ САНКЦИЈА",
}
_CITY_PREFIXES = ("БЕОГРАД-", "НИШ-", "ПОЖАРЕВАЦ-", "УЖИЦЕ-")


def _key(name: str) -> str:
    value = " ".join(to_cyrillic(name).upper().replace("–", "-").split())
    for prefix in ("ГРАДСКА ОПШТИНА ", "ГРАД "):
        if value.startswith(prefix):
            value = value[len(prefix):]
    value = _ALIASES.get(value, value)
    for prefix in _CITY_PREFIXES:
        if value.startswith(prefix):
            value = value[len(prefix):]
            break
    if value.endswith(" - ГРАД"):
        value = value[:-7]
    return value.strip()


@lru_cache(maxsize=1)
def _data() -> dict:
    raw = json.loads(DATA.read_text(encoding="utf-8"))
    municipalities: dict[str, dict] = {}
    for name, stations in raw["municipalities"].items():
        municipalities.setdefault(_key(name), {}).update(stations)
    return {**raw, "municipalities": municipalities}


def station_location(municipality: str, station_no: str) -> dict[str, str] | None:
    values = _data()["municipalities"].get(_key(municipality), {}).get(str(station_no))
    if values is None:
        return None
    return {"settlement": values[0], "address": values[1]}


def station_location_source() -> dict[str, str]:
    data = _data()
    return {key: data[key] for key in ("source", "revision", "retrieved")}
