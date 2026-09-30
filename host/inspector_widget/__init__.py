"""Inspector Widget host driver — a standalone Android View Layout Inspector host.

Public surface:
  - :mod:`inspector_widget.adb`      thin adb CLI wrappers
  - :mod:`inspector_widget.framing`  VWSPCT01 message framing
  - :mod:`inspector_widget.inject`   inject + connect to the on-device agent
  - :mod:`inspector_widget.client`   synchronous request/response Client
  - :mod:`inspector_widget.png`      Screenshot -> PNG decoding
  - :mod:`inspector_widget.strings`  string-table resolution + tree-to-dict
"""

from __future__ import annotations

from typing import Optional

__version__ = "1.0.0"

# Re-export the most commonly used entry points for convenience. Submodules that
# need protobuf import it lazily, so `import inspector_widget` stays lightweight.
from . import adb, framing  # noqa: F401

__all__ = [
    "adb",
    "framing",
    "list_devices",
    "list_processes",
    "attach",
    "connect_existing",
    "Session",
    "__version__",
]


# --------------------------------------------------------------------------- #
# High-level facade consumed by host/mcp_server.py and host/cli.py.
# Bridges the module-level API the MCP server expects
# (list_devices / list_processes / attach -> session) onto the lower-level
# adb + inject + client primitives, translating the kwarg differences between
# the MCP session contract and inspector_widget.client.Client.
# --------------------------------------------------------------------------- #
def list_devices():
    """[{serial, state, api_level, abi}] for every attached device."""
    out = []
    for dev in adb.devices():
        api = abi = model = None
        if dev.state == "device":
            try:
                api = adb.shell(dev.serial, "getprop ro.build.version.sdk").strip()
                abi = adb.shell(dev.serial, "getprop ro.product.cpu.abi").strip()
                model = adb.shell(dev.serial, "getprop ro.product.model").strip()
            except Exception:
                pass
        api_int = int(api) if api and api.isdigit() else api
        out.append({
            "serial": dev.serial,
            "state": dev.state,
            "api": api_int,
            "api_level": api_int,
            "abi": abi,
            "model": model,
        })
    return out


def list_processes(serial: str = None):
    """[{package, pid, running}] for every debuggable third-party package.

    ``serial`` ``None`` picks ``$ANDROID_SERIAL`` or the only attached device.
    """
    serial = adb.resolve_serial(serial)
    out = []
    for pkg in adb.list_debuggable_packages(serial):
        pid = None
        try:
            pid = adb.pidof(serial, pkg)
        except Exception:
            pass
        out.append({"package": pkg, "pid": pid, "running": pid is not None})
    return out


class Session:
    """A live inspection session over an injected agent.

    Wraps an :class:`inject.Injection` and exposes the method surface the MCP
    server drives, delegating to a :class:`client.Client` and translating the
    keyword names (``include_*`` / ``screenshot_scale`` -> the Client's
    ``properties`` / ``resolution_stack`` / ``scale``).

    Lifecycle: :meth:`disconnect` (also ``close()`` and leaving a ``with``
    block) drops this connection and leaves the agent running for the next
    caller; :meth:`shutdown` stops the agent for every client. :meth:`is_alive`
    says whether the connection is still usable and the app still has the same
    pid.
    """

    def __init__(self, injection):
        from .client import Client
        self.injection = injection
        self.serial = injection.serial
        self.package = injection.package
        self.pid = injection.pid
        self.warm = bool(injection.warm)
        hello = getattr(injection, "hello", None)
        self.agent_version = injection.agent_version
        self.build_id = injection.build_id
        self.api_level = hello.api_level if hello is not None else None
        self.abi = hello.abi if hello is not None else None
        # A warning worth passing on (e.g. a stale-build agent kept because
        # other clients use it), or None.
        self.note = getattr(injection, "note", None)
        self.client = Client(injection.sock, owns_socket=False)

    # ------------------------------------------------------------------ #
    # Metadata / health
    # ------------------------------------------------------------------ #
    def info(self) -> dict:
        """What attach reports: device, pid, warm/cold and the agent's Hello."""
        return {
            "serial": self.serial,
            "package": self.package,
            "pid": self.pid,
            "warm": self.warm,
            "agent_version": self.agent_version,
            "build_id": self.build_id,
            "api_level": self.api_level,
            "abi": self.abi,
        }

    @property
    def closed(self) -> bool:
        return bool(self.injection.closed or self.client.broken)

    def is_alive(self) -> bool:
        """True while the connection is open and the app still runs under ``pid``.

        Costs one ``adb shell pidof``; a dropped connection is detected locally.
        """
        if self.closed or not self.client.is_open():
            return False
        try:
            return adb.pidof(self.serial, self.package) == self.pid
        except Exception:
            return False

    # ------------------------------------------------------------------ #
    # Commands
    # ------------------------------------------------------------------ #
    def dump_tree(self, root_id: int = 0, include_properties: bool = False,
                  include_resolution_stack: bool = False,
                  include_screenshot: bool = False, screenshot_scale: float = 1.0):
        return self.client.dump_tree(
            root_id=root_id, properties=include_properties,
            resolution_stack=include_resolution_stack,
            screenshot=include_screenshot, scale=screenshot_scale)

    def get_properties(self, view_id: int, include_resolution_stack: bool = False):
        return self.client.get_properties(view_id, resolution_stack=include_resolution_stack)

    def screenshot(self, root_id: int = 0, scale: float = 1.0):
        return self.client.screenshot(root_id=root_id, scale=scale)

    def get_windows(self):
        return self.client.get_windows()

    def dump_compose(self, root_view_id: int = 0, include_semantics: bool = True,
                     include_slot_table: bool = True, enable_inspection: bool = False):
        # enable_inspection is opt-in everywhere (Session, CLI, MCP): populating the
        # slot table hot-reloads every composition in the process, which resets
        # remember{} state. See strings.ENABLE_INSPECTION_WARNING.
        return self.client.dump_compose(
            root_view_id=root_view_id, include_semantics=include_semantics,
            include_slot_table=include_slot_table, enable_inspection=enable_inspection)


    def dump_a11y(self, root_id: int = 0, include_extras: bool = True,
                  include_rendering_info: bool = False):
        return self.client.dump_a11y(
            root_id=root_id, include_extras=include_extras,
            include_rendering_info=include_rendering_info)

    def a11y_focus(self, after_seq: int = 0, wait_ms: int = 0, quiet_ms: int = 0,
                   include_input_focus: bool = False, subtree_depth: int = 0,
                   max_events: int = 0):
        """Accessibility focus now (``wait_ms`` 0), or after the next focus move (a long-poll).

        Returns the ``A11yFocusResponse`` proto; ``a11y.a11y_focus_to_dict`` shapes it.
        Pass its ``seq`` as the next ``after_seq``. A long-poll's deadline is the base
        deadline plus ``wait_ms + quiet_ms``, so a slow step is not mistaken for a frozen app.
        """
        return self.client.a11y_focus(
            after_seq=after_seq, wait_ms=wait_ms, quiet_ms=quiet_ms,
            include_input_focus=include_input_focus, subtree_depth=subtree_depth,
            max_events=max_events)

    def a11y_act(self, node_key=None, action="accessibility_focus", host_view_id: int = None,
                 virtual_id: int = -1, raw_action_id: int = 0, args: dict = None,
                 subtree_depth: int = 0):
        """Perform an accessibility action on one node; returns the ``A11yActResponse`` proto.

        Name the node by ``node_key`` (``view:<id>``, ``compose:<acv>:<id>``,
        ``virtual:<host>:<id>``, ``composeview:<acv>``) or by ``host_view_id`` +
        ``virtual_id`` (-1 = the View itself). ``action``: see ``client.node_action``.
        ``a11y.a11y_act_to_dict`` shapes the reply.
        """
        if node_key is not None:
            from . import a11y as _a11y
            parsed = _a11y.parse_node_key(node_key)
            if parsed[0] in ("view", "composeview"):
                host_view_id, virtual_id = parsed[1], -1
            elif parsed[0] == "virt":
                host_view_id, virtual_id = parsed[1], parsed[2]
            else:
                raise ValueError(f"node key {node_key!r} names no ComposeView; use "
                                 "compose:<acvId>:<semanticsId> from a dump")
        if host_view_id is None:
            raise ValueError("a11y_act needs node_key or host_view_id")
        return self.client.a11y_act(
            host_view_id=host_view_id, virtual_id=virtual_id, action=action,
            raw_action_id=raw_action_id, args=args, subtree_depth=subtree_depth)

    def capture_skp(self, root_id: int = 0):
        return self.client.capture_skp(root_id=root_id)

    def hello(self):
        return self.client.hello()

    # ------------------------------------------------------------------ #
    # Lifecycle
    # ------------------------------------------------------------------ #
    def disconnect(self) -> None:
        """Drop this connection (socket + adb forward). The agent keeps running,
        so the next attach is a cheap warm connect. Idempotent.

        A request in flight on another thread fails at once with
        :class:`~inspector_widget.client.SessionLostError` rather than waiting
        out its deadline.
        """
        closed = self.client.abort("disconnected")
        # If a request is in flight, its thread closes the socket as it fails;
        # closing it under that thread could leave it waiting out its deadline.
        self.injection.close(close_socket=closed)

    close = disconnect

    def shutdown(self, wait: Optional[float] = None) -> bool:
        """Stop the agent for EVERY client (SHUTDOWN), then disconnect.

        If this connection can't carry the SHUTDOWN (it is already closed), a
        fresh connection to the same pid does. Returns True once nothing
        listens on the agent's socket any more (within ``wait`` seconds,
        default ``inject.STOP_WAIT``), False if the agent is still there (it
        didn't stop, or couldn't be reached).
        """
        from . import inject
        from .client import AgentTimeoutError, ClientError, NotSentError
        wait = inject.STOP_WAIT if wait is None else wait
        sent = False
        try:
            self.client.shutdown(timeout=wait)
            sent = True
        except NotSentError:
            pass
        except AgentTimeoutError:
            sent = True  # delivered but unanswered (a busy agent may still stop)
        except ClientError:
            self.disconnect()
            return False  # the agent answered with an error: it did not stop
        self.disconnect()
        if sent:
            return inject._wait_for_socket_gone(self.serial, self.injection.socket_name, wait)
        try:
            fresh = inject._try_warm_connect(self.serial, self.pid, self.package)
        except Exception:
            fresh = None
        if fresh is None:
            # Nothing answered: stopped already, unless something still listens.
            return not adb.socket_exists(self.serial, self.injection.socket_name)
        return inject.stop_agent(fresh, wait=wait)

    # Old name: detach() always meant "send SHUTDOWN". Prefer shutdown().
    detach = shutdown

    def __enter__(self) -> "Session":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.disconnect()


def attach(serial: str = None, package: str = "com.oberkfell.a11yprobe", build_out=None,
           force_reinject: bool = False) -> "Session":
    """Inject (or warm-reconnect) the agent into ``package`` and return a Session.

    ``serial`` ``None`` picks ``$ANDROID_SERIAL`` or the only attached device.
    ``build_out`` is the artifacts directory; ``None`` resolves it from
    ``$INSPECTOR_WIDGET_ARTIFACTS`` / ``$VIEWSPECTOR_ARTIFACTS`` / the repo's
    ``build-out/`` (see :func:`inspector_widget.inject.resolve_build_out`).
    ``force_reinject`` stops a running agent and injects a fresh one.

    Use it as a context manager (or call :meth:`Session.disconnect`) to release
    the connection while leaving the agent warm; :meth:`Session.shutdown`
    stops the agent.
    """
    from . import inject
    injection = inject.inject_and_connect(serial=serial, package=package, build_out=build_out,
                                          force_reinject=force_reinject)
    return Session(injection)


def connect_existing(serial: str = None, package: str = "com.oberkfell.a11yprobe"):
    """A Session on an agent already running in ``package``, or ``None``.

    Never injects: this is how ``detach`` reaches an agent to stop it.
    """
    from . import inject
    injection = inject.connect_existing(serial, package)
    return Session(injection) if injection is not None else None
