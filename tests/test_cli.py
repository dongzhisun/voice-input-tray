"""Console entry point and version behaviour (Task 1 acceptance)."""
import json
import subprocess
import sys

import pytest

from voicetray import __version__, cli
from conftest import ROOT, MODEL_DIR


def run_module(*args):
    return subprocess.run([sys.executable, "-m", "voicetray", *args],
                          cwd=str(ROOT), capture_output=True, text=True,
                          timeout=120)


def test_version_flag_prints_version_and_exits_zero():
    r = run_module("--version")
    assert r.returncode == 0, r.stderr
    assert f"VoiceTray {__version__}" in r.stdout


def test_check_model_subcommand_reports_ready():
    r = run_module("check-model", "--model-dir", str(MODEL_DIR))
    assert r.returncode == 0, r.stderr
    data = json.loads(r.stdout)
    assert data["ready"] is True
    assert all(f["valid"] for f in data["files"].values())
    assert "NOT Apache-2.0" in data["weights_license"]


def test_check_model_detects_corrupt_directory(tmp_path):
    r = run_module("check-model", "--model-dir", str(tmp_path))
    assert r.returncode == 1
    data = json.loads(r.stdout)
    assert data["ready"] is False


def test_selftest_subcommand_emits_valid_json():
    r = run_module("selftest")
    assert r.returncode == 0, r.stderr
    data = json.loads(r.stdout)
    assert data["version"] == __version__
    assert data["model_ready"] is True
    assert data["hook_ok"] is True
    assert data["clipboard_roundtrip"] is True
    assert data["uia_ok"] is True
    assert data["chosen_input_device"] == 16


def test_selftest_full_includes_real_recognition():
    r = run_module("selftest", "--full")
    assert r.returncode == 0, r.stderr
    data = json.loads(r.stdout)
    assert "开放时间" in data["recognition"]["zh.wav"]
    assert "tribal chieftain" in data["recognition"]["en.wav"]
    assert data["engine"]["streaming"] is False
    assert data["engine"]["cancel_api"] is False


def test_selftest_function_accepts_explicit_model_dir():
    report = cli.selftest(model_dir=MODEL_DIR)
    assert report["model_ready"] is True
    assert report["clipboard_roundtrip"] is True
    # Clipboard competition from other processes is reported, not hidden;
    # it must never be mistaken for a hard dependency/config failure.
    assert not any(("自检失败" in w) or ("依赖缺失" in w)
                   for w in report["warnings"])
