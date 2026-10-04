import __future__

import argparse
import ast
import io
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parent.parent
SOURCE = ROOT / "scripts/export_closures.py"
SEQUENCES = json.loads((ROOT / "tests/fixtures/exporter/retry-responses.json").read_text())["cases"]


def functions(names, env):
    tree = ast.parse(SOURCE.read_text())
    tree.body = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name in names]
    exec(compile(tree, str(SOURCE), "exec", flags=__future__.annotations.compiler_flag), env)
    return env


@pytest.mark.parametrize("statuses", SEQUENCES, ids=lambda s: "-".join(map(str, s)))
def test_bounded_retry(statuses):
    calls, sleeps = [], []

    def urlopen(req, timeout):
        assert timeout == 90
        calls.append(1)
        status = statuses[len(calls) - 1]
        if status != 200:
            raise urllib.error.HTTPError("https://fixture.invalid/", status, "synthetic", {}, None)
        return io.BytesIO(b"valid fixture")

    fake_urllib = SimpleNamespace(
        error=urllib.error, request=SimpleNamespace(Request=urllib.request.Request, urlopen=urlopen)
    )
    env = functions(
        {"get"},
        dict(
            urllib=fake_urllib,
            API="https://fixture.invalid",
            time=SimpleNamespace(sleep=sleeps.append),
        ),
    )
    if statuses[-1] == 200:
        assert env["get"]("/fixture", "synthetic-never-sent") == b"valid fixture"
    else:
        with pytest.raises(urllib.error.HTTPError) as error:
            env["get"]("/fixture", "synthetic-never-sent")
        assert error.value.code == statuses[-1]
    assert len(calls) == len(statuses) <= 3
    assert sleeps == [1, 2][: len(statuses) - 1]


def last_good(tmp_path):
    stem = tmp_path / "sample"
    stem.mkdir()
    files = {
        stem / "closures-72h.csv": b"last-good,csv\n",
        stem / "closures-72h.jsonl": b'{"last-good":true}\n',
    }
    for path, blob in files.items():
        path.write_bytes(blob)
    return files


def assert_preserved(files):
    for path, blob in files.items():
        assert path.read_bytes() == blob


@pytest.mark.parametrize(
    "rows,blocked,deny",
    [
        ([], set(), set()),
        ([{"provider": "fixture", "company": "blocked"}], {("fixture", "blocked")}, set()),
        ([{"provider": "excluded", "company": "x"}], set(), {"excluded"}),
    ],
)
def test_empty_after_filter_never_calls_writer(rows, blocked, deny, tmp_path):
    files = last_good(tmp_path)

    def forbidden(*a, **kw):
        pytest.fail("write_pair must not be called, so no file is opened/truncated")

    env = functions(
        {"export", "publishable", "recent_days"},
        dict(SAMPLE_DAYS=3, FREE_FIELDS=(), PUBLISH_DENY_PROVIDERS=deny, write_pair=forbidden),
    )
    with pytest.raises(ValueError, match="existing files preserved"):
        env["export"](rows, [], tmp_path, sample=True, blocked=blocked)
    assert_preserved(files)


@pytest.mark.parametrize("stage", ["list_keys", "counts", "events", "baseline"])
def test_http_failure_after_partial_reads_preserves_both_files(stage, tmp_path):
    files = last_good(tmp_path)
    calls = []

    def fail_at(name):
        calls.append(name)
        if name == stage:
            raise urllib.error.HTTPError("https://fixture.invalid/", 502, "Bad Gateway", {}, None)

    def list_keys(*a):
        fail_at("list_keys")
        return ["counts.2026-10-01", "counts.2026-10-02"]

    def read_jsonl(*a):
        fail_at(["counts", "events", "baseline"][len(calls) - 1])
        return [{"synthetic": "previous successful partial read"}]

    def forbidden(*a, **kw):
        pytest.fail("export/write reached after a failed input request")

    env = functions(
        {"main"},
        dict(
            argparse=argparse,
            Path=Path,
            REPO=tmp_path,
            sys=sys,
            urllib=urllib,
            __doc__="Offline fixture",
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
        ),
    )
    with pytest.raises(SystemExit, match="502: Bad Gateway"):
        env["main"](["--sample", "--out", str(tmp_path)])
    assert_preserved(files)
