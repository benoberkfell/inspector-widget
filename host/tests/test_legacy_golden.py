"""The legacy tool outputs, pinned (work package G1), and the Phase-0 rollback.

``tests/golden/legacy/<scene>/<surface>-legacy.json.gz`` holds what every legacy
MCP tool and CLI subcommand returned before Phase 0, through the real entry
points over the harness fake adb and agent, on four scenes (``record_goldens.py``).
Running the same calls with the rollback (``detail="full"`` and ``max_bytes=0``;
``--detail full --max-bytes 0``) must reproduce them (spec section 2.7), entry by
entry, parsed JSON (and the text of the human outputs). The only exceptions are
the entries re-recorded since, each with its reason (``LEGACY_DELTAS``).

``<surface>-brief.json.gz`` pins the Phase-0 defaults (brief, budgeted) the same
way, so any change to a shaper or to the brief rules shows up as a path-level diff.

Regenerate a golden only for a deliberate change, and say why in the commit:
``PYTHONPATH=. .venv/bin/python tests/record_goldens.py [--mode brief|legacy]
[--only ENTRY] [SCENE...]`` (a legacy entry needs a ``LEGACY_DELTAS`` reason).
"""

from __future__ import annotations

import pytest
import record_goldens as rg


def _check(scene: str, surface: str, tmp_path, mode: str) -> None:
    golden = rg.load_golden(scene, surface, mode)["entries"]
    actual = rg.run(scene, surface, mode, str(tmp_path))
    problems = []
    for name in sorted(set(golden) | set(actual)):
        if name not in actual:
            problems.append(f"{surface} {name} [{scene}]: not run any more")
            continue
        if name not in golden:
            problems.append(f"{surface} {name} [{scene}]: no golden (re-record)")
            continue
        lines = rg.diff(rg.comparable(golden[name]), rg.comparable(actual[name]))
        if lines:
            problems.append(f"{surface} {name} [{scene}] "
                            f"(golden from {golden[name]['source_commit']}):\n    "
                            + "\n    ".join(lines))
    assert not problems, f"{mode} outputs changed:\n" + "\n".join(problems)


@pytest.mark.parametrize("scene", rg.SCENES)
@pytest.mark.parametrize("surface", ["mcp", "cli"])
def test_the_rollback_reproduces_the_legacy_outputs(scene, surface, tmp_path):
    _check(scene, surface, tmp_path, "legacy")


@pytest.mark.parametrize("scene", rg.SCENES)
@pytest.mark.parametrize("surface", ["mcp", "cli"])
def test_brief_outputs_match_the_goldens(scene, surface, tmp_path):
    _check(scene, surface, tmp_path, "brief")


def test_only_the_documented_deltas_were_re_recorded():
    for scene in rg.SCENES:
        for surface in ("mcp", "cli"):
            for name, entry in rg.load_golden(scene, surface, "legacy")["entries"].items():
                if entry["source_commit"] == rg.G1_COMMIT:
                    assert "delta" not in entry, (scene, surface, name)
                else:
                    assert entry.get("delta") == rg.LEGACY_DELTAS[(surface, name)], (
                        f"{surface} {name} [{scene}] was re-recorded without a documented "
                        f"reason")


def test_goldens_cover_every_legacy_tool_and_subcommand():
    tools = {"list_devices", "list_processes", "attach", "detach", "dump_tree",
             "get_properties", "screenshot", "dump_compose", "compose_overlay",
             "dump_accessibility", "a11y_lint", "a11y_overlay", "inspect", "inspect_node",
             "component_image"}
    subcommands = {"devices", "packages", "attach", "dump", "compose", "a11y", "a11y-lint",
                   "inspect", "inspect-node", "component-image", "screenshot",
                   "get-properties", "detach"}
    for scene in rg.SCENES:
        for mode in rg.MODES:
            mcp = rg.load_golden(scene, "mcp", mode)["entries"]
            assert {e["tool"] for e in mcp.values()} == tools, (scene, mode)
            cli = rg.load_golden(scene, "cli", mode)["entries"]
            assert {e["argv"][0] for e in cli.values()} == subcommands, (scene, mode)
            for name in ("dump_text", "compose_text", "a11y_text", "inspect_text"):
                assert "stdout" in cli[name], (scene, mode, name)
            assert all(e["source_commit"] for e in [*mcp.values(), *cli.values()])


def test_a_renamed_key_fails_with_a_readable_diff(monkeypatch, tmp_path):
    """Mutating a shaper (strings.node_to_dict renaming ``class_name``) must fail
    the comparison and name the path that changed."""
    from inspector_widget import strings

    real = strings.node_to_dict

    def renamed(node, resolver):
        out = real(node, resolver)
        out["klass"] = out.pop("class_name")
        return out

    monkeypatch.setattr(strings, "node_to_dict", renamed)
    with pytest.raises(AssertionError) as exc:
        _check("default", "cli", tmp_path, "legacy")
    msg = str(exc.value)
    assert "cli dump [default]" in msg
    assert "$.json.roots[0].class_name: missing" in msg
    assert "$.json.roots[0].klass: unexpected" in msg


def test_diff_names_paths_and_values():
    golden = {"a": [1, {"b": "x"}], "c": 1}
    assert rg.diff(golden, {"a": [1, {"b": "y"}], "c": 1}) == [
        '$.a[1].b: golden "x" != actual "y"']
    assert rg.diff(golden, {"a": [1], "c": 1, "d": 2}) == [
        "$.a: 2 items in the golden, 1 now", "$.d: unexpected 2"]
    assert rg.diff(1, 1.0) == ["$: golden 1 != actual 1.0"]
