"""Contracts for the Atlas three-pane tool shell.

The UI is plain HTML/CSS/JS, so these pin the structure a Python test can
reach: the rail / work / detail split, search-first ordering, the model-free
versus local-model distinction, the right-pane module and citation surfaces,
and the graphite / bone / moss token system.
"""

from __future__ import annotations

import re

from atlas.web import STATIC_DIR


def read(name: str) -> str:
    return (STATIC_DIR / name).read_text(encoding="utf-8")


def between(text: str, start: str, end: str) -> str:
    return text[text.index(start) : text.index(end)]


# -- search is the first action ----------------------------------------


def test_search_opens_the_left_rail() -> None:
    html = read("index.html")

    assert 'href="#find-input"' in html, "the skip link should land on search"
    rail = between(html, 'class="rail"', 'class="work"')
    assert 'id="search-heading"' in rail, "search belongs to the left rail"
    form = between(html, '<form id="find-form"', 'id="find-results"')
    assert re.search(r'<form id="find-form"[^>]*class="omni"[^>]*role="search"', form)
    assert re.search(r'<label[^>]*for="find-input"[^>]*>\s*Search the codebase', form, re.S)
    assert 'aria-labelledby="search-heading"' in html
    assert 'class="badge instant"' in html
    assert "Instant / no model" in html


def test_search_has_a_visible_keyboard_shortcut() -> None:
    html = read("index.html")
    script = read("app.js")

    assert 'aria-keyshortcuts="/"' in html
    assert 'id="ask-from-search"' in html
    assert 'event.key === "/"' in script
    assert "search.focus()" in script
    assert "search.select()" in script


def test_search_is_first_then_files_then_guide() -> None:
    html = read("index.html")
    rail = between(html, 'class="rail"', 'class="work"')

    assert rail.index('id="search-heading"') < rail.index('id="files-title"')
    assert rail.index('id="files-title"') < rail.index('id="guide-title"')
    assert 'id="file-count"' in rail
    assert 'id="tree"' in rail
    assert 'data-help="1"' in rail


# -- three-pane workbench ----------------------------------------------


def test_workspace_is_a_three_pane_tool_not_a_landing_page() -> None:
    html = read("index.html")
    css = read("app.css")

    for part in ('class="topbar"', 'class="workspace"', 'class="detail"', 'class="statusbar'):
        assert part in html, f"missing shell part: {part}"
    assert 'id="main"' in html
    assert 'class="rail"' in html
    assert 'class="work"' in html
    assert 'class="work-tabs"' in html
    assert 'role="tablist"' in html
    assert 'role="tabpanel"' in html

    # A hard three-column grid at the primary viewport, not floating cards.
    assert "grid-template-columns: 236px minmax(0, 1fr) 348px" in css
    assert 'class="hero"' not in html
    assert "read-only / local only" in html


def test_middle_pane_holds_editor_and_ask() -> None:
    html = read("index.html")
    script = read("app.js")
    work = between(html, 'class="work"', 'class="detail"')

    assert 'id="source-title"' in work
    assert 'id="work-tab-editor"' in work
    assert 'id="work-tab-ask"' in work
    assert 'id="work-editor"' in work
    assert 'id="work-ask"' in work
    assert 'id="explain-file"' in work
    assert 'id="ask-form"' in work
    assert 'id="answer"' in work
    # Explain sits with the file header, not with the repo-wide question.
    header = between(work, 'class="work-head"', 'class="work-tabs"')
    assert 'id="explain-file"' in header
    assert 'class="badge slow"' in work
    assert "Local model / can take a minute" in work

    assert "function setWorkTab(name, options)" in script
    assert "function initWorkTabs()" in script
    assert 'state.workTab = tab;' in script
    assert 'entry.panel.hidden = !entry.active;' in script
    assert '["ArrowLeft", "ArrowRight", "Home", "End"]' in script
    assert 'setWorkTab("ask");' in script
    assert 'setWorkTab("editor");' in script


def test_right_pane_holds_modules_citations_and_model() -> None:
    html = read("index.html")
    script = read("app.js")
    detail = between(html, 'class="detail"', "</main>")

    assert 'id="graph"' in detail
    assert 'id="graph-meta"' in detail
    assert 'id="module-count"' in detail
    assert 'id="chunks"' in detail
    assert 'id="chunk-count"' in detail
    assert 'id="model-status"' in detail
    assert "local only" in detail
    assert "on this machine" in detail
    assert "function renderChunks(retrieval)" in script
    assert 'resetChunks()' in script
    # Chunks are clickable in the detail pane, not only inside the answer.
    assert '$("chunks").addEventListener("click"' in script


# -- module view is simplified -----------------------------------------


def test_modules_default_to_a_list_with_map_secondary() -> None:
    html = read("index.html")
    script = read("app.js")

    assert 'id="map-view"' in html
    assert 'data-view="list" class="active"' in html
    assert 'data-view="map"' in html
    assert 'id="map-level"' in html
    assert 'mapView: "list"' in script
    assert "function setMapView(view)" in script
    assert 'if (state.mapView === "map")' in script
    assert "renderMapSvg(host, data)" in script
    assert "renderMapList(host, data, level)" in script
    assert "map-group-title" in script


# -- palette and typography --------------------------------------------


def test_palette_is_graphite_bone_and_one_moss_signal() -> None:
    css = read("app.css")
    html = read("index.html")

    for token in (
        "--ground: #0b0c0d",
        "--panel: #101315",
        "--ink: #e8e7e1",
        "--moss: #8db56b",
        '--ui: "Segoe UI Variable Text"',
    ):
        assert token in css, f"missing token: {token}"

    # The rejected warm paper / lime / serif system must be gone, not masked.
    for old in (
        "#F7F3E5",
        "#D7F26B",
        "Iowan Old Style",
        "Palatino",
        "chartreuse",
        "var(--paper)",
    ):
        assert old not in css, f"rejected value remains: {old}"

    # No second brand accent: amber/cyan are not part of this system.
    for old in ("#FF8E6E", "#38E1DA", "#FFC46B"):
        assert old not in css

    assert '<meta name="color-scheme" content="dark">' in html
    assert "font-family: var(--mono)" in css
    # Ready states, not navigation, own the single green.
    assert ".badge-dot" in css
    assert "background: var(--moss);" in css


def test_no_custom_cursor_glass_or_webgl() -> None:
    css = read("app.css")
    html = read("index.html")
    script = read("app.js")

    for banned in ("cursor: url(", "backdrop-filter", "webgl", "THREE."):
        assert banned not in css.lower()
        assert banned not in html.lower()
        assert banned not in script.lower()
    assert "animation: sweep" in css, "only the simple pending loader should animate"


def test_guide_steps_match_the_new_layout() -> None:
    html = read("index.html")
    guide = between(html, 'id="help-dialog"', "</dialog>")

    for step in (
        "Search the index",
        "Open a file and explain it",
        "Ask the repo",
        "Follow citations and modules",
    ):
        assert step in guide
    assert "<b>Search</b> section" in guide
    assert "<b>Ask</b> tab" in guide
    assert "<b>Citations</b> list on the right" in guide
    # No product-marketing copy from the reference site.
    for banned in ("Pricing", "Testimonials", "Integrations", "Autonomous engineering"):
        assert banned not in html
