from rg2watch.findings import findings_for
from rg2watch.models import Doc, Event


def doc(t: int, label: str = "x") -> Doc:
    return Doc("Град Зајечар", "3", "ОШ Тест", label, t, "https://x/a.pdf", "2025-11-02T21:05")


def test_rg9_high():
    [f] = findings_for([Event("added", doc(9))])
    assert (f.rule, f.severity, f.summary) == ("D1", "HIGH", "RG-9 published")


def test_rg7_medium():
    [f] = findings_for([Event("added", doc(7)), ])
    assert (f.rule, f.severity) == ("D1", "MEDIUM")
    [r] = findings_for([Event("added", doc(10))])
    assert (r.rule, r.severity) == ("D1", "MEDIUM")


def test_replaced_rg2_high():
    [f] = findings_for([Event("replaced", doc(6), "https://x/old.pdf")])
    assert (f.rule, f.severity, f.summary) == ("D2", "HIGH", "RG-2 file replaced")
    [g] = findings_for([Event("replaced", doc(12), "o")])
    assert g.severity == "MEDIUM"


def test_added_rg2_no_finding():
    assert findings_for([Event("added", doc(6))]) == []


def test_removed_d4():
    [f] = findings_for([Event("removed", doc(6))])
    assert (f.rule, f.severity, f.summary) == ("D4", "MEDIUM", "document removed from listing")


def test_d2_carries_previous_url():
    from rg2watch.models import Doc, Event
    d = Doc("M", "1", "S", "x", 6, "https://x/new.pdf", "2025-06-08T23:00")
    (f,) = findings_for([Event("replaced", d, previous_url="https://x/old.pdf")])
    assert f.rule == "D2" and f.previous_url == "https://x/old.pdf"
