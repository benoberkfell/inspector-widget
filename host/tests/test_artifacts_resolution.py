"""Where the host looks for the on-device artifacts (device-free).

After a wheel install ``inject.py`` lives in site-packages, so the repo-relative
default points nowhere useful; the artifacts directory must be settable. The
lookup order is: explicit ``build_out`` (CLI ``--build-out``) >
``$INSPECTOR_WIDGET_ARTIFACTS`` > legacy ``$VIEWSPECTOR_ARTIFACTS`` > the repo's
``build-out/``. These tests pin that order and check every injecting entry point
(CLI subcommands, ``inspector_widget.attach``, the MCP self-check) goes through it.
"""

from __future__ import annotations

import os

import pytest

import cli
import inspector_widget
import mcp_server
from inspector_widget import inject

_ENVS = (inject.ARTIFACTS_ENV, inject.LEGACY_ARTIFACTS_ENV)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for name in _ENVS:
        monkeypatch.delenv(name, raising=False)


def _make_artifacts(directory, names=inject.ARTIFACT_NAMES):
    directory.mkdir(parents=True, exist_ok=True)
    for name in names:
        (directory / name).write_bytes(b"x")
    return directory


# --------------------------------------------------------------------------- #
# Resolution order
# --------------------------------------------------------------------------- #
def test_env_names_are_the_documented_ones():
    assert inject.ARTIFACTS_ENV == "INSPECTOR_WIDGET_ARTIFACTS"
    assert inject.LEGACY_ARTIFACTS_ENV == "VIEWSPECTOR_ARTIFACTS"
    assert inject.ARTIFACT_NAMES == ("libviewspector.so", "bootstrap.dex", "payload.jar")


def test_default_is_repo_build_out_when_nothing_is_set():
    repo_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    assert inject.resolve_build_out() == os.path.join(repo_root, "build-out")
    assert inject.resolve_build_out() == inject.DEFAULT_BUILD_OUT
    assert inject.artifact_status()["source"].startswith("default")


def test_legacy_env_used_when_new_env_unset(monkeypatch, tmp_path):
    monkeypatch.setenv("VIEWSPECTOR_ARTIFACTS", str(tmp_path / "legacy"))
    assert inject.resolve_build_out() == str(tmp_path / "legacy")
    assert inject.artifact_status()["source"] == "$VIEWSPECTOR_ARTIFACTS"


def test_new_env_beats_legacy_env(monkeypatch, tmp_path):
    monkeypatch.setenv("VIEWSPECTOR_ARTIFACTS", str(tmp_path / "legacy"))
    monkeypatch.setenv("INSPECTOR_WIDGET_ARTIFACTS", str(tmp_path / "new"))
    assert inject.resolve_build_out() == str(tmp_path / "new")
    assert inject.artifact_status()["source"] == "$INSPECTOR_WIDGET_ARTIFACTS"


def test_explicit_dir_beats_both_env_vars(monkeypatch, tmp_path):
    monkeypatch.setenv("VIEWSPECTOR_ARTIFACTS", str(tmp_path / "legacy"))
    monkeypatch.setenv("INSPECTOR_WIDGET_ARTIFACTS", str(tmp_path / "new"))
    assert inject.resolve_build_out(str(tmp_path / "flag")) == str(tmp_path / "flag")
    assert inject.artifact_status(str(tmp_path / "flag"))["source"] == "--build-out"


def test_empty_env_var_counts_as_unset(monkeypatch, tmp_path):
    monkeypatch.setenv("INSPECTOR_WIDGET_ARTIFACTS", "")
    monkeypatch.setenv("VIEWSPECTOR_ARTIFACTS", str(tmp_path / "legacy"))
    assert inject.resolve_build_out() == str(tmp_path / "legacy")


def test_env_is_read_at_call_time_not_import_time(monkeypatch, tmp_path):
    before = inject.resolve_build_out()
    monkeypatch.setenv("INSPECTOR_WIDGET_ARTIFACTS", str(tmp_path))
    assert inject.resolve_build_out() == str(tmp_path) != before


def test_relative_and_tilde_paths_are_made_absolute(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    assert inject.resolve_build_out("out") == str(tmp_path / "out")
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("INSPECTOR_WIDGET_ARTIFACTS", "~/arts")
    assert inject.resolve_build_out() == str(tmp_path / "arts")


# --------------------------------------------------------------------------- #
# artifact_status
# --------------------------------------------------------------------------- #
def test_artifact_status_all_present(monkeypatch, tmp_path):
    _make_artifacts(tmp_path)
    monkeypatch.setenv("INSPECTOR_WIDGET_ARTIFACTS", str(tmp_path))
    st = inject.artifact_status()
    assert st["dir"] == str(tmp_path)
    assert st["missing"] == []
    assert all(st["present"].values())


def test_artifact_status_reports_missing(tmp_path):
    _make_artifacts(tmp_path, names=("libviewspector.so", "payload.jar"))
    st = inject.artifact_status(str(tmp_path))
    assert st["missing"] == ["bootstrap.dex"]
    assert st["present"] == {
        "libviewspector.so": True, "bootstrap.dex": False, "payload.jar": True,
    }


def test_missing_artifact_error_names_the_override(tmp_path):
    with pytest.raises(inject.InjectionError) as ei:
        inject._artifact_path(str(tmp_path), "bootstrap.dex")
    msg = str(ei.value)
    assert str(tmp_path / "bootstrap.dex") in msg
    assert "--build-out" in msg and "INSPECTOR_WIDGET_ARTIFACTS" in msg


# --------------------------------------------------------------------------- #
# inject_and_connect stages from the resolved directory
# --------------------------------------------------------------------------- #
class _Staged(Exception):
    pass


def _stub_cold_inject(monkeypatch):
    """Make inject_and_connect reach _push_and_stage without a device; record build_out."""
    seen = {}
    monkeypatch.setattr(inject.adb, "pidof", lambda serial, package: 4242)
    monkeypatch.setattr(inject.adb, "shell", lambda serial, cmd: "")
    monkeypatch.setattr(inject, "_try_warm_connect", lambda serial, pid: None)

    def fake_stage(serial, package, build_out):
        seen["build_out"] = build_out
        raise _Staged()

    monkeypatch.setattr(inject, "_push_and_stage", fake_stage)
    return seen


def test_inject_and_connect_uses_env_dir(monkeypatch, tmp_path):
    seen = _stub_cold_inject(monkeypatch)
    monkeypatch.setenv("INSPECTOR_WIDGET_ARTIFACTS", str(tmp_path))
    with pytest.raises(_Staged):
        inject.inject_and_connect(serial="s", package="p")
    assert seen["build_out"] == str(tmp_path)


def test_inject_and_connect_explicit_dir_wins(monkeypatch, tmp_path):
    seen = _stub_cold_inject(monkeypatch)
    monkeypatch.setenv("INSPECTOR_WIDGET_ARTIFACTS", str(tmp_path / "env"))
    with pytest.raises(_Staged):
        inject.inject_and_connect(serial="s", package="p", build_out=str(tmp_path / "flag"))
    assert seen["build_out"] == str(tmp_path / "flag")


def test_package_attach_forwards_build_out(monkeypatch, tmp_path):
    seen = _stub_cold_inject(monkeypatch)
    with pytest.raises(_Staged):
        inspector_widget.attach("s", "p", build_out=str(tmp_path))
    assert seen["build_out"] == str(tmp_path)
    with pytest.raises(_Staged):
        inspector_widget.attach("s", "p")  # the MCP path: no flag, env/default
    assert seen["build_out"] == inject.DEFAULT_BUILD_OUT


# --------------------------------------------------------------------------- #
# CLI: every subcommand that can inject accepts --build-out and passes it through
# --------------------------------------------------------------------------- #
_INJECTING = [
    ["attach"],
    ["dump"],
    ["compose"],
    ["a11y"],
    ["a11y-lint"],
    ["inspect"],
    ["inspect-node", "--view-id", "5"],
    ["component-image", "--view-id", "5"],
    ["screenshot", "--out", "unused.png"],
    ["get-properties", "--view-id", "5"],
    ["detach"],
]


@pytest.mark.parametrize("argv", _INJECTING, ids=lambda a: a[0])
def test_cli_build_out_flag_reaches_injection(monkeypatch, tmp_path, argv):
    seen = _stub_cold_inject(monkeypatch)
    monkeypatch.setenv("INSPECTOR_WIDGET_ARTIFACTS", str(tmp_path / "env"))
    rc = cli.main(argv + ["--build-out", str(tmp_path / "flag")])
    assert rc == 1  # our stub aborts the injection; main() reports it cleanly
    assert seen["build_out"] == str(tmp_path / "flag")


@pytest.mark.parametrize("argv", _INJECTING, ids=lambda a: a[0])
def test_cli_without_flag_uses_env(monkeypatch, tmp_path, argv):
    seen = _stub_cold_inject(monkeypatch)
    monkeypatch.setenv("INSPECTOR_WIDGET_ARTIFACTS", str(tmp_path / "env"))
    cli.main(argv)
    assert seen["build_out"] == str(tmp_path / "env")


def test_every_injecting_subcommand_is_covered():
    """devices/packages never inject; every other subcommand must take --build-out."""
    parser = cli.build_parser()
    sub = next(a for a in parser._actions if a.__class__.__name__ == "_SubParsersAction")
    injecting = {name for name in sub.choices if name not in ("devices", "packages")}
    assert injecting == {argv[0] for argv in _INJECTING}
    for name in injecting:
        opts = {o for a in sub.choices[name]._actions for o in a.option_strings}
        assert "--build-out" in opts, f"{name} lacks --build-out"


# --------------------------------------------------------------------------- #
# MCP self-check reports the artifacts (warning only, never a failure)
# --------------------------------------------------------------------------- #
def test_self_check_reports_found_artifacts(monkeypatch, tmp_path, capsys):
    _make_artifacts(tmp_path)
    monkeypatch.setenv("INSPECTOR_WIDGET_ARTIFACTS", str(tmp_path))
    mcp_server.main(["--self-check"])
    out = capsys.readouterr().out
    assert f"artifacts: {tmp_path} (from $INSPECTOR_WIDGET_ARTIFACTS)" in out
    for name in inject.ARTIFACT_NAMES:
        assert f"{name}: OK" in out
    assert "WARNING" not in out


def test_self_check_warns_on_missing_artifacts_without_failing(monkeypatch, tmp_path, capsys):
    _make_artifacts(tmp_path, names=("libviewspector.so",))
    monkeypatch.setenv("VIEWSPECTOR_ARTIFACTS", str(tmp_path))
    rc_missing = mcp_server.main(["--self-check"])
    out = capsys.readouterr().out
    assert "(from $VIEWSPECTOR_ARTIFACTS)" in out
    assert "bootstrap.dex: MISSING" in out and "payload.jar: MISSING" in out
    assert "WARNING: 2 of 3 artifacts missing" in out

    _make_artifacts(tmp_path)
    rc_present = mcp_server.main(["--self-check"])
    capsys.readouterr()
    assert rc_missing == rc_present  # missing artifacts never change the exit code
