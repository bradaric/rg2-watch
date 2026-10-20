import pytest

from rg2watch.names import MunicipalityError, resolve_municipalities, to_cyrillic

NAMES = {"Нови Сад": "1", "Нови Београд": "2", "Београд": "3", "Љиг": "4", "Књажевац": "5",
         "Ђердап": "6", "Џеп": "7", "Шид": "8", "Чачак": "9", "Ћуприја": "10", "Жабаљ": "11"}


def test_to_cyrillic_digraphs_and_diacritics():
    assert to_cyrillic("Ljig Njegoševo Džep") == "љиг његошево џеп"
    assert to_cyrillic("Šid Čačak Ćuprija Žabalj Đerdap") == "шид чачак ћуприја жабаљ ђердап"
    assert to_cyrillic("Нови Сад") == "нови сад"


@pytest.mark.parametrize("query,name", [("ljig", "Љиг"), ("KNJAŽEVAC", "Књажевац"), ("шид", "Шид"),
                                        ("džep", "Џеп"), ("cuprija", None), ("Ćuprija", "Ћуприја"),
                                        ("novi s", "Нови Сад"), ("ђердап", "Ђердап")])
def test_resolve_single(query, name):
    if name is None:
        with pytest.raises(MunicipalityError):
            resolve_municipalities(NAMES, [query])
    else:
        assert resolve_municipalities(NAMES, [query]) == {name: NAMES[name]}


def test_exact_beats_substring():
    assert resolve_municipalities(NAMES, ["Beograd"]) == {"Београд": "3"}


def test_several_names_and_blank_ignored():
    got = resolve_municipalities(NAMES, ["Ljig", " ", "Šid"])
    assert got == {"Љиг": "4", "Шид": "8"}


def test_ambiguous_lists_candidates():
    with pytest.raises(MunicipalityError) as e:
        resolve_municipalities(NAMES, ["novi"])
    assert e.value.candidates == ["Нови Београд", "Нови Сад"]


def test_no_match_and_empty():
    with pytest.raises(MunicipalityError) as e:
        resolve_municipalities(NAMES, ["Sid2"])
    assert "Шид" in e.value.candidates
    with pytest.raises(MunicipalityError):
        resolve_municipalities(NAMES, [" "])


def test_watch_only_unknown_or_ambiguous_exits_2(tmp_path, capsys):
    from rg2watch.cli import main
    from rg2watch.models import Election
    from rg2watch.store import Store

    db = tmp_path / "w.sqlite"
    store = Store(db)
    store.upsert_election(Election("1", "2", "3", "T", "http://127.0.0.1:9"))
    store.save_municipalities("1", {i: n for n, i in NAMES.items()})
    store.close()
    common = ["--parent", "1", "--db", str(db)]
    assert main(["watch", *common, "--only", "novi"]) == 2
    assert "Нови Сад" in capsys.readouterr().err
    assert main(["watch", *common, "--only", "Ljig,Nowhere"]) == 2


def test_nfc_whitespace_and_precomposed_digraphs():
    import unicodedata
    assert to_cyrillic("Lj́ig") != ""  # decomposed input does not crash
    assert to_cyrillic(unicodedata.normalize("NFD", "Šid")) == "шид"
    assert to_cyrillic("  Novi \t Sad ") == "нови сад"
    assert to_cyrillic("ǉig ǌegoš ǆep Ǉ") == "љиг његош џеп љ"


def test_only_empty_is_an_error(tmp_path):
    from rg2watch.cli import main
    assert main(["watch", "--parent", "1", "--db", str(tmp_path / "x.sqlite"), "--only", ""]) == 2
