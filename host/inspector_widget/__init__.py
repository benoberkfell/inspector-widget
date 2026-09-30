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


def list_processes(serial: str):
    """[{package, pid, running}] for every debuggable third-party package."""
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
    """

    def __init__(self, injection):
        from .client import Client
        self.injection = injection
        self.serial = injection.serial
        self.package = injection.package
        self.pid = injection.pid
        self.client = Client(injection.sock, owns_socket=False)

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

    def hello(self):
        return self.client.hello()

    def detach(self):
        try:
            self.client.shutdown()
        except Exception:
            pass
        self.injection.close()

    # Aliases the MCP server may probe for.
    shutdown = detach
    close = detach


def attach(serial: str, package: str) -> "Session":
    """Inject (or warm-reconnect) the agent into ``package`` and return a Session."""
    from . import inject
    injection = inject.inject_and_connect(serial=serial, package=package)
    return Session(injection)
