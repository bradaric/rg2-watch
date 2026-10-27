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
