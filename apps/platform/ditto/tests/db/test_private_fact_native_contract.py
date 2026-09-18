"""Native serializer -> Platform pin contract; never semantic qualification."""

import json
import os
import shutil
import subprocess
from pathlib import Path
from uuid import uuid4

import pytest

from ditto.db.queries.private_benchmark_datasets import (
    PrivateDatasetError,
    PrivateDatasetIdentity,
    _validate,
    find_private_dataset,
    pin_private_dataset,
)


@pytest.fixture(scope="module")
def native_candidates(tmp_path_factory):
    root = tmp_path_factory.mktemp("native-fact-contract")
    root.chmod(0o700)
    go = shutil.which("go")
    assert go is not None, "Go is required for the native producer contract test"
    repo = Path(__file__).resolve().parents[5]
    # Never pass provider/admin/cloud credentials to this test process. Its
    # test-only renderer generates structural fixtures without network calls.
    environment = {
        key: os.environ[key]
        for key in ("PATH", "HOME", "GOCACHE", "GOPATH")
        if key in os.environ
    }
    environment["DITTO_FACT_CONTRACT_OUTPUT"] = str(root)
    subprocess.run(
        [
            go,
            "test",
            "./cmd/private-produce",
            "-run",
            "^TestFactProducerPlatformContract$",
            "-count=1",
            "-timeout=3m",
        ],
        cwd=repo / "research/dittobench-datagen",
        env=environment,
        check=True,
        capture_output=True,
        timeout=200,
    )
    return root


@pytest.mark.parametrize("size", ["small", "medium", "full"])
async def test_native_fact_output_pins_and_reloads(
    native_candidates, session_maker, size
):
    directory = native_candidates / size
    key = PrivateDatasetIdentity(
        str(uuid4()), 42, size, "a" * 64, generation_mode="fact-world-v1"
    )
    values = {
        "base_bytes": (directory / "generation.json").read_bytes(),
        "dataset_bytes": (directory / "dataset.json").read_bytes(),
        "validation_receipt_bytes": (directory / "validation.json").read_bytes(),
    }
    async with session_maker() as session, session.begin():
        pinned = await pin_private_dataset(session, identity=key, **values)
    async with session_maker() as session:
        assert await find_private_dataset(session, identity=key) == pinned
    receipt = json.loads(pinned.validation_receipt_bytes)
    assert receipt["qualified"] is False and receipt["replay_verified"] is True
    assert json.loads(pinned.base_bytes)["world_seed"] == -731
    # The producer's diagnostic candidate receipt is NOT publication evidence.
    with pytest.raises(PrivateDatasetError):
        _validate(
            key,
            values["base_bytes"],
            values["dataset_bytes"],
            (directory / "fact-candidate.json").read_bytes(),
        )
    with pytest.raises(PrivateDatasetError):
        _validate(
            key,
            values["base_bytes"],
            values["dataset_bytes"] + b" ",
            values["validation_receipt_bytes"],
        )
