import __future__

import ast
import os
import shutil
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SOURCE = ROOT / "scripts/export_closures.py"


def load(names, **extras):
    tree = ast.parse(SOURCE.read_text())
    tree.body = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name in names]
    env = dict(Path=Path, tempfile=tempfile, shutil=shutil, os=os)
    env.update(extras)
    exec(compile(tree, str(SOURCE), "exec", flags=__future__.annotations.compiler_flag), env)
    return env


@pytest.fixture
def pair(tmp_path):
    return (tmp_path / "sample.csv", tmp_path / "sample.jsonl")


def seed(pair, present):
    old = (b"old,csv\n", b'{"old":true}\n')
    for path, exists, blob in zip(pair, present, old, strict=True):
        if exists:
            path.write_bytes(blob)
    return old


def preserved(pair, present, old):
    for path, exists, blob in zip(pair, present, old, strict=True):
        assert path.exists() == exists
        if exists:
            assert path.read_bytes() == blob


@pytest.mark.parametrize("present", [(True, True), (False, False), (True, False), (False, True)])
@pytest.mark.parametrize(
    "failure", ["new-0", "new-1", "old-0", "old-1", "verify", "rename-0", "rename-1"]
)
def test_failure_restores_complete_prior_state(pair, present, failure, monkeypatch):
    if failure.startswith("old-") and not present[int(failure[-1])]:
        pytest.skip("No backup write exists for an absent destination")
    old = seed(pair, present)
    original_write = Path.write_bytes
    original_replace = os.replace

    def write(path, blob):
        result = original_write(path, blob)
        if path.name == failure:
            raise OSError("synthetic disk failure after partial staging/backup write")
        if failure == "verify" and path.name == "new-1":
            original_write(path, b"corrupted staged output")
        return result

    def replace(src, dst):
        if failure == f"rename-{Path(src).name[-1]}" and Path(src).name.startswith("new-"):
            raise OSError("synthetic rename failure")
        return original_replace(src, dst)

    monkeypatch.setattr(Path, "write_bytes", write)
    monkeypatch.setattr(os, "replace", replace)
    with pytest.raises(OSError):
        load({"replace_pair"})["replace_pair"](pair, (b"new,csv\n", b'{"new":true}\n'))
    preserved(pair, present, old)
    assert not list(pair[0].parent.glob(".closures-pair-*"))


@pytest.mark.parametrize("present", [(True, True), (False, False)])
def test_success_publishes_one_serialized_generation(pair, present):
    seed(pair, present)
    blobs = (b"new,csv\n", b'{"new":true}\n')
    load({"replace_pair"})["replace_pair"](pair, blobs)
    assert tuple(p.read_bytes() for p in pair) == blobs
    assert not list(pair[0].parent.glob(".closures-pair-*"))


@pytest.mark.parametrize("present", [(True, True), (False, False)])
def test_second_serialization_failure_never_stages_or_changes(pair, present):
    old = seed(pair, present)

    def fail(*args):
        raise ValueError("synthetic second output serialization failure")

    def forbidden(*args):
        pytest.fail("staging called before both outputs serialized")

    env = load(
        {"write_pair"}, to_csv=lambda *a: b"new,csv\n", to_jsonl=fail, replace_pair=forbidden
    )
    with pytest.raises(ValueError):
        env["write_pair"](pair[0].with_suffix(""), [], (), gz=False, preserve_pair=True)
    preserved(pair, present, old)


@pytest.mark.parametrize("present", [(True, True), (False, False)])
def test_rollback_failure_is_loud_and_retains_recovery(pair, present, monkeypatch):
    old = seed(pair, present)
    original_replace = os.replace
    original_unlink = Path.unlink

    def replace(src, dst):
        if Path(src).name in ("new-1", "old-0"):
            raise OSError("synthetic commit/rollback failure")
        return original_replace(src, dst)

    def unlink(path, *a, **kw):
        if path == pair[0]:
            raise OSError("synthetic rollback unlink failure")
        return original_unlink(path, *a, **kw)

    monkeypatch.setattr(os, "replace", replace)
    monkeypatch.setattr(Path, "unlink", unlink)
    with pytest.raises(RuntimeError, match="do not publish; recovery files retained"):
        load({"replace_pair"})["replace_pair"](pair, (b"new,csv\n", b'{"new":true}\n'))
    retained = list(pair[0].parent.glob(".closures-pair-*"))
    assert len(retained) == 1
    assert (retained[0] / "new-1").read_bytes() == b'{"new":true}\n'
    if present[0]:
        assert (retained[0] / "old-0").read_bytes() == old[0]
    # Failed rollback is explicitly not claimed to preserve the published pair.
    assert pair[0].read_bytes() == b"new,csv\n"


def test_free_sample_selects_transactional_writer():
    calls = []
    row = {"d": "2026-10-03"}
    env = load(
        {"export"},
        recent_days=lambda r: r,
        publishable=lambda r, b: r,
        FREE_FIELDS=("d",),
        write_pair=lambda *a, **kw: calls.append(kw),
    )
    assert env["export"]([row], [], Path("/unused"), sample=True, blocked=set())["rows"] == 1
    assert calls == [{"gz": False, "preserve_pair": True}]
