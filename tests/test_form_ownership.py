"""Tests for form ownership resolution.

These tests cover the behavior expected from a real browser when a page
contains several forms and controls located outside their ``<form>``
element:

* selecting a form never moves or copies DOM nodes;
* a control with a ``form`` attribute belongs to the form it points at,
  a control without one belongs to its nearest ancestor form, and a
  reference to a non-existent id means the control has no owner;
* changing a ``form`` attribute takes effect on the next operation;
* filling fields, ticking boxes, choosing the submit button and
  submitting all agree on which controls belong to the form;
* document order and same-name multi-value fields are preserved;
* disabled controls (including the disabled-fieldset / first-legend
  rules) are excluded based on the original DOM;
* GET and POST requests carry the ordered fields actually submitted.
"""
import io
from urllib.parse import parse_qsl, urlparse

import pytest
import requests_mock
import setpath  # noqa:F401, must come before 'import mechanicalsoup'
from bs4 import BeautifulSoup

import mechanicalsoup
from mechanicalsoup.utils import get_form_owner, iter_form_controls


def make_browser(html):
    """Build a StatefulBrowser on a mock page whose posted data is
    captured for ordered-field assertions."""
    adapter = requests_mock.Adapter()
    browser = mechanicalsoup.StatefulBrowser(
        requests_adapters={"https://": adapter})
    captured = []

    def record(request, context):
        if request.method == "GET":
            fields = parse_qsl(urlparse(request.url).query)
        else:
            fields = parse_qsl(request.text)
        captured.append({"method": request.method, "fields": fields,
                         "body": request.text,
                         "content_type": request.headers.get(
                             "Content-Type", "")})
        return "ok"

    adapter.register_uri("POST", "https://example.com/post", text=record,
                         headers={"Content-Type": "text/html"})
    adapter.register_uri("GET", "https://example.com/get", text=record,
                         headers={"Content-Type": "text/html"})
    browser.open_fake_page(html, url="https://example.com/page")
    return browser, captured


# A page with two forms plus controls located outside both forms. The
# external input/select/textarea/button are shared test material.
two_forms_page = """
<html><body>
  <form id="f1" method="post" action="https://example.com/post">
    <input type="hidden" name="a" value="1"/>
    <input type="hidden" name="dup" value="f1-first"/>
    <input type="text" name="inside1" value="keep"/>
  </form>
  <form id="f2" method="get" action="https://example.com/get">
    <input type="hidden" name="b" value="2"/>
  </form>
  <input type="text" name="external1" value="ext1" form="f1"/>
  <select name="external_select" form="f1">
    <option value="s1" selected="selected">S1</option>
    <option value="s2">S2</option>
  </select>
  <textarea name="external_text" form="f1">hello</textarea>
  <input type="hidden" name="dup" value="external-middle" form="f1"/>
  <input type="text" name="other_form" value="x" form="f2"/>
  <input type="text" name="no_owner" value="x" form="does-not-exist"/>
</body></html>
"""


def test_select_form_does_not_move_or_copy_dom_nodes():
    """select_form must leave the document tree untouched."""
    browser, _ = make_browser(two_forms_page)
    page_before = str(browser.page)
    parents_before = {
        tag.get("name"): tag.parent.name
        for tag in browser.page.find_all(("input", "select", "textarea"))
        if tag.get("name")}

    browser.select_form("#f1")
    browser.select_form("#f2")
    browser.select_form("#f1")

    assert str(browser.page) == page_before
    parents_after = {
        tag.get("name"): tag.parent.name
        for tag in browser.page.find_all(("input", "select", "textarea"))
        if tag.get("name")}
    assert parents_after == parents_before
    # External controls are direct children of body, not of a form.
    assert parents_before["external1"] == "body"
    assert parents_before["other_form"] == "body"


def test_two_forms_switching_and_ordered_post():
    """Switching between forms changes which fields are submitted, in
    document order, with same-name values preserved."""
    browser, captured = make_browser(two_forms_page)

    browser.select_form("#f1")
    # Filling an external control through the form's API must work.
    browser["external1"] = "filled"
    browser.submit_selected(update_state=False)

    assert captured[0]["method"] == "POST"
    # Document order: the three "dup" controls appear at their positions.
    assert captured[0]["fields"] == [
        ("a", "1"),
        ("dup", "f1-first"),
        ("inside1", "keep"),
        ("external1", "filled"),
        ("external_select", "s1"),
        ("external_text", "hello"),
        ("dup", "external-middle"),
    ]

    # Now switch to the second form: the first form's controls (including
    # the externally bound ones) must not be submitted. GET this time.
    browser.select_form("#f2")
    browser["other_form"] = "y"
    browser.submit_selected(update_state=False)

    assert captured[1]["method"] == "GET"
    assert captured[1]["fields"] == [("b", "2"), ("other_form", "y")]

    # And back to the first form: results must be stable across switches.
    browser.select_form("#f1")
    browser.submit_selected(update_state=False)
    assert captured[2]["fields"] == [
        ("a", "1"),
        ("dup", "f1-first"),
        ("inside1", "keep"),
        ("external1", "filled"),
        ("external_select", "s1"),
        ("external_text", "hello"),
        ("dup", "external-middle"),
    ]


def test_control_with_missing_form_id_has_no_owner():
    browser, _ = make_browser(two_forms_page)
    orphan = browser.page.find("input", form="does-not-exist")
    assert get_form_owner(orphan) is None
    browser.select_form("#f1")
    assert all(control is not orphan
               for control in iter_form_controls(browser.form.form))
    # It cannot be filled through either form.
    with pytest.raises(mechanicalsoup.LinkNotFoundError):
        browser["no_owner"] = "z"


fieldset_page = """
<html><body>
  <form id="fs" method="post" action="https://example.com/post">
    <input type="hidden" name="enabled" value="1"/>
    <fieldset disabled>
      <legend><input type="hidden" name="in_legend" value="legend"/></legend>
      <input type="hidden" name="disabled_plain" value="d"/>
      <fieldset>
        <input type="hidden" name="disabled_nested" value="dn"/>
      </fieldset>
    </fieldset>
  </form>
  <fieldset disabled>
    <input type="hidden" name="external_disabled" value="ed" form="fs"/>
  </fieldset>
  <input type="hidden" name="external_enabled" value="ee" form="fs"/>
</body></html>
"""


def test_disabled_fieldset_and_first_legend_exception():
    """Disabled controls are judged from the original DOM, including the
    first-<legend> exception and external controls inside a disabled
    fieldset."""
    browser, captured = make_browser(fieldset_page)
    dom_before = str(browser.page)
    browser.select_form("#fs")
    browser.submit_selected(update_state=False)

    assert captured[0]["fields"] == [
        ("enabled", "1"),
        ("in_legend", "legend"),
        ("external_enabled", "ee"),
    ]
    assert str(browser.page) == dom_before


external_buttons_page = """
<html><body>
  <form id="bf" method="post" action="https://example.com/post">
    <input type="hidden" name="f" value="1"/>
  </form>
  <input type="submit" name="outside_submit" value="Go" form="bf"/>
  <button type="submit" name="outside_button" value="Press" form="bf">
    Press
  </button>
  <button type="reset" name="outside_reset" form="bf">Reset</button>
  <input type="submit" name="orphan_submit" value="X"
         form="missing-form"/>
</body></html>
"""


def test_external_submit_buttons():
    """External submit controls are selectable; reset buttons and
    controls without an owner are not."""
    browser, captured = make_browser(external_buttons_page)

    # Default: first valid submit in document order.
    browser.select_form("#bf")
    assert browser.form.choose_submit(None).get("name") == "outside_submit"
    browser.submit_selected(update_state=False)
    assert captured[0]["fields"] == [
        ("f", "1"), ("outside_submit", "Go")]

    # Choose the external <button> by name and by tag.
    browser.select_form("#bf")
    browser.form.choose_submit("outside_button")
    browser.submit_selected(update_state=False)
    assert captured[1]["fields"] == [
        ("f", "1"), ("outside_button", "Press")]

    button_tag = browser.page.find("button", attrs={"name": "outside_button"})
    browser.select_form("#bf")
    browser.form.choose_submit(button_tag)
    browser.submit_selected(update_state=False)
    assert captured[2]["fields"] == [
        ("f", "1"), ("outside_button", "Press")]

    # The orphan submit and the reset button cannot be chosen.
    browser.select_form("#bf")
    with pytest.raises(mechanicalsoup.LinkNotFoundError):
        browser.form.choose_submit("orphan_submit")
    with pytest.raises(mechanicalsoup.LinkNotFoundError):
        browser.form.choose_submit("outside_reset")

    # choose_submit(False) submits no button at all.
    browser.select_form("#bf")
    browser.form.choose_submit(False)
    browser.submit_selected(update_state=False)
    assert captured[3]["fields"] == [("f", "1")]


reattribution_page = """
<html><body>
  <form id="g1" method="post" action="https://example.com/post">
    <input type="hidden" name="only_g1" value="1"/>
  </form>
  <form id="g2" method="post" action="https://example.com/post">
    <input type="hidden" name="only_g2" value="2"/>
  </form>
  <input type="hidden" name="movable" value="m" form="g1"/>
  <input type="hidden" name="ancestor">
  <form id="g3" method="post" action="https://example.com/post">
    <input type="hidden" name="in_g3" value="3"/>
  </form>
</body></html>
"""


def test_form_attribute_change_takes_effect_next_operation():
    browser, captured = make_browser(reattribution_page)
    movable = browser.page.find("input", attrs={"name": "movable"})

    browser.select_form("#g1")
    assert get_form_owner(movable) is browser.form.form
    browser.submit_selected(update_state=False)
    assert captured[0]["fields"] == [
        ("only_g1", "1"), ("movable", "m")]

    # Re-attribute the control to the other form: no DOM move, and the
    # very next operation sees the new owner.
    movable["form"] = "g2"
    assert get_form_owner(movable) is browser.page.find("form", id="g2")

    browser.select_form("#g1")
    browser.submit_selected(update_state=False)
    assert captured[1]["fields"] == [("only_g1", "1")]

    browser.select_form("#g2")
    browser.submit_selected(update_state=False)
    assert captured[2]["fields"] == [
        ("only_g2", "2"), ("movable", "m")]

    # Dropping the attribute makes the nearest ancestor form the owner;
    # here there is no ancestor form, so the control becomes ownerless.
    del movable["form"]
    assert get_form_owner(movable) is None
    browser.select_form("#g2")
    with pytest.raises(mechanicalsoup.LinkNotFoundError):
        browser["movable"] = "x"


def test_nearest_ancestor_form_without_form_attribute():
    html = """
    <html><body>
      <form id="outer" method="post" action="https://example.com/post">
        <input type="hidden" name="outer_field" value="o"/>
        <div>
          <input type="hidden" name="nested_field" value="n"/>
        </div>
      </form>
      <input type="hidden" name="truly_external" value="e"/>
    </body></html>
    """
    browser, captured = make_browser(html)
    nested = browser.page.find("input", attrs={"name": "nested_field"})
    external = browser.page.find("input", attrs={"name": "truly_external"})
    assert get_form_owner(nested) is browser.page.find("form", id="outer")
    assert get_form_owner(external) is None

    browser.select_form("#outer")
    browser.submit_selected(update_state=False)
    assert captured[0]["fields"] == [
        ("outer_field", "o"), ("nested_field", "n")]


def test_radio_and_checkbox_families_include_external_controls():
    html = """
    <html><body>
      <form id="cb" method="post" action="https://example.com/post">
        <input type="checkbox" name="box" value="in"/>
      </form>
      <input type="checkbox" name="box" value="out" form="cb"/>
      <input type="radio" name="rad" value="a" form="cb"/>
      <input type="radio" name="rad" value="b" form="cb"/>
    </body></html>
    """
    browser, captured = make_browser(html)
    browser.select_form("#cb")

    # The external checkbox belongs to the same family and is found by
    # the same API used for nested controls.
    browser.form.set_checkbox({"box": "out"})
    browser.form.set_radio({"rad": "b"})
    browser.submit_selected(update_state=False)
    assert captured[0]["fields"] == [
        ("box", "out"), ("rad", "b")]


def test_get_and_post_ordered_fields():
    """Both methods must serialize fields in document order."""
    html_post = """
    <form method="post" action="https://example.com/post">
      <input type="hidden" name="p1" value="1"/>
      <input type="hidden" name="p2" value="2"/>
      <input type="hidden" name="p1" value="3"/>
    </form>"""
    browser, captured = make_browser(html_post)
    browser.select_form()
    browser.submit_selected(update_state=False)
    assert captured[0]["fields"] == [
        ("p1", "1"), ("p2", "2"), ("p1", "3")]

    html_get = html_post.replace("post", "get").replace(
        'method="post"', 'method="get"')
    browser, captured = make_browser(html_get)
    browser.select_form()
    browser.submit_selected(update_state=False)
    assert captured[0]["method"] == "GET"
    assert captured[0]["fields"] == [
        ("p1", "1"), ("p2", "2"), ("p1", "3")]


def test_multipart_upload_with_external_file_input():
    """File uploads keep working, including a file input bound to the
    form through a form attribute."""
    html = """
    <html><body>
      <form id="up" method="post" action="https://example.com/post"
            enctype="multipart/form-data">
        <input type="text" name="plain" value="v"/>
      </form>
      <input type="file" name="uploaded" form="up"/>
    </body></html>
    """
    browser, captured = make_browser(html)
    browser.select_form("#up")
    browser["uploaded"] = io.BytesIO(b"file bytes")
    browser.submit_selected(update_state=False)

    assert captured[0]["method"] == "POST"
    assert "multipart/form-data" in captured[0]["content_type"]
    # requests-mock stores the multipart body verbatim.
    assert b"file bytes" in captured[0]["body"].encode("latin-1")
    assert b'name="uploaded"' in captured[0]["body"].encode("latin-1")
    assert b'name="plain"' in captured[0]["body"].encode("latin-1")


def test_standalone_form_fragment_still_supported():
    """A Form built directly from a parsed <form> fragment (the
    pre-SelectForm API) must keep working unchanged."""
    soup = BeautifulSoup("""
    <form method="post" action="https://example.com/post">
      <input type="text" name="frag" value="old"/>
      <select name="choice">
        <option value="a">A</option>
        <option value="b" selected="selected">B</option>
      </select>
    </form>""", "lxml")
    form = mechanicalsoup.Form(soup.form)
    form["frag"] = "new"

    browser, captured = make_browser("<html></html>")
    mechanicalsoup.Browser.submit(browser, form, url="https://example.com/x")
    assert captured[0]["fields"] == [
        ("frag", "new"), ("choice", "b")]


def test_form_built_from_non_form_tag_keeps_legacy_behavior():
    """Constructing a Form from a non-<form> tag (deprecated, but still
    supported) must keep finding the nested controls."""
    soup = BeautifulSoup("""
    <div>
      <input type="hidden" name="d1" value="1"/>
      <textarea name="d2">txt</textarea>
    </div>""", "lxml")
    with pytest.warns(FutureWarning):
        form = mechanicalsoup.Form(soup.div)
    form.set_input({"d1": "2"})
    assert [t.get("name") for t in iter_form_controls(form.form)] == [
        "d1", "d2"]


def test_empty_form_attribute_has_no_owner():
    """An empty form attribute references no id, so per the HTML spec the
    control has no form owner -- even when nested inside a form."""
    html = """
    <html><body>
      <form id="h" method="post" action="https://example.com/post">
        <input type="hidden" name="inside" value="i" form=""/>
      </form>
      <input type="hidden" name="outside" value="o" form=""/>
    </body></html>
    """
    browser, captured = make_browser(html)
    inside = browser.page.find("input", attrs={"name": "inside"})
    outside = browser.page.find("input", attrs={"name": "outside"})
    assert get_form_owner(inside) is None
    assert get_form_owner(outside) is None

    browser.select_form("#h")
    # The nested control is excluded because it opted out of ownership.
    browser.submit_selected(update_state=False)
    assert captured[0]["fields"] == []


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
