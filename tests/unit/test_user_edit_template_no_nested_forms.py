# Calibre-Web Automated – fork of Calibre-Web
# Copyright (C) 2018-2026 Calibre-Web contributors
# Copyright (C) 2024-2026 Calibre-Web Automated contributors
# SPDX-License-Identifier: GPL-3.0-or-later
# See CONTRIBUTORS for full list of authors.

"""Regression test for fork issue #109 — nested-<form> bug in user_edit.html.

Pre-fix: the v4.0.31 app-passwords UI was added inside the existing
profile-save outer <form>. HTML5 forbids nested <form> elements; browsers
silently ignore the inner <form> tags and submit the OUTER form when
clicking the inner button. The user saw `/me` reload with no flash and
no token — exactly the user-visible symptom from #109.

Post-fix: app-passwords block lives OUTSIDE the profile <form>. This
test pins that invariant — any future contributor who places a <form>
inside the profile <form> trips this red.

Stand-alone parser (no Flask runtime, no Jinja render) — pure HTMLParser
walk over the template source, ignoring Jinja directives.
"""

import re
from html.parser import HTMLParser
from pathlib import Path

import pytest


TEMPLATE = (Path(__file__).resolve().parent.parent.parent /
            "cps" / "templates" / "user_edit.html")


def _strip_jinja(src: str) -> str:
    """Remove Jinja directives so HTMLParser sees only HTML.

    `{% ... %}` blocks (control flow, includes) → empty
    `{{ ... }}` expressions (values) → placeholder text
    `{# ... #}` comments → empty
    """
    src = re.sub(r"\{#.*?#\}", "", src, flags=re.DOTALL)
    src = re.sub(r"\{%.*?%\}", "", src, flags=re.DOTALL)
    src = re.sub(r"\{\{.*?\}\}", "X", src, flags=re.DOTALL)
    return src


class _FormDepthTracker(HTMLParser):
    """Walks the parsed HTML tracking <form> depth. Records every position
    where we see an opening <form> while another <form> is already open."""

    def __init__(self):
        super().__init__()
        self.depth = 0
        self.violations = []  # list of (line, col, depth_at_open)

    def handle_starttag(self, tag, attrs):
        if tag == "form":
            if self.depth >= 1:
                line, col = self.getpos()
                self.violations.append((line, col, self.depth))
            self.depth += 1

    def handle_endtag(self, tag):
        if tag == "form" and self.depth > 0:
            self.depth -= 1


@pytest.mark.unit
class TestUserEditTemplateNoNestedForms:
    """Pin: user_edit.html must not contain a nested <form>. The browser
    silently ignores nested <form> tags and the inner button submits to
    the outer form's action — which produced fork issue #109 in v4.0.31."""

    def test_template_is_present(self):
        assert TEMPLATE.is_file(), f"template missing: {TEMPLATE}"

    def test_no_nested_forms(self):
        src = _strip_jinja(TEMPLATE.read_text())
        tracker = _FormDepthTracker()
        tracker.feed(src)
        assert not tracker.violations, (
            "user_edit.html has nested <form> elements at: " +
            ", ".join(f"line {l} col {c} (inside form depth={d})"
                      for l, c, d in tracker.violations) +
            "\nHTML5 forbids nested forms. Browsers silently submit the "
            "outer form when an inner button is clicked, dropping the "
            "inner form entirely. See fork issue #109 — exactly this "
            "regression on the app-passwords UI in v4.0.31."
        )

    def test_app_passwords_form_action_is_app_password_create(self):
        """Sanity: the create-form action stays bound to the right route."""
        src = TEMPLATE.read_text()
        assert "url_for('web.app_password_create')" in src, (
            "app_password_create endpoint must be the form action. If this "
            "test goes red, the form may have lost its action attribute or "
            "been pointed at the wrong endpoint."
        )

    def test_app_passwords_section_lives_outside_outer_profile_form(self):
        """Stronger pin than 'no nested forms anywhere' — explicitly check
        that the app-passwords create form is NOT nested inside the
        profile-save outer <form>. Counts opens/closes in the raw source
        up to the position of the app-passwords form; nesting depth at
        that point must be 0."""
        src = TEMPLATE.read_text()
        # Strip HTML comments + Jinja directives so we don't count `<form>`
        # mentions inside `<!-- ... -->`, `{# ... #}`, or `{% if foo %}`.
        clean = re.sub(r"<!--.*?-->", "", src, flags=re.DOTALL)
        clean = _strip_jinja(clean)
        # Locate the line that opens the app-password create form (the
        # `url_for('web.app_password_create')` action is the unique tell).
        # _strip_jinja replaces `{{ ... }}` with `X`, so the action text
        # in cleaned form is `action="X"`. Grep the original src for the
        # identifying expression, then find that line in `clean` by other
        # context. Simpler: keep `_strip_jinja` but DON'T expand `{{ }}` to
        # `X` — leave it intact so the marker survives.
        clean2 = re.sub(r"<!--.*?-->", "", src, flags=re.DOTALL)
        clean2 = re.sub(r"\{#.*?#\}", "", clean2, flags=re.DOTALL)
        clean2 = re.sub(r"\{%.*?%\}", "", clean2, flags=re.DOTALL)
        # Find the `<form ...>` open whose action references app_password_create.
        # We want the OPEN-TAG position so we can count nesting BEFORE it.
        ap_create_form = re.search(
            r"<form[^>]*url_for\('web\.app_password_create'\)[^>]*>",
            clean2,
        )
        assert ap_create_form is not None, (
            "app_password_create <form> not found in template; either the "
            "endpoint name changed or the form was removed"
        )
        prefix = clean2[:ap_create_form.start()]
        opens = len(re.findall(r"<form\b", prefix))
        closes = len(re.findall(r"</form>", prefix))
        depth = opens - closes
        assert depth == 0, (
            f"app_password_create <form> opens at <form>-nesting depth "
            f"{depth} (expected 0). It must live OUTSIDE the profile-save "
            f"outer <form>. HTML5 forbids nested forms; the browser "
            f"silently submits the OUTER form when the inner button is "
            f"clicked, dropping the inner form's data — see fork #109."
        )
