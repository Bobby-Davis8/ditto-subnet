"""Synthetic V15 runtime metadata carrying every consumed source-emission entry."""

from __future__ import annotations

import copy
from typing import Any

from ditto.chain.runtime_interface import (
    CONSUMED_CALLS,
    CONSUMED_EVENTS,
    CONSUMED_STORAGE,
)

U16, BOOL, VECTOR, PAIR, EVENTS, CALLS, SYSTEM_EVENTS, TREE = range(8)


def _variants(names: list[str]) -> dict[str, Any]:
    return {
        "variant": {
            "variants": [
                {
                    "name": name,
                    "index": index,
                    "fields": [
                        {
                            "name": "netuid",
                            "type": U16,
                            "typeName": "NetUid",
                            "docs": [],
                        }
                    ],
                    "docs": [f"{name} docs"],
                }
                for index, name in enumerate(names)
            ]
        }
    }


def metadata(*, epoch_consensus: bool = True) -> tuple[str, dict[str, Any]]:
    """Return a decoded ``MetadataVersioned`` value as the library exposes it."""
    subtensor_events = [n for p, n in CONSUMED_EVENTS if p == "SubtensorModule"]
    system_events = [n for p, n in CONSUMED_EVENTS if p == "System"]
    calls = [n for _, n in CONSUMED_CALLS]
    types = [
        (U16, {"primitive": "u16"}),
        (BOOL, {"primitive": "bool"}),
        (VECTOR, {"sequence": {"type": PAIR}}),
        (PAIR, {"tuple": [U16, U16]}),
        (EVENTS, _variants([*subtensor_events, "Unrelated"])),
        (CALLS, _variants(calls)),
        (SYSTEM_EVENTS, _variants(system_events)),
        # Recursive: a tree whose children are trees.
        (TREE, {"composite": {"fields": [{"name": "kids", "type": VECTOR}]}}),
    ]
    registry = [
        {
            "id": type_id,
            "type": {
                "path": [f"t{type_id}"],
                "params": [],
                "def": definition,
                "docs": [],
            },
        }
        for type_id, definition in types
    ]
    storage: dict[str, list[dict[str, Any]]] = {}
    for pallet, name in CONSUMED_STORAGE:
        if name == "SubnetEpochConsensus" and not epoch_consensus:
            continue
        storage.setdefault(pallet, []).append(
            {
                "name": name,
                "modifier": "Default",
                "type": {"Map": {"hashers": ["Identity"], "key": U16, "value": VECTOR}},
                "default": "\x00",
                "documentation": [f"{name} docs"],
            }
        )
    pallets = [
        {
            "name": "SubtensorModule",
            "storage": {
                "prefix": "SubtensorModule",
                "entries": storage["SubtensorModule"],
            },
            "calls": {"ty": CALLS},
            "event": {"ty": EVENTS},
            "index": 7,
        },
        {
            "name": "System",
            "storage": None,
            "calls": None,
            "event": {"ty": SYSTEM_EVENTS},
        },
        {
            "name": "Timestamp",
            "storage": {"prefix": "Timestamp", "entries": storage["Timestamp"]},
            "calls": None,
            "event": None,
        },
    ]
    return ("0x6d657461", {"V15": {"types": {"types": registry}, "pallets": pallets}})


def body(value: tuple[str, dict[str, Any]]) -> dict[str, Any]:
    return value[1]["V15"]


def clone(value: tuple[str, dict[str, Any]]) -> tuple[str, dict[str, Any]]:
    return copy.deepcopy(value)


def entry(value: tuple[str, dict[str, Any]], pallet: str, name: str) -> dict[str, Any]:
    for item in next(p for p in body(value)["pallets"] if p["name"] == pallet)[
        "storage"
    ]["entries"]:
        if item["name"] == name:
            return item
    raise KeyError(name)


def type_def(value: tuple[str, dict[str, Any]], type_id: int) -> dict[str, Any]:
    return next(t for t in body(value)["types"]["types"] if t["id"] == type_id)["type"]
