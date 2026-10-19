from dataclasses import dataclass
from typing import Literal

DOC_TYPES: dict[int, str] = {
    6: "RG-2", 7: "RG-7", 8: "RG-8", 9: "RG-9", 10: "RG-2-R", 11: "RG-6", 12: "RG-11",
    13: "ISPRAVKA", 14: "RG-7-SUD", 15: "RG-2-R2", 16: "RG-2-R3", 17: "RG-11-R2",
    18: "RG-11-R3", 19: "RG-7-1", 20: "RG-7-2", 21: "RG-7-3", 22: "RG-7-4", 23: "RG-7-1-R",
    24: "RG-7-2-R", 25: "RG-7-3-R", 26: "RG-7-4-R", 27: "RG-8-R", 28: "RG-9-R",
    29: "PRIMEDBA", 30: "PRIMEDBA-R1", 31: "PRIMEDBA-R2",
}

RARE_TYPES: tuple[int, ...] = (
    7, 8, 9, 10, 14, 15, 16, 19, 20, 21, 22, 23, 24, 25, 26, 27, 28, 29, 30, 31,
)


@dataclass(frozen=True)
class Election:
    parent: str
    round: str
    election_type: str
    title: str
    base_url: str


@dataclass(frozen=True)
class Doc:
    municipality: str
    station_no: str
    station_name: str
    label: str
    doc_type: int | None
    url: str
    uploaded_at: str

    @property
    def station_key(self) -> str:
        return f"{self.municipality}|{self.station_no}"


@dataclass(frozen=True)
class ListingPage:
    docs: list[Doc]
    records: int
    max_page: int


@dataclass(frozen=True)
class Filters:
    doc_type: int | None = None
    municipality_id: str | None = None
    station_no_id: str | None = None


@dataclass(frozen=True)
class Event:
    kind: Literal["added", "replaced", "removed"]
    doc: Doc
    previous_url: str | None = None


@dataclass(frozen=True)
class Finding:
    rule: str
    severity: Literal["HIGH", "MEDIUM", "LOW"]
    doc: Doc
    summary: str
