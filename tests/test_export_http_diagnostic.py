"""Offline tests of the exact main function AST, with all I/O replaced by fail-fast stubs."""

import argparse
import ast
import json
import os
import sys
import urllib.error
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
CASES = json.loads((ROOT / "tests/fixtures/exporter/http-errors.json").read_text())["cases"]
SOURCE = Path(os.environ.get("EXPORT_DIAGNOSTIC_SOURCE", str(ROOT / "scripts/export_closures.py")))


@pytest.mark.parametrize("case", CASES, ids=lambda c: str(c["status"]))
@pytest.mark.parametrize("stage", ["list_keys", "counts", "events", "baseline"])
def test_http_error_has_accurate_message_without_export_or_retry(case, stage, tmp_path):
    tree = ast.parse(SOURCE.read_text())
    main = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "main")
    calls = []

    def io_call(name):
        calls.append(name)
        if name == stage:
            raise urllib.error.HTTPError(
                "https://fixture.invalid/resource", case["status"], case["reason"], {}, None
            )

    def list_keys(*args):
        io_call("list_keys")
        return ["counts.2026-10-01", "counts.2026-10-02", "events.2026-10-02"]

    def read_jsonl(*args):
        name = ["counts", "events", "baseline"][len(calls) - 1]
        io_call(name)
        return []

    def forbidden(*args, **kwargs):
        pytest.fail(
            "No export, filesystem write or downstream processing is allowed after HTTP failure"
        )

    env = dict(
        argparse=argparse,
        Path=Path,
        REPO=tmp_path,
        sys=sys,
        urllib=urllib,
        __doc__="Offline diagnostic test",
        STORE_NAME="fixture",
        SAMPLE_DAYS=3,
        FREE_FIELDS=(),
        api_token=lambda: "synthetic-never-sent",
        list_keys=list_keys,
        read_jsonl=read_jsonl,
        day_keys=lambda *a: [],
        summarise=forbidden,
        first_days=forbidden,
        export=forbidden,
        read_blocklist=forbidden,
    )
    exec(
        compile(
            ast.fix_missing_locations(ast.Module(body=[main], type_ignores=[])), str(SOURCE), "exec"
        ),
        env,
    )
    with pytest.raises(SystemExit) as err:
        env["main"](["--sample", "--out", str(tmp_path / "must-not-exist")])
    assert str(err.value) == f"Apify API {case['status']}: {case['reason']} — {case['hint']}"
    assert (
        calls
        == ["list_keys", "counts", "events", "baseline"][
            : ["list_keys", "counts", "events", "baseline"].index(stage) + 1
        ]
    )
    assert not (tmp_path / "must-not-exist").exists()
