class LinkNotFoundError(Exception):
    """Exception raised when mechanicalsoup fails to find something.

    This happens in situations like (non-exhaustive list):

    * :func:`~mechanicalsoup.StatefulBrowser.find_link` is called, but
      no link is found.

    * The browser was configured with raise_on_404=True and a 404
      error is triggered while browsing.

    * The user tried to fill-in a field which doesn't exist in a form
      (e.g. browser["name"] = "val" with browser being a
      StatefulBrowser).
    """
    pass


def is_multipart_file_upload(form, tag):
    return (
        form.get("enctype", "") == "multipart/form-data" and
        tag.get("type", "").lower() == "file"
    )


def is_disabled(tag):
    """Return whether a form control is disabled per the HTML specification.

    A control is disabled if it carries a ``disabled`` attribute, or if it is
    a descendant of a ``<fieldset>`` element whose ``disabled`` attribute is
    set -- except when it is inside that fieldset's first ``<legend>`` child,
    which stays enabled. Disabled controls are barred from submission, so
    browsers do not include their name/value pairs in the form data.

    https://html.spec.whatwg.org/multipage/form-elements.html#concept-fieldset-disabled
    """
    if tag.has_attr("disabled"):
        return True
    for fieldset in tag.find_parents("fieldset"):
        if not fieldset.has_attr("disabled"):
            continue
        # Controls inside the disabled fieldset's first <legend> child are
        # not disabled by that fieldset.
        legend = fieldset.find("legend", recursive=False)
        if legend is not None and any(p is legend for p in tag.parents):
            continue
        return True
    return False


# Tags that can be submitted as part of a form and therefore have a form
# owner per the HTML specification.
# https://html.spec.whatwg.org/multipage/form-control-infrastructure.html#form-owner  # noqa: E501
FORM_CONTROL_TAGS = ("input", "button", "select", "textarea")


def _root_tag(tag):
    """Return the topmost ancestor of ``tag`` (the root of its document
    tree or fragment)."""
    root = tag
    for parent in tag.parents:
        root = parent
    return root


def get_form_owner(tag):
    """Return the form element that owns ``tag`` per the HTML spec, or
    ``None`` if the control has no form owner.

    A control with a ``form`` attribute is owned by the element whose
    ``id`` matches it, provided that element is a ``<form>`` in the same
    tree; a reference to a missing or non-form id (including an empty
    ``form`` attribute) means the control has no form owner, even when it
    is nested inside another form. A control without a ``form``
    attribute is owned by its nearest ancestor ``<form>``.

    The result is always computed from the current DOM, so changing a
    ``form`` attribute takes effect on the next operation, and no DOM
    node is ever moved or copied.
    https://html.spec.whatwg.org/multipage/form-control-infrastructure.html#form-owner  # noqa: E501
    """
    form_id = tag.get("form")
    if form_id is not None:
        for candidate in _root_tag(tag).find_all("form", id=form_id):
            return candidate
        return None

    return tag.find_parent("form")


def iter_form_controls(form):
    """Yield the form-associated elements owned by ``form`` in document
    order, without moving or copying any DOM node.

    Both controls nested inside the ``<form>`` element and controls bound
    to it through a ``form`` attribute are included; controls whose
    ``form`` attribute points at a missing id (or at another form) are
    excluded. The walk is performed on the live tree, so attribute
    changes and re-parenting are reflected immediately.
    """
    # Backwards-compatible path for a Form built from a non-<form> tag
    # (which emits a FutureWarning at construction).
    if form.name != "form":
        yield from form.find_all(FORM_CONTROL_TAGS)
        return

    root = _root_tag(form)
    # Index forms by id once so each ownership lookup below is O(1).
    forms_by_id = {}
    for candidate in root.find_all("form"):
        candidate_id = candidate.get("id")
        if candidate_id is not None:
            forms_by_id.setdefault(candidate_id, candidate)

    for tag in root.find_all(FORM_CONTROL_TAGS):
        if tag.has_attr("form"):
            # A dangling or non-form id resolves to no owner.
            owner = forms_by_id.get(tag.get("form"))
        else:
            owner = tag.find_parent("form")
        if owner is form:
            yield tag
