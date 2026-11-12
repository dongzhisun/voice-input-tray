"""Black-box clauses 2 / 11: model download, SHA-256, corrupt-model refusal."""
import hashlib
import json

import pytest

from voicetray import manifest as m
from conftest import MODEL_DIR


class FakeResponse:
    def __init__(self, data: bytes):
        self._data = data
        self._pos = 0
        self.headers = {"Content-Length": str(len(data))}

    def read(self, n=-1):
        if n is None or n < 0:
            n = len(self._data) - self._pos
        out = self._data[self._pos:self._pos + n]
        self._pos += len(out)
        return out

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def test_sha256_file(tmp_path):
    p = tmp_path / "x.bin"
    p.write_bytes(b"abc")
    assert m.sha256_file(p) == hashlib.sha256(b"abc").hexdigest()


def test_real_bundled_model_verifies():
    """Independent evidence: the local weights match the pinned digests."""
    assert MODEL_DIR.is_dir(), f"missing model dir: {MODEL_DIR}"
    status = m.verify_dir(MODEL_DIR)
    assert set(status) == set(m.MANIFEST)
    assert all(s.valid for s in status.values()), status
    assert m.is_ready(MODEL_DIR)

    onnx = (MODEL_DIR / "model.int8.onnx").stat().st_size
    assert onnx == m.MANIFEST["model.int8.onnx"]["size"] == 239233841
    assert (MODEL_DIR / "LICENSE").read_text(encoding="utf-8").strip().startswith(
        "Ref to https://github.com/modelscope/FunASR")


def test_corrupt_file_is_detected(tmp_path):
    (tmp_path / "tokens.txt").write_bytes(b"truncated")
    (tmp_path / "model.int8.onnx").write_bytes(b"\x00" * 32)
    status = m.verify_dir(tmp_path)
    assert not status["tokens.txt"].valid
    assert not status["model.int8.onnx"].valid
    assert not status["LICENSE"].present
    assert not m.is_ready(tmp_path)


def test_required_paths_point_into_model_dir():
    model, tokens = m.required_paths(MODEL_DIR)
    assert model.endswith("model.int8.onnx")
    assert tokens.endswith("tokens.txt")


def test_ensure_model_writes_verified_files_and_manifest(tmp_path, monkeypatch):
    payload = b"sensevoice-weights-payload"
    spec = {
        "a.bin": {
            "size": len(payload),
            "sha256": hashlib.sha256(payload).hexdigest(),
            "url": "https://example.invalid/a.bin",
        },
    }
    monkeypatch.setattr(m, "MANIFEST", spec)
    monkeypatch.setattr(m.urllib.request, "urlopen",
                        lambda req, timeout=None: FakeResponse(payload))

    m.ensure_model(tmp_path)
    assert (tmp_path / "a.bin").read_bytes() == payload
    local = json.loads((tmp_path / m.MANIFEST_FILENAME).read_text("utf-8"))
    assert local["model_id"] == m.MODEL_ID
    assert local["weights_license"].startswith("See bundled LICENSE")
    assert "Apache-2.0" in local["software_license"]


def test_ensure_model_rejects_hash_mismatch_and_leaves_no_file(tmp_path, monkeypatch):
    spec = {
        "a.bin": {"size": 5, "sha256": "0" * 64,
                  "url": "https://example.invalid/a.bin"},
    }
    monkeypatch.setattr(m, "MANIFEST", spec)
    monkeypatch.setattr(m.urllib.request, "urlopen",
                        lambda req, timeout=None: FakeResponse(b"bad!!"))
    with pytest.raises(m.ModelError, match="SHA-256 mismatch"):
        m.ensure_model(tmp_path)
    assert not (tmp_path / "a.bin").exists()
    assert not (tmp_path / "a.bin.part").exists()


def test_ensure_model_reports_network_failure(tmp_path, monkeypatch):
    spec = {"a.bin": {"size": 3, "sha256": "0" * 64,
                      "url": "https://example.invalid/a.bin"}}
    monkeypatch.setattr(m, "MANIFEST", spec)

    def boom(req, timeout=None):
        raise m.urllib.error.URLError("offline")

    monkeypatch.setattr(m.urllib.request, "urlopen", boom)
    with pytest.raises(m.ModelError, match="download failed"):
        m.ensure_model(tmp_path)
    assert not (tmp_path / "a.bin.part").exists()
