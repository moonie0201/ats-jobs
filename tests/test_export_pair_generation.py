import csv
import json

import pytest

from .test_export_closures import ex


@pytest.mark.parametrize("existing", [True, False])
def test_csv_and_jsonl_decode_to_same_expected_generation(tmp_path, existing):
    stem = tmp_path / "sample" / "closures-72h"
    stem.parent.mkdir()
    if existing:
        stem.with_suffix(".csv").write_bytes(b"previous CSV")
        stem.with_suffix(".jsonl").write_bytes(b"previous JSONL")
    rows = [
        {
            "d": "2026-10-03",
            "provider": "fixture",
            "company": "synthetic",
            "open": 4,
            "added": None,
            "removed": 2,
        }
    ]
    ex.write_pair(stem, rows, ex.FREE_FIELDS, gz=False, preserve_pair=True)
    with stem.with_suffix(".csv").open(newline="") as file:
        csv_rows = list(csv.DictReader(file))
    json_rows = [json.loads(line) for line in stem.with_suffix(".jsonl").read_text().splitlines()]
    assert csv_rows == [
        {
            "d": "2026-10-03",
            "provider": "fixture",
            "company": "synthetic",
            "open": "4",
            "added": "",
            "removed": "2",
        }
    ]
    assert json_rows == rows
    assert len(csv_rows) == len(json_rows) == 1
    assert not list(stem.parent.glob(".closures-pair-*"))
