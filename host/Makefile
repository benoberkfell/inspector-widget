# Inspector Widget host driver — convenience targets.
#
# Usage:
#   make proto     regenerate the Python protobuf bindings (via generate_proto.sh,
#                  which refuses a protoc that doesn't match the checked-in gencode)
#   make deps      install host Python dependencies (protobuf)
#   make devices   list attached devices
#   make clean     remove Python build/test byproducts (never the tracked gencode)

HERE := $(dir $(abspath $(lastword $(MAKEFILE_LIST))))
REPO_ROOT := $(abspath $(HERE)/..)
PROTO_DIR := $(REPO_ROOT)/proto
PROTO_FILE := $(PROTO_DIR)/view_inspection.proto
OUT_DIR := $(HERE)inspector_widget/proto
PB2 := $(OUT_DIR)/view_inspection_pb2.py

PYTHON ?= python3
# Passed through to generate_proto.sh; may be a command with args,
# e.g. PROTOC="python -m grpc_tools.protoc".
PROTOC ?= protoc

.PHONY: proto deps devices clean

proto: $(PB2)

$(PB2): $(PROTO_FILE)
	PROTOC="$(PROTOC)" bash $(HERE)generate_proto.sh

deps:
	$(PYTHON) -m pip install -r $(HERE)requirements.txt

devices:
	$(PYTHON) $(HERE)cli.py devices

# view_inspection_pb2.py is tracked in git and pins the protobuf runtime the
# package needs, so clean must not delete it (regenerate with `make proto`).
clean:
	rm -rf $(HERE)build $(HERE)dist $(HERE)*.egg-info $(HERE).pytest_cache
	find $(HERE) -name __pycache__ -type d -not -path '*/.venv/*' -prune -exec rm -rf {} +
