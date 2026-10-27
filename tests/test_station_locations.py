import json
import re

import pytest

from rg2watch.station_locations import station_location


def test_station_location_matches_rik_municipality_names_and_station_numbers():
    assert station_location("Градска општина Гроцка", "7") == {
        "settlement": "ВИНЧА",
        "address": "ВИНЧА, ЈОВАНА ЈОВАНОВИЋА ЗМАЈА 1",
    }
    assert station_location("Аранђеловац", "31") == {
        "settlement": "КОПЉАРЕ",
        "address": "КОПЉАРИ",
    }


def test_station_location_handles_city_district_aliases_and_unknown_stations():
    assert station_location("Градска општина Палилула (Београд)", "1")["settlement"]
    assert station_location("Градска општина Палилула (Ниш)", "1")["settlement"]
    assert station_location("Град Врање", "100")["settlement"] == "ВРАЊСКА БАЊА"
    assert station_location("Непостојећа општина", "1") is None
    assert station_location("Градска општина Гроцка", "999") is None


def test_foreign_station_location_uses_city_and_country_without_exposing_street_as_settlement():
    assert station_location("Иностранство", "79") == {
        "settlement": "ПАРИЗ · ФРАНЦУСКА",
        "address": "123 RUE SAINT-MARTIN, 75004 ПАРИЗ, ФРАНЦУСКА",
    }
    assert station_location("Иностранство", "11") == {
        "settlement": "БАЊА ЛУКА · БОСНА И ХЕРЦЕГОВИНА",
        "address": "ВОЈВОДЕ РАДОМИРА ПУТНИКА 2, 78000 БАЊА ЛУКА, БОСНА И ХЕРЦЕГОВИНА",
    }


@pytest.mark.parametrize("number,expected", [
    ("1", "МЕЛБУРН · АУСТРАЛИЈА"),
    ("10", "САРАЈЕВО · БОСНА И ХЕРЦЕГОВИНА"),
    ("12", "БОСНА И ХЕРЦЕГОВИНА"),
    ("52", "BEGGEN · ЛУКСЕМБУРГ"),
    ("53", "FLORIANA · МАЛТА"),
    ("71", "ВАШИНГТОН · СЈЕДИЊЕНЕ АМЕРИЧКЕ ДРЖАВЕ"),
    ("77", "ЛОНДОН · УЈЕДИЊЕНО КРАЉЕВСТВО"),
    ("92", "МАДРИД · ШПАНИЈА"),
])
def test_foreign_station_location_handles_source_address_variants(number, expected):
    assert station_location("Иностранство", number)["settlement"] == expected


@pytest.mark.parametrize("municipality", ["Градска општина Гроцка", "БЕОГРАД-ГРОЦКА",
                                          "Gradska opština Grocka"])
def test_station_coordinates_are_available_offline_with_existing_name_matching(municipality):
    from rg2watch.station_locations import station_coordinates

    assert station_coordinates(municipality, "11") == {
        "latitude": 44.664, "longitude": 20.59089, "location_confidence": 0,
    }


@pytest.mark.parametrize("number,latitude,longitude,confidence", [
    ("42", 43.46271, 21.06587, 1),
    ("1", 43.42095, 20.80147, 2),
])
def test_station_coordinates_preserve_unverified_and_approximate_flags(
        number, latitude, longitude, confidence):
    from rg2watch.station_locations import station_coordinates

    assert station_coordinates("Александровац", number) == {
        "latitude": latitude, "longitude": longitude, "location_confidence": confidence,
    }


def test_unknown_stations_and_missing_coordinates_do_not_become_zero_zero():
    from rg2watch.station_locations import station_coordinates

    assert station_coordinates("Непостојећа општина", "1") is None
    assert station_coordinates("Градска општина Гроцка", "999") is None
    for number in ("13", "85", "86"):
        assert station_coordinates("Иностранство", number) is None


def test_coordinate_snapshot_is_complete_and_records_its_source_and_schema():
    from rg2watch.station_locations import COORDINATES

    data = json.loads(COORDINATES.read_text(encoding="utf-8"))
    assert data["source"].startswith("https://bm.itblokada.org/map_data.json")
    assert data["revision"] == "6a91046e"
    assert re.fullmatch(r"\d{4}-\d\d-\d\d", data["retrieved"])
    assert re.fullmatch(r"[0-9a-f]{64}", data["sha256"])
    assert data["fields"] == ["latitude", "longitude", "location_confidence"]
    assert data["confidence_labels"] == {"0": "confident", "1": "unverified", "2": "approximate"}
    rows = [row for stations in data["municipalities"].values() for row in stations.values()]
    assert len(rows) == 8392
    assert sum(row[0] is not None for row in rows) == 8389
    for latitude, longitude, confidence in rows:
        assert (latitude is None) == (longitude is None)
        if latitude is not None:
            assert -90 <= latitude <= 90 and -180 <= longitude <= 180
            assert (latitude, longitude) != (0, 0)
        assert confidence in (0, 1, 2)
