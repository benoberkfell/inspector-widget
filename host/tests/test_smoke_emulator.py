"""End-to-end smoke test against a live emulator (opt-in, auto-skipping).

Marked ``device`` so the default run (``pytest -m 'not device'``) never touches
adb. When an ``emulator-5554`` device with the A11yProbe app is present, this
attaches, dumps the a11y tree, and runs the lint — proving the whole host stack
works against a real agent.

Run it explicitly with:
    host/.venv/bin/pytest host/tests -m device
"""

from __future__ import annotations

import pytest

from conftest import device_present

SERIAL = "emulator-5554"
PACKAGE = "com.oberkfell.a11yprobe"

pytestmark = pytest.mark.device


@pytest.fixture
def session():
    if not device_present(SERIAL):
        pytest.skip(f"no live {SERIAL} device/adb present")
    import inspector_widget

    devices = {d["serial"] for d in inspector_widget.list_devices()}
    if SERIAL not in devices:
        pytest.skip(f"{SERIAL} not in adb device list")

    packages = {p["package"] for p in inspector_widget.list_processes(SERIAL)}
    if PACKAGE not in packages:
        pytest.skip(f"{PACKAGE} not installed/debuggable on {SERIAL}")

    # Other device tests (the a11y goldens) force-stop the app when they finish.
    from inspector_widget import adb
    adb.shell(SERIAL, f"am start -W -n {PACKAGE}/.MainActivity", check=False)
    sess = inspector_widget.attach(SERIAL, PACKAGE)
    try:
        yield sess
    finally:
        try:
            sess.detach()
        except Exception:
            pass


def test_hello(session):
    hello = session.hello()
    assert hello.agent_version
    assert hello.api_level > 0


def test_dump_a11y_and_lint(session):
    from inspector_widget import a11y as a11ymod, strings as st, a11y_lint

    resp = session.dump_a11y(root_id=0, include_extras=True)
    data = a11ymod.a11y_to_dict(resp)
    assert "windows" in data
    assert data["windows"], "expected at least one a11y window"
    # The reading order should contain at least one focus stop (a11y_to_dict lists only
    # the stops, each with its 1-based "order"; is_focus_stop appears only with the
    # structural nodes included).
    assert any(e.get("order") for e in data.get("focus_order", []))

    compose = st.dump_compose_to_dict(
        session.dump_compose(include_semantics=True, include_slot_table=False))
    roots = [w["root"] for w in compose.get("windows", []) if w.get("root")]
    # Lint runs tree-only here (no screenshot) and must not raise.
    findings = a11y_lint.lint_tree(roots, a11y_lint.LintContext(density=420))
    summary = a11y_lint.summarize(findings)
    assert summary["total"] == len(findings)
