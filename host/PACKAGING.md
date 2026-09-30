# Packaging — Inspector Widget (local install)

Inspector Widget ships as a **locally installable** Python package. There is no
publishing, no network registry, and no credentials involved — you install it
straight from this checkout.

- Distribution / project name: **`inspector-widget`** (version `0.1.0`)
- Import package (unchanged for now): **`inspector_widget`**
- Console scripts: **`inspector-widget`** and **`inspector-widget-mcp`**

> The import package stays `inspector_widget` so the code runs today. A later
> mechanical pass renames `viewspector` → `inspector_widget`. Only the
> user-facing product name is "Inspector Widget".

## Install

From the repo root, install the package that lives under `host/`:

    # Editable install with everything (recommended for local dev):
    pip install -e 'host[all]'

    # Or pick extras a la carte:
    pip install -e 'host[mcp,images,overlay]'

    # Minimal (protobuf only — host driver + JSON-RPC MCP fallback):
    pip install -e host

`requires-python` is `>=3.10`.

### Extras

| Extra      | Pulls in        | Enables                                                              |
|------------|-----------------|---------------------------------------------------------------------|
| `mcp`      | `mcp>=1.19,<3`  | the real MCP stdio transport, SDK 1.x or 2.x (otherwise a built-in JSON-RPC fallback) |
| `images`   | `grpcio>=1.60`  | `inspector_widget.skia_client` → SKP image decoding                  |
| `overlay`  | `Pillow>=10`    | every overlay tool (`inspector_widget.overlay`) + the component-image crop fallback |
| `dev`      | `pytest`, `ruff`| tests + lint                                                        |
| `all`      | all of the above| convenience                                                         |

The only **required** runtime dependency is `protobuf>=6.33.5,<7` — it must
match the gencode in `inspector_widget/proto/view_inspection_pb2.py`, which calls
`ValidateProtobufRuntimeVersion` with version 6.33.5. An older runtime (e.g. 5.x)
raises at import time (see `CONTRACT.md`).

## Console scripts

After install, two commands are on `PATH`:

    inspector-widget --help          # the host CLI (host/cli.py)
    inspector-widget-mcp --self-check # the MCP server (host/mcp_server.py)

### Why the `_cli` / `_mcp` wrappers exist

`cli.py` and `mcp_server.py` currently live at **`host/`**, *beside* the
`inspector_widget` package rather than inside it — so they are **not** importable
as `inspector_widget.cli` / `inspector_widget.mcp_server`, and an entry point
cannot target them directly. We do not want to move them yet (the rename pass
owns file moves).

So the build does two things:

1. Ships `host/cli.py` and `host/mcp_server.py` in the wheel as **top-level
   importable modules** via `py-modules` in `[tool.setuptools]`:

       [tool.setuptools]
       py-modules = ["cli", "mcp_server"]

   After `pip install`, `import cli` / `import mcp_server` resolve from
   site-packages — no checkout on disk required.

2. Ships two thin in-package shims that the console scripts target:

   - `inspector_widget/_cli.py` → `main()` does `import cli` and calls
     `cli.main(argv)`.
   - `inspector_widget/_mcp.py` → `main()` does `import mcp_server` and calls
     `mcp_server.main(argv)`.

The entry points target the wrappers:

    inspector-widget     = inspector_widget._cli:main
    inspector-widget-mcp = inspector_widget._mcp:main

> Because `cli` and `mcp_server` are packaged as top-level modules, the console
> scripts work from a **plain wheel install** as well as an editable install —
> they no longer depend on the wrappers finding the loose scripts on disk. A
> later rename pass will move the scripts into the package and drop the wrappers.

## Package data

The build includes the generated bindings + stubs as package data so an install
is runnable without re-running `protoc`:

- `inspector_widget/proto/*.py`      — `view_inspection_pb2.py` (protobuf gencode)
- `inspector_widget/skia_grpc/*.py`  — `skia_pb2.py`, `skia_pb2_grpc.py` (gRPC stubs)

Regenerate the protobuf bindings with `make proto` (or `./generate_proto.sh`)
if `proto/view_inspection.proto` changes. Both need protoc 33.x: the script reads
the protoc release from the checked-in gencode header and refuses a different
major, since protoc 34+ emits gencode the `<7` runtime pin can't import.
`make clean` only removes build/test byproducts, never the tracked bindings.

## Runtime artifacts are NOT package data

The on-device agent artifacts are **build outputs**, not Python package data,
and are deliberately excluded from the wheel:

    build-out/libviewspector.so
    build-out/bootstrap.dex
    build-out/payload.jar

They are produced by the native/Gradle build (`scripts/build.sh`) and located at
runtime by `inspector_widget.inject.resolve_build_out`, first match wins:

1. the CLI's `--build-out DIR` flag (on every subcommand that injects);
2. `$INSPECTOR_WIDGET_ARTIFACTS`;
3. `$VIEWSPECTOR_ARTIFACTS` (legacy name);
4. `inject.DEFAULT_BUILD_OUT`: the repo's `build-out/`, three directories up from
   `inject.py`. This only works for an editable install from the checkout.

After a **wheel** install `inject.py` lives in site-packages, so the default points
nowhere useful: set `INSPECTOR_WIDGET_ARTIFACTS=<checkout>/build-out` (the MCP
server reads it too) or pass `--build-out`. `inspector-widget-mcp --self-check`
prints the directory it resolved and whether each artifact is there.
