# Inspector Widget host driver — convenience targets.
#
# Usage:
#   make proto     regenerate the Python protobuf bindings
#   make deps      install host Python dependencies (protobuf)
#   make devices   list attached devices
#   make clean     remove generated protobuf bindings

HERE := $(dir $(abspath $(lastword $(MAKEFILE_LIST))))
REPO_ROOT := $(abspath $(HERE)/..)
PROTO_DIR := $(REPO_ROOT)/proto
PROTO_FILE := $(PROTO_DIR)/view_inspection.proto
OUT_DIR := $(HERE)inspector_widget/proto
PB2 := $(OUT_DIR)/view_inspection_pb2.py

PYTHON ?= python3
PROTOC ?= protoc

.PHONY: proto deps devices clean

proto: $(PB2)

$(PB2): $(PROTO_FILE)
	@mkdir -p $(OUT_DIR)
	$(PROTOC) --proto_path=$(PROTO_DIR) --python_out=$(OUT_DIR) $(PROTO_FILE)
	@echo "generated $(PB2)"

deps:
	$(PYTHON) -m pip install -r $(HERE)requirements.txt

devices:
	$(PYTHON) $(HERE)cli.py devices

clean:
	rm -f $(PB2)
