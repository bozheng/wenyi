from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
DRIVER = (
    REPO_ROOT
    / ".codex"
    / "skills"
    / "wenyi-interactive-translate"
    / "scripts"
    / "interactive_translate.py"
)


def _run(*args: object, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(DRIVER), *(str(arg) for arg in args)],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        check=check,
    )


def _json_run(*args: object) -> dict:
    return json.loads(_run(*args).stdout)


def _start(tmp_path: Path) -> tuple[Path, dict]:
    source = tmp_path / "story.txt"
    source.write_text(
        "# Chapter One\n\nAlice entered the room.\n\nShe closed the door.\n",
        encoding="utf-8",
    )
    run_dir = tmp_path / "state"
    initialized = _json_run(
        "init",
        source,
        "--source-lang",
        "en",
        "--run-dir",
        run_dir,
    )
    assert initialized["ok"] is True
    return run_dir, _json_run("next", "--run-dir", run_dir)


def test_interactive_translation_round_trip(tmp_path: Path) -> None:
    run_dir, work = _start(tmp_path)
    assert work["kind"] == "body"
    response = {
        "kind": "body",
        "batch_id": work["batch_id"],
        "chapter": work["chapter"],
        "segment_indices": work["segment_indices"],
        "translations": [f"译文 {index}" for index in work["segment_indices"]],
        "terms": [{"source": "Alice", "target": "爱丽丝", "type": "人物"}],
    }
    response_path = tmp_path / "response.json"
    response_path.write_text(json.dumps(response, ensure_ascii=False), encoding="utf-8")
    applied = _json_run("apply", "--run-dir", run_dir, "--file", response_path)
    assert applied["status"]["segments_done"] == applied["status"]["segments_total"]
    assert applied["status"]["glossary"]["terms"] == 1

    title_work = _json_run("next", "--run-dir", run_dir)
    assert title_work["kind"] == "titles"
    title_response = {
        "kind": "titles",
        "batch_id": title_work["batch_id"],
        "sources": title_work["sources"],
        "translations": ["第一章" for _source in title_work["sources"]],
    }
    title_path = tmp_path / "titles.json"
    title_path.write_text(json.dumps(title_response, ensure_ascii=False), encoding="utf-8")
    _json_run("apply", "--run-dir", run_dir, "--file", title_path)
    assert _json_run("next", "--run-dir", run_dir)["kind"] == "complete"

    output = tmp_path / "story.zh.txt"
    result = _json_run(
        "assemble",
        "--run-dir",
        run_dir,
        "--format",
        "txt",
        "--out",
        output,
    )
    assert result["partial"] is False
    assert "译文" in output.read_text(encoding="utf-8")


def test_invalid_payload_does_not_advance_batch(tmp_path: Path) -> None:
    run_dir, work = _start(tmp_path)
    invalid = {
        "kind": "body",
        "batch_id": work["batch_id"],
        "chapter": work["chapter"],
        "segment_indices": work["segment_indices"],
        "translations": ["too few"],
        "terms": [{"source": "Alice", "target": ""}],
    }
    response_path = tmp_path / "invalid.json"
    response_path.write_text(json.dumps(invalid), encoding="utf-8")
    failed = _run(
        "apply",
        "--run-dir",
        run_dir,
        "--file",
        response_path,
        check=False,
    )
    assert failed.returncode == 2
    assert _json_run("next", "--run-dir", run_dir)["batch_id"] == work["batch_id"]
