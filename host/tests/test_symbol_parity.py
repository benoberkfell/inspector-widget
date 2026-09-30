"""AST symbol-parity guard: every attribute the CLI / MCP server reads off a
host submodule must actually exist on that imported module.

This is the highest-leverage regression guard for the whole bug class that this
test pass targets: it statically scans ``cli.py`` and ``mcp_server.py`` for every
``<module>.<attr>`` access on the inspector_widget submodules they import
(adb / a11y / a11y_lint / overlay / png / correlate / strings / inject / client /
results) and asserts the referenced attribute is a real member of the imported
module.

It would have caught, in one shot:
  * ``adb.display_density`` / ``adb.font_scale`` missing from adb.py
  * ``a11y.lint_a11y`` (the lint moved to a11y_lint.lint_a11y)
  * ``overlay.render_integrated_overlay`` missing from overlay.py

The scan is import-alias aware: it handles ``from inspector_widget import adb``,
``... import overlay as ovmod``, grouped/parenthesised function-scope imports
(``from inspector_widget import (a11y as a11ymod, strings as st, ...)``), and the
bare ``import inspector_widget`` package handle.

The second half of the file (E8) goes further: it binds every resolvable call
into inspector_widget against the real ``inspect.signature`` (kwargs + arity),
checks methods/fields on Session / Client / Injection / proto messages, and
checks literal ``getattr``/``hasattr`` probes. It also scans the package modules
that sit on the device path (``__init__``, ``inject``, ``client``, ``correlate``).
The runtime counterpart is the fake-agent harness in ``test_e2e_fake_agent.py``.
"""

from __future__ import annotations

import ast
import importlib
import os
from typing import Dict, List, Set, Tuple

import pytest

# host/ is conftest's _HOST_DIR (already on sys.path); these two scripts live there.
_HOST_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# The inspector_widget submodules whose attribute access we police. Anything
# imported from inspector_widget that is NOT in this set is ignored here; the
# signature scan at the bottom of this file covers the rest of the package.
_POLICED_SUBMODULES = {
    "adb", "a11y", "a11y_lint", "overlay", "png", "correlate", "strings",
    "inject", "client", "results",
}

_SCRIPTS = ("cli.py", "mcp_server.py")


def _load_module_members(submodule: str) -> Set[str]:
    mod = importlib.import_module(f"inspector_widget.{submodule}")
    return set(dir(mod))


class _AliasResolver(ast.NodeVisitor):
    """Walk a module AST, tracking which local names are bound to a policed
    inspector_widget submodule, and collect every attribute access on them.

    Alias bindings are collected module-wide first (a single pass), which keeps
    the resolver simple and is sufficient here because these scripts never rebind
    a module alias to something else. ``import inspector_widget [as iw]`` is
    tracked separately so ``iw.attach`` etc. is NOT flagged (the package, not a
    policed submodule).
    """

    def __init__(self) -> None:
        # local alias name -> policed submodule name (e.g. "ovmod" -> "overlay")
        self.alias_to_submodule: Dict[str, str] = {}
        # local names bound to the inspector_widget package itself
        self.package_aliases: Set[str] = set()
        # (local_alias, attr, submodule, lineno)
        self.accesses: List[Tuple[str, str, str, int]] = []

    # -- binding collection ------------------------------------------------- #
    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            name = alias.name
            local = alias.asname or name.split(".")[0]
            if name == "inspector_widget":
                self.package_aliases.add(local)
            elif name.startswith("inspector_widget."):
                leaf = name.split(".")[-1]
                if leaf in _POLICED_SUBMODULES:
                    self.alias_to_submodule[alias.asname or leaf] = leaf
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        if node.module == "inspector_widget":
            for alias in node.names:
                if alias.name in _POLICED_SUBMODULES:
                    local = alias.asname or alias.name
                    self.alias_to_submodule[local] = alias.name
        self.generic_visit(node)

    # -- access collection -------------------------------------------------- #
    def visit_Attribute(self, node: ast.Attribute) -> None:
        # Only direct ``Name.attr`` accesses; nested ``a.b.c`` recurse via
        # generic_visit so the inner ``a.b`` is examined on its own.
        if isinstance(node.value, ast.Name):
            self.accesses.append((node.value.id, node.attr, "", node.lineno))
        self.generic_visit(node)


def _scan_script(path: str) -> Tuple[_AliasResolver, List[Tuple[str, str, str, int]]]:
    with open(path, "r", encoding="utf-8") as f:
        tree = ast.parse(f.read(), filename=path)
    resolver = _AliasResolver()
    resolver.visit(tree)
    # Keep only accesses whose Name is a policed-submodule alias.
    resolved = [
        (alias, attr, resolver.alias_to_submodule[alias], lineno)
        for (alias, attr, _sub, lineno) in resolver.accesses
        if alias in resolver.alias_to_submodule
    ]
    return resolver, resolved


@pytest.mark.parametrize("script", _SCRIPTS)
def test_every_module_attr_access_exists(script: str) -> None:
    path = os.path.join(_HOST_DIR, script)
    assert os.path.isfile(path), f"expected to find {path}"
    _resolver, accesses = _scan_script(path)

    members_cache: Dict[str, Set[str]] = {}
    missing: List[str] = []
    for alias, attr, submodule, lineno in accesses:
        if submodule not in members_cache:
            members_cache[submodule] = _load_module_members(submodule)
        if attr not in members_cache[submodule]:
            missing.append(
                f"{script}:{lineno}: {alias}.{attr} -> "
                f"inspector_widget.{submodule} has no attribute '{attr}'"
            )

    assert not missing, (
        "Attribute(s) referenced in the script do not exist on the imported "
        "inspector_widget submodule:\n  " + "\n  ".join(missing)
    )


def test_scan_actually_finds_contract_symbols() -> None:
    """Guard the guard: prove the scan resolves the exact symbols the shared
    contract introduced, so a future refactor can't silently neuter this test
    into asserting over an empty access list.
    """
    found: Set[Tuple[str, str]] = set()
    for script in _SCRIPTS:
        _resolver, accesses = _scan_script(os.path.join(_HOST_DIR, script))
        for _alias, attr, submodule, _lineno in accesses:
            found.add((submodule, attr))

    expected = {
        ("adb", "display_density"),
        ("adb", "font_scale"),
        ("a11y_lint", "run_lint"),
        ("overlay", "render_integrated_overlay"),
        ("png", "write_png"),
        ("results", "dump_tree"),
        ("results", "a11y_lint"),
    }
    not_seen = expected - found
    assert not not_seen, (
        "symbol-parity scan failed to even observe these contract symbols "
        f"(scanner regression?): {sorted(not_seen)}"
    )


# =========================================================================== #
# E8: signatures, not just names.
#
# The attribute-existence scan above can't see a call that passes a kwarg the
# callee doesn't take, the wrong number of positionals, a method missing from
# Session/Client/Injection, a proto field typo, or a getattr() probe for an
# attribute that no longer exists. The scan below resolves call targets
# statically and binds each call against the real signature with
# inspect.signature(...).bind().
#
# Receivers are typed from (in order): `self` inside a scanned class; a local
# assigned from a resolvable call (a class -> an instance; a function -> its
# return annotation); proto message fields; and, for untyped hand-offs such as
# mcp_server's `session = SESSIONS.get_or_attach(...)`, the variable name
# (_NAMED_RECEIVERS). Only targets that live in inspector_widget (including the
# generated proto classes) are policed.
# =========================================================================== #
import dataclasses  # noqa: E402
import inspect  # noqa: E402
import textwrap  # noqa: E402
import typing  # noqa: E402

from google.protobuf import message_factory  # noqa: E402

_SIG_SOURCES = (
    "cli.py",
    "mcp_server.py",
    "inspector_widget/__init__.py",
    "inspector_widget/inject.py",
    "inspector_widget/client.py",
    "inspector_widget/correlate.py",
    "inspector_widget/results.py",
    "inspector_widget/talkback/device.py",
    "inspector_widget/talkback/inject.py",
    "inspector_widget/talkback/walk.py",
    "inspector_widget/talkback/diff.py",
    "inspector_widget/talkback/scenarios.py",
)

_NAMED_RECEIVERS = {
    "session": ("inspector_widget", "Session"),
    "client": ("inspector_widget.client", "Client"),
    "inj": ("inspector_widget.inject", "Injection"),
    "injection": ("inspector_widget.inject", "Injection"),
}

# getattr()/hasattr() probes that are deliberately optional: the caller has a
# working fallback when the attribute is absent. Anything else that fails to
# resolve is a bug (or belongs in an xfail below with its ledger id).
_KNOWN_OPTIONAL_PROBES = {
    ("Session", "capture_skp"): "correlate._capture_skp falls back to session.client.capture_skp",
}
_XFAIL_PROBES: Dict[Tuple[str, str], str] = {}


class _Inst:
    """An instance of ``cls`` (as opposed to the class object itself)."""

    def __init__(self, cls):
        self.cls = cls


def _is_proto(cls) -> bool:
    return hasattr(cls, "DESCRIPTOR") and hasattr(cls.DESCRIPTOR, "fields_by_name")


def _policed(obj) -> bool:
    cls = obj.cls if isinstance(obj, _Inst) else obj
    if inspect.ismodule(cls):
        return cls.__name__.split(".")[0] == "inspector_widget"
    if inspect.isclass(cls) and _is_proto(cls):
        return True
    return (getattr(cls, "__module__", "") or "").split(".")[0] == "inspector_widget"


def _label(obj) -> str:
    cls = obj.cls if isinstance(obj, _Inst) else obj
    return getattr(cls, "__name__", repr(cls)).split(".")[-1]


_ATTRS_CACHE: Dict[type, Set[str]] = {}


def _instance_attrs(cls) -> Set[str]:
    if cls in _ATTRS_CACHE:
        return _ATTRS_CACHE[cls]
    names = set(dir(cls))
    if _is_proto(cls):
        names |= set(cls.DESCRIPTOR.fields_by_name)
    if dataclasses.is_dataclass(cls):
        names |= {f.name for f in dataclasses.fields(cls)}
    names |= set(getattr(cls, "__annotations__", {}))
    # self.<attr> assignments in the class and in the base classes it inherits from
    for klass in getattr(cls, "__mro__", (cls,)):
        if (getattr(klass, "__module__", "") or "").split(".")[0] != "inspector_widget" \
                and klass is not cls:
            continue
        try:
            tree = ast.parse(textwrap.dedent(inspect.getsource(klass)))
        except (OSError, TypeError, SyntaxError):
            tree = None
        for node in ast.walk(tree) if tree else ():
            targets = node.targets if isinstance(node, ast.Assign) else (
                [node.target] if isinstance(node, (ast.AnnAssign, ast.AugAssign)) else [])
            for t in targets:
                if isinstance(t, ast.Attribute) and isinstance(t.value, ast.Name) \
                        and t.value.id == "self":
                    names.add(t.attr)
    _ATTRS_CACHE[cls] = names
    return names


def _returns(fn):
    """The class a callable returns, from its annotation (Optional unwrapped)."""
    if inspect.isclass(fn):
        return fn
    try:
        ret = typing.get_type_hints(fn).get("return")
    except Exception:
        return None
    args = [a for a in typing.get_args(ret) if a is not type(None)]
    if args and len(args) == 1:
        ret = args[0]
    return ret if inspect.isclass(ret) else None


class _SignatureScan(ast.NodeVisitor):
    """Resolve call targets / receivers in one source file and check them."""

    def __init__(self, path: str, module_name: Optional[str]) -> None:
        self.path = path
        self.module = importlib.import_module(module_name) if module_name else None
        self.package = module_name if module_name and os.path.basename(path) == "__init__.py" \
            else (module_name.rsplit(".", 1)[0] if module_name and "." in module_name else None)
        self.bindings: Dict[str, object] = {}
        self.scopes: List[Dict[str, List[ast.expr]]] = []
        self.class_stack: List[Optional[type]] = []
        self.class_attr_types: Dict[type, Dict[str, List[ast.expr]]] = {}
        self.binds: List[Tuple[str, str, int]] = []      # (owner, name, line) checked OK
        self.bind_failures: List[str] = []
        self.missing_attrs: List[str] = []
        self.probes: List[Tuple[str, str, bool, int]] = []  # (owner, attr, resolved, line)

    # ---- pass 1: imports ------------------------------------------------- #
    def collect_imports(self, tree: ast.AST) -> None:
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name.split(".")[0] != "inspector_widget":
                        continue
                    if alias.asname:
                        self.bindings[alias.asname] = importlib.import_module(alias.name)
                    else:
                        self.bindings["inspector_widget"] = importlib.import_module("inspector_widget")
            elif isinstance(node, ast.ImportFrom):
                if node.level:
                    if not self.package:
                        continue
                    # `from .. import x` in a subpackage is relative to its parent.
                    parts = self.package.split(".")
                    parts = parts[:len(parts) - (node.level - 1)]
                    base = ".".join(parts) + ("." + node.module if node.module else "")
                else:
                    base = node.module or ""
                if base.split(".")[0] != "inspector_widget":
                    continue
                mod = importlib.import_module(base)
                for alias in node.names:
                    local = alias.asname or alias.name
                    if hasattr(mod, alias.name):
                        self.bindings[local] = getattr(mod, alias.name)
                    else:
                        try:
                            self.bindings[local] = importlib.import_module(f"{base}.{alias.name}")
                        except ImportError:
                            self.missing_attrs.append(
                                f"{self.path}:{node.lineno}: from {base} import {alias.name}")

    # ---- scopes ---------------------------------------------------------- #
    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        cls = getattr(self.module, node.name, None) if self.module else None
        self.class_stack.append(cls if inspect.isclass(cls) else None)
        if inspect.isclass(cls):
            attr_types = self.class_attr_types.setdefault(cls, {})
            for sub in ast.walk(node):
                if isinstance(sub, ast.Assign) and len(sub.targets) == 1:
                    t = sub.targets[0]
                    if isinstance(t, ast.Attribute) and isinstance(t.value, ast.Name) \
                            and t.value.id == "self":
                        attr_types.setdefault(t.attr, []).append(sub.value)
        self.generic_visit(node)
        self.class_stack.pop()

    def _visit_function(self, node) -> None:
        assigned: Dict[str, List[ast.expr]] = {}
        for sub in ast.walk(node):
            if isinstance(sub, ast.Assign) and len(sub.targets) == 1 \
                    and isinstance(sub.targets[0], ast.Name):
                assigned.setdefault(sub.targets[0].id, []).append(sub.value)
        self.scopes.append(assigned)
        self.generic_visit(node)
        self.scopes.pop()

    visit_FunctionDef = _visit_function
    visit_AsyncFunctionDef = _visit_function

    # ---- resolution ------------------------------------------------------ #
    def resolve(self, expr: ast.expr, depth: int = 0):
        if depth > 8:
            return None
        if isinstance(expr, ast.Name):
            if expr.id == "self" and self.class_stack and self.class_stack[-1] is not None:
                return _Inst(self.class_stack[-1])
            for scope in reversed(self.scopes):
                if expr.id in scope:
                    types = [self._type_of(v, depth) for v in scope[expr.id]]
                    if all(isinstance(t, _Inst) for t in types) and \
                            len({t.cls for t in types}) == 1:
                        return types[0]
                    break
            if expr.id in self.bindings:
                return self.bindings[expr.id]
            if expr.id in _NAMED_RECEIVERS:
                mod, name = _NAMED_RECEIVERS[expr.id]
                return _Inst(getattr(importlib.import_module(mod), name))
            return None
        if isinstance(expr, ast.Attribute):
            base = self.resolve(expr.value, depth + 1)
            if base is None:
                return None
            if isinstance(base, _Inst):
                cls = base.cls
                if _is_proto(cls):
                    field = cls.DESCRIPTOR.fields_by_name.get(expr.attr)
                    if field is not None and field.message_type is not None \
                            and not _is_repeated(field):
                        return _Inst(message_factory.GetMessageClass(field.message_type))
                    return None
                exprs = self.class_attr_types.get(cls, {}).get(expr.attr)
                if exprs:
                    types = [self._type_of(v, depth + 1) for v in exprs]
                    if all(isinstance(t, _Inst) for t in types) and len({t.cls for t in types}) == 1:
                        return types[0]
                return None
            return getattr(base, expr.attr, None)
        if isinstance(expr, ast.Call):
            return self._type_of(expr, depth)
        return None

    def _type_of(self, value: ast.expr, depth: int):
        if not isinstance(value, ast.Call):
            return None
        func = value.func
        if isinstance(func, ast.Attribute):
            base = self.resolve(func.value, depth + 1)
            if isinstance(base, _Inst):
                target = getattr(base.cls, func.attr, None)
            else:
                target = getattr(base, func.attr, None) if base is not None else None
        else:
            target = self.resolve(func, depth + 1)
        if target is None or not callable(target):
            return None
        ret = _returns(target)
        return _Inst(ret) if ret is not None else None

    # ---- checks ---------------------------------------------------------- #
    def visit_Attribute(self, node: ast.Attribute) -> None:
        base = self.resolve(node.value)
        if base is not None and _policed(base):
            if isinstance(base, _Inst):
                ok = node.attr in _instance_attrs(base.cls)
                # A new attribute on a plain Python object is legal; on a proto it raises.
                if not ok and (isinstance(node.ctx, ast.Load) or _is_proto(base.cls)):
                    self.missing_attrs.append(
                        f"{self.path}:{node.lineno}: <{_label(base)} instance>.{node.attr}")
            elif not hasattr(base, node.attr):
                self.missing_attrs.append(f"{self.path}:{node.lineno}: {_label(base)}.{node.attr}")
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        self._check_probe(node)
        self._check_bind(node)
        self.generic_visit(node)

    def _check_probe(self, node: ast.Call) -> None:
        if not (isinstance(node.func, ast.Name) and node.func.id in ("getattr", "hasattr")
                and len(node.args) >= 2 and isinstance(node.args[1], ast.Constant)
                and isinstance(node.args[1].value, str)):
            if isinstance(node.func, ast.Attribute) and node.func.attr == "HasField" \
                    and node.args and isinstance(node.args[0], ast.Constant):
                base = self.resolve(node.func.value)
                if isinstance(base, _Inst) and _is_proto(base.cls) \
                        and node.args[0].value not in base.cls.DESCRIPTOR.fields_by_name:
                    self.missing_attrs.append(
                        f"{self.path}:{node.lineno}: <{_label(base)}>.HasField({node.args[0].value!r})")
            return
        base = self.resolve(node.args[0])
        if base is None or not _policed(base):
            return
        attr = node.args[1].value
        ok = attr in _instance_attrs(base.cls) if isinstance(base, _Inst) else hasattr(base, attr)
        self.probes.append((_label(base), attr, ok, node.lineno))

    def _check_bind(self, node: ast.Call) -> None:
        if any(isinstance(a, ast.Starred) for a in node.args) or \
                any(k.arg is None for k in node.keywords):
            return
        func = node.func
        placeholder_self = False
        if isinstance(func, ast.Attribute):
            base = self.resolve(func.value)
            if base is None or not _policed(base):
                return
            if isinstance(base, _Inst):
                target = inspect.getattr_static(base.cls, func.attr, None)
                if isinstance(target, (staticmethod, classmethod)):
                    target = getattr(base.cls, func.attr)
                elif inspect.isfunction(target):
                    placeholder_self = True
                else:
                    return  # proto / builtin methods: no reliable signature
            else:
                target = getattr(base, func.attr, None)
            owner = _label(base)
        else:
            target = self.resolve(func)
            if target is None or not _policed(target):
                return
            owner = "<call>"
        if target is None or not callable(target):
            return
        try:
            sig = inspect.signature(target)
        except (TypeError, ValueError):
            return
        args = [None] * (len(node.args) + (1 if placeholder_self else 0))
        kwargs = {k.arg: None for k in node.keywords}
        name = getattr(target, "__name__", "?")
        try:
            sig.bind(*args, **kwargs)
        except TypeError as exc:
            self.bind_failures.append(
                f"{self.path}:{node.lineno}: {owner}.{name}({ast.unparse(node)[len(ast.unparse(func)):]}) "
                f"does not bind to {name}{sig}: {exc}")
        else:
            self.binds.append((owner, name, node.lineno))


def _is_repeated(field) -> bool:
    flag = getattr(field, "is_repeated", None)
    if flag is not None:  # protobuf >= 6: property (or method on some builds)
        return bool(flag() if callable(flag) else flag)
    return field.label == field.LABEL_REPEATED


def _module_name_for(rel_path: str) -> Optional[str]:
    if not rel_path.startswith("inspector_widget/"):
        return os.path.splitext(rel_path)[0]  # cli / mcp_server are importable top-level modules
    mod = os.path.splitext(rel_path)[0].replace("/", ".")
    return mod[: -len(".__init__")] if mod.endswith(".__init__") else mod


def _scan_signatures(rel_path: str, source: Optional[str] = None) -> _SignatureScan:
    path = os.path.join(_HOST_DIR, rel_path)
    if source is None:
        with open(path, "r", encoding="utf-8") as f:
            source = f.read()
    tree = ast.parse(source, filename=path)
    scan = _SignatureScan(rel_path, _module_name_for(rel_path))
    scan.collect_imports(tree)
    scan.visit(tree)
    return scan


@pytest.fixture(scope="module")
def signature_scans() -> Dict[str, _SignatureScan]:
    return {rel: _scan_signatures(rel) for rel in _SIG_SOURCES}


def test_calls_into_host_code_bind_to_the_real_signatures(signature_scans) -> None:
    failures = [f for s in signature_scans.values() for f in s.bind_failures]
    assert not failures, "call(s) that would raise TypeError at runtime:\n  " + "\n  ".join(failures)


def test_attribute_reads_on_host_objects_resolve(signature_scans) -> None:
    missing = [m for s in signature_scans.values() for m in s.missing_attrs]
    assert not missing, "attribute(s) that do not exist:\n  " + "\n  ".join(missing)


def test_getattr_probes_resolve_or_are_documented(signature_scans) -> None:
    unexplained = [
        f"{rel}:{line}: getattr({owner}, {attr!r})"
        for rel, scan in signature_scans.items()
        for owner, attr, ok, line in scan.probes
        if not ok and (owner, attr) not in _KNOWN_OPTIONAL_PROBES
        and (owner, attr) not in _XFAIL_PROBES
    ]
    assert not unexplained, (
        "getattr()/hasattr() probes for attributes that don't exist (a silent None at "
        "runtime):\n  " + "\n  ".join(unexplained))


def _probe_misses(scans, ledger_id: str) -> List[str]:
    return [f"{rel}:{line}: getattr({owner}, {attr!r})"
            for rel, scan in scans.items() for owner, attr, ok, line in scan.probes
            if not ok and _XFAIL_PROBES.get((owner, attr)) == ledger_id]


def test_attach_metadata_and_liveness_resolve() -> None:
    """E9 / E2: what tool_attach reports and what the session cache probes exist."""
    import inspector_widget
    session_attrs = _instance_attrs(inspector_widget.Session)
    for attr in ("api_level", "abi", "agent_version", "pid", "warm", "info", "is_alive",
                 "disconnect", "shutdown"):
        assert attr in session_attrs, f"Session lacks {attr}"


def test_signature_scan_actually_checks_the_contract_calls(signature_scans) -> None:
    """Guard the guard: the scan must really bind these cross-module calls."""
    seen = {(owner, name) for s in signature_scans.values() for owner, name, _ in s.binds}
    expected = {
        ("inject", "inject_and_connect"),   # cli raw-Client subcommands
        ("<call>", "Client"),               # cli / inject / Session constructing a Client
        ("Client", "dump_tree"),            # Session -> Client kwarg translation
        ("Client", "dump_compose"),
        ("Client", "a11y_focus"),           # Session -> Client: the TalkBack focus long-poll
        ("Client", "a11y_act"),
        ("Session", "dump_tree"),           # mcp_server / correlate -> Session
        ("Session", "get_properties"),
        ("correlate", "inspect_node"),
        ("correlate", "component_image"),
        ("skia_client", "per_component_images"),
        ("overlay", "render_a11y_overlay"),
        ("a11y_lint", "run_lint"),          # cli / mcp_server -> the unified lint
        ("device", "action"),               # talkback tool / subcommand
        ("walk", "run_walk"),               # tb_walk
        ("scenarios", "run_scenario"),      # tb_scenario
        ("Session", "dump_a11y"),           # the walk's focus reader
        ("adb", "shell"),                   # talkback/device.py
        ("diff", "analyze"),
        ("results", "dump_tree"),           # mcp_server -> the shared result shapes
        ("results", "get_properties"),
        ("results", "with_target"),
        ("strings", "dump_tree_to_dict"),   # mcp_server's dump_tree (E3: no second decoder)
        ("strings", "get_properties_to_dict"),
    }
    assert expected <= seen, f"scan no longer checks: {sorted(expected - seen)}"
    probes = {(o, a) for s in signature_scans.values() for o, a, _ok, _l in s.probes}
    assert ("Session", "capture_skp") in probes


def test_signature_scan_catches_seeded_bugs() -> None:
    """The checker reports each bug class it exists for."""
    source = textwrap.dedent('''
        import inspector_widget as iw
        from inspector_widget import correlate, inject
        from inspector_widget.client import Client

        def run(sock, session):
            correlate.inspect_node(session, nod_key="view:1")        # kwarg typo
            client = Client(sock, True, None, "extra")               # arity
            client.dump_tree(root=0)                                 # Session-style kwarg on Client
            session.dump_tree(properties=True)                       # Client-style kwarg on Session
            session.no_such_method()                                 # missing method
            inj = inject.inject_and_connect(serial="s", package="p")
            inj.no_such_field                                        # missing dataclass field
            hello = client.hello()
            hello.agent_versoin                                      # proto field typo
            resp = client.screenshot()
            resp.HasField("screnshot")                               # HasField typo
            getattr(session, "api_levle", None)                      # probe typo
            iw.atach("s", "p")                                       # package attribute typo
    ''')
    scan = _scan_signatures("cli.py", source)
    joined = "\n".join(scan.bind_failures + scan.missing_attrs)
    for needle in ("nod_key", "'extra'", "root=0", "properties=True", "no_such_method",
                   "no_such_field", "agent_versoin", "screnshot", "atach"):
        assert needle in joined, f"seeded bug {needle!r} not reported:\n{joined}"
    assert ("Session", "api_levle", False, 18) in scan.probes
