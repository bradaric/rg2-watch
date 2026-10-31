"""Read and normalize RIK's national official-results web feed."""
import json
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

import httpx

from .models import Election


RESULTS_PATH = "/542645/rezultati/"


class OfficialResultsError(ValueError):
    pass


def official_results_supported(election: Election) -> bool:
    """Whether RIK exposes this election through the national official-results feed."""
    return election.election_type == "2"


def _integer(value) -> int:
    if isinstance(value, bool):
        raise OfficialResultsError("boolean where RIK result number was expected")
    if isinstance(value, int):
        return value
    if not isinstance(value, str):
        raise OfficialResultsError("invalid RIK result number")
    compact = re.sub(r"[.\s\u00a0]", "", value)
    if not compact.isdigit():
        raise OfficialResultsError(f"invalid RIK result number: {value!r}")
    return int(compact)


def _chart_pair(body: dict, key: str) -> tuple[int, int]:
    try:
        values = body[key]["data"]["datasets"][0]["data"]
    except (KeyError, IndexError, TypeError) as exc:
        raise OfficialResultsError(f"invalid RIK {key}") from exc
    if not isinstance(values, list) or len(values) != 2:
        raise OfficialResultsError(f"invalid RIK {key}")
    return _integer(values[0]), _integer(values[1])


def _normalize(body: dict, source: str, built: str) -> dict:
    if not isinstance(body, dict):
        raise OfficialResultsError("RIK results response is not an object")
    try:
        summary, table = body["stat_sum_numbers"], body["table_data"]
        title, rik_at = summary["election_title"], summary["datetime"]
    except (KeyError, TypeError) as exc:
        raise OfficialResultsError("invalid RIK results response") from exc
    if not isinstance(summary, dict) or not isinstance(table, list):
        raise OfficialResultsError("invalid RIK results response")
    not_voted, voted = _chart_pair(body, "processed_config")
    valid, invalid = _chart_pair(body, "sum_config")
    lists = []
    for row in table:
        try:
            no, name, votes, percent = (row["count_number"], row["list_name"],
                                        row["won_number"], row["won_percent"])
        except (KeyError, TypeError) as exc:
            raise OfficialResultsError("invalid RIK list result") from exc
        if (not isinstance(no, int) or isinstance(no, bool) or not isinstance(name, str)
                or not isinstance(percent, (int, float)) or isinstance(percent, bool)
                or not 0 <= percent <= 100):
            raise OfficialResultsError("invalid RIK list result")
        name = re.sub(rf"^\s*{no}\.\s+", "", name, count=1)
        lists.append({"no": no, "name": name, "votes": _integer(votes),
                      "share": round(percent / 100, 6)})
    processed_registered = not_voted + voted
    return {
        "built": built, "rik_at": rik_at, "title": title, "source": source,
        "stations": {"processed": _integer(summary["processed_stations"]),
                     "total": _integer(summary["number_of_stations"])},
        "voters": {"registered": _integer(summary["total_voters"]),
                   "processed_registered": processed_registered, "voted": voted,
                   "turnout": voted / processed_registered if processed_registered else None},
        "ballots": {"valid": valid, "invalid": invalid},
        "lists": lists,
    }


class OfficialResultsClient:
    def __init__(self, election: Election, http: httpx.Client, timeout_s: float = 20.0,
                 now: Callable[[], str] | None = None):
        self.election, self.http, self.timeout_s = election, http, timeout_s
        self.now = now or (lambda: datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))
        self.source = election.base_url.rstrip("/") + RESULTS_PATH
        self._round: str | None = None
        match = re.search(r"\b(20\d{2})\b", election.title)
        self.year = match.group(1) if match else None

    def _post(self, path: str, data: dict) -> dict:
        response = self.http.post(
            self.election.base_url.rstrip("/") + path, data=data, timeout=self.timeout_s,
            headers={"Referer": self.source, "X-Requested-With": "XMLHttpRequest"})
        response.raise_for_status()
        try:
            body = response.json()
        except ValueError as exc:
            raise OfficialResultsError("RIK response is not JSON") from exc
        if not isinstance(body, dict):
            raise OfficialResultsError("RIK response is not an object")
        return body

    def _discover_round(self) -> str:
        if self.year is None:
            raise OfficialResultsError("election title has no year for RIK results discovery")
        response = self.http.get(self.source, timeout=self.timeout_s)
        response.raise_for_status()
        body = self._post("/get-elections/", {"election_type": "2"})
        rounds = body.get("rounds")
        if not isinstance(rounds, dict):
            raise OfficialResultsError("invalid RIK results rounds")
        matches = [(str(key), value) for key, value in rounds.items()
                   if isinstance(value, str) and self.year in value]
        if not matches:
            raise OfficialResultsError(f"no RIK results round for {self.year}")
        if len(matches) != 1:
            raise OfficialResultsError(f"multiple RIK results rounds for {self.year}")
        return matches[0][0]

    def fetch(self) -> dict:
        for attempt in range(2):
            if self._round is None:
                self._round = self._discover_round()
            try:
                body = self._post("/get_results/", {
                    "should_update_pies": "1", "type": "2", "election_round": self._round,
                    "region": "0", "municipality": "0", "election_station": "0",
                })
                break
            except httpx.HTTPStatusError as exc:
                if attempt or exc.response.status_code not in (401, 403):
                    raise
                self._round = None
        return _normalize(body, self.source, self.now())


def write_official_results(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=True, separators=(",", ":"))
        os.chmod(tmp, 0o644)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
