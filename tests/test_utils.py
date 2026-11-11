from bs4 import BeautifulSoup

import pytest

import mechanicalsoup
from mechanicalsoup.utils import get_form_owner, iter_form_controls


def test_LinkNotFoundError():
    with pytest.raises(mechanicalsoup.LinkNotFoundError):
        raise mechanicalsoup.utils.LinkNotFoundError
    with pytest.raises(Exception):
        raise mechanicalsoup.utils.LinkNotFoundError


PAGE = """
<html><body>
  <form id="f1">
    <input name="a"/>
    <div><select name="s"><option>x</option></select></div>
  </form>
  <form id="f2">
    <textarea name="t">txt</textarea>
  </form>
  <input name="bound1" form="f1"/>
  <button name="bound2" form="f2">go</button>
  <input name="dangling" form="missing"/>
  <input name="empty" form=""/>
  <input name="free"/>
</body></html>
"""


def _soup():
    return BeautifulSoup(PAGE, "lxml")


def test_get_form_owner_by_ancestor():
    soup = _soup()
    form = soup.find("form", id="f1")
    assert get_form_owner(soup.find("input", attrs={"name": "a"})) is form
    # A deeply nested control still resolves to its nearest ancestor form.
    assert get_form_owner(soup.find("select", attrs={"name": "s"})) is form


def test_get_form_owner_by_form_attribute():
    soup = _soup()
    f1 = soup.find("form", id="f1")
    f2 = soup.find("form", id="f2")
    assert get_form_owner(
        soup.find("input", attrs={"name": "bound1"})) is f1
    assert get_form_owner(
        soup.find("button", attrs={"name": "bound2"})) is f2


def test_get_form_owner_dangling_and_empty_ids():
    soup = _soup()
    # A reference to a missing id (including the empty string) means the
    # control has no owner, even if nested inside a form.
    assert get_form_owner(
        soup.find("input", attrs={"name": "dangling"})) is None
    assert get_form_owner(
        soup.find("input", attrs={"name": "empty"})) is None
    # A control outside any form without a form attribute has no owner.
    assert get_form_owner(
        soup.find("input", attrs={"name": "free"})) is None


def test_get_form_owner_tracks_live_attribute_changes():
    soup = _soup()
    f2 = soup.find("form", id="f2")
    bound = soup.find("input", attrs={"name": "bound1"})
    assert get_form_owner(bound) is soup.find("form", id="f1")

    bound["form"] = "f2"
    assert get_form_owner(bound) is f2

    del bound["form"]
    # No ancestor form: the control now sits directly under body.
    assert get_form_owner(bound) is None


def test_iter_form_controls_document_order_and_membership():
    soup = _soup()
    f1 = soup.find("form", id="f1")
    f1_controls = list(iter_form_controls(f1))
    # Document order across the whole page; external bound controls are
    # interleaved at their position, not appended to the form.
    assert [t.get("name") for t in f1_controls] == ["a", "s", "bound1"]

    f2 = soup.find("form", id="f2")
    f2_controls = list(iter_form_controls(f2))
    assert [t.get("name") for t in f2_controls] == ["t", "bound2"]

    # The dangling, empty-id and free controls belong to no form.
    all_owned = {t.get("name")
                 for form in soup.find_all("form")
                 for t in iter_form_controls(form)}
    assert "dangling" not in all_owned
    assert "empty" not in all_owned
    assert "free" not in all_owned


def test_iter_form_controls_does_not_mutate_tree():
    soup = _soup()
    before = str(soup)
    list(iter_form_controls(soup.find("form", id="f1")))
    list(iter_form_controls(soup.find("form", id="f2")))
    assert str(soup) == before
    # The externally bound control's parent is still body.
    assert soup.find("input", attrs={"name": "bound1"}).parent.name == "body"


def test_non_form_id_target_resolves_to_no_owner():
    """A form attribute pointing at a non-<form> element id is dangling."""
    soup = BeautifulSoup("""
    <form id="real"><input name="x" form="decoy"/></form>
    <div id="decoy"></div>
    """, "lxml")
    assert get_form_owner(soup.find("input", attrs={"name": "x"})) is None
    assert list(iter_form_controls(soup.find("form", id="real"))) == []
