"""First-run empty states and the add-company modal field order (UX panel
review, UI-01 / UI-03).

UI-01: Jobs, Companies, and Contacts used to render a "no X match the current
filters/search" line unconditionally, so a first-run user with zero data and
no active filter saw a search-failure message. Each empty state now branches
on the UNFILTERED collection total (mirroring applications.js): records exist
but none pass the filter -> the filtered message; the collection is truly
empty -> an inviting first-run line.

UI-03: The add-company modal front-loaded pre-defaulted selects and buried the
detection-critical Careers URL last. The modal body now follows the wizard's
identity-first order: Name -> Website -> Careers -> Location -> Priority ->
Status -> Values fit.

No JS runtime here (see test_a11y_rows_frontend / test_settings_frontend), so
the behavior is pinned against the shipped source.
"""

from jshq import paths

FRONTEND = paths.FRONTEND_DIR


def _read(rel):
    return (FRONTEND / rel).read_text(encoding="utf-8")


# ---- UI-01: two-state empty branch on the unfiltered total -----------------

# (view file, state collection, filtered message, empty-collection message)
EMPTY_STATE_CASES = (
    (
        "js/views/jobs.js",
        "state.jobs.length",
        "No jobs match the current filters.",
        "No jobs yet — they'll appear here once your first board is pulled.",
    ),
    (
        "js/views/companies.js",
        "state.companies.length",
        "No companies match the current filters.",
        "No companies yet — add one with the “+ Add company” button.",
    ),
    (
        "js/views/contacts.js",
        "state.contacts.length",
        "No contacts match the current search.",
        "No contacts yet — add one from a company's page.",
    ),
)


def test_empty_states_branch_on_unfiltered_total():
    for rel, coll, filtered_msg, empty_msg in EMPTY_STATE_CASES:
        src = _read(rel)
        # Branches on the unfiltered collection length...
        assert coll in src, f"{rel}: missing branch on {coll}"
        # ...serving the filtered message when records exist but none pass...
        assert filtered_msg in src, f"{rel}: missing filtered message"
        # ...and the inviting first-run line when the collection is empty.
        assert empty_msg in src, f"{rel}: missing empty-collection message"


def test_empty_state_branch_is_present_at_both_render_sites():
    # template() and repaintList() both build the list-pane; both must carry
    # the two-state branch (the repaint site previously did not).
    for rel, coll, _filtered, _empty in EMPTY_STATE_CASES:
        src = _read(rel)
        if rel == "js/views/jobs.js":
            # jobs.js consolidated both render sites into a shared listBodyHtml()
            # (#58, which prepends the hidden-by-filters notice), so the two-state
            # branch lives once there and both paths delegate to it.
            assert src.count(coll) >= 1, rel
            assert src.count("listBodyHtml()") >= 2, f"{rel}: both sites must use the shared body"
        else:
            assert src.count(coll) >= 2, f"{rel}: expected the branch at both render sites"


# ---- UI-03: add-company modal field order ----------------------------------

MODAL_FIELD_ORDER = (
    "name",
    "website",
    "careers_url",
    "location",
    "priority",
    "status",
    "values_fit",
)


def _add_modal_body(src):
    # Isolate the addModal() body: from its opener to the footer: key.
    start = src.index("function addModal()")
    footer = src.index("footer:", start)
    return src[start:footer]


def test_add_company_modal_field_order_matches_wizard():
    src = _read("js/views/companies.js")
    body = _add_modal_body(src)
    indices = []
    for name in MODAL_FIELD_ORDER:
        needle = f'name="{name}"'
        assert needle in body, f"add-company modal missing field {name}"
        indices.append(body.index(needle))
    assert indices == sorted(indices), (
        "add-company modal fields are out of order; expected "
        f"{' -> '.join(MODAL_FIELD_ORDER)}"
    )
