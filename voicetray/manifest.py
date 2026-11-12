"""Model manifest: download + SHA-256 verification, offline by design.

Weights come from the official sherpa-onnx SenseVoice conversion repo via
hf-mirror (GitHub release downloads are unreachable on this host). The
mirror's `X-Linked-Etag` header is the LFS SHA-256, so the local digest is
verifiable against an independent value, not just a self-generated one.
"""
from __future__ import annotations

import hashlib
import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass, asdict
from pathlib import Path

MODEL_ID = "sensevoice-int8-2024-07-17"
REPO = "csukuangfj/sherpa-onnx-sense-voice-zh-en-ja-ko-yue-2024-07-17"
BASE = f"https://hf-mirror.com/{REPO}/resolve/main"

MANIFEST: dict[str, dict] = {
    "model.int8.onnx": {
        "size": 239233841,
        "sha256": "c71f0ce00bec95b07744e116345e33d8cbbe08cef896382cf907bf4b51a2cd51",
        "url": f"{BASE}/model.int8.onnx",
    },
    "tokens.txt": {
        "size": 315894,
        "sha256": "f449eb28dc567533d7fa59be34e2abca8784f771850c78a47fb731a31429a1dc",
        "url": f"{BASE}/tokens.txt",
    },
    "LICENSE": {
        "size": 71,
        "sha256": "221c6df10b0931a5629adad671ea48fb7747e034c414b6d2bfa275bc3dd4ea17",
        "url": f"{BASE}/LICENSE",
    },
}
MANIFEST_FILENAME = "manifest.json"


class ModelError(RuntimeError):
    pass


@dataclass
class FileStatus:
    name: str
    present: bool
    size_ok: bool
    hash_ok: bool

    @property
    def valid(self) -> bool:
        return self.present and self.size_ok and self.hash_ok


def sha256_file(path: Path, chunk: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            b = f.read(chunk)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def verify_dir(model_dir: Path) -> dict[str, FileStatus]:
    out: dict[str, FileStatus] = {}
    for name, spec in MANIFEST.items():
        p = model_dir / name
        if not p.is_file():
            out[name] = FileStatus(name, False, False, False)
            continue
        size = p.stat().st_size
        size_ok = size == spec["size"]
        digest = sha256_file(p) if size_ok else ""
        out[name] = FileStatus(name, True, size_ok, digest == spec["sha256"])
    return out


def is_ready(model_dir: Path) -> bool:
    return all(s.valid for s in verify_dir(model_dir).values())


def ensure_model(model_dir: Path, progress=None, timeout: int = 120) -> None:
    """Download missing/corrupt files, verify SHA-256, write local manifest.
    Raises ModelError on any hash mismatch or network failure."""
    model_dir.mkdir(parents=True, exist_ok=True)
    for name, spec in MANIFEST.items():
        p = model_dir / name
        if p.is_file() and p.stat().st_size == spec["size"] and \
                sha256_file(p) == spec["sha256"]:
            continue
        tmp = model_dir / (name + ".part")
        try:
            req = urllib.request.Request(spec["url"],
                                         headers={"User-Agent": "VoiceTray/0.1"})
            with urllib.request.urlopen(req, timeout=timeout) as r, \
                    open(tmp, "wb") as f:
                total, done = int(r.headers.get("Content-Length", 0)), 0
                while True:
                    buf = r.read(1 << 20)
                    if not buf:
                        break
                    f.write(buf)
                    done += len(buf)
                    if progress:
                        progress(name, done, total)
            os.replace(tmp, p)
        except (urllib.error.URLError, OSError) as e:
            tmp.unlink(missing_ok=True)
            raise ModelError(f"download failed for {name}: {e}") from e
        if p.stat().st_size != spec["size"] or sha256_file(p) != spec["sha256"]:
            p.unlink(missing_ok=True)
            raise ModelError(f"SHA-256 mismatch for {name}; download rejected")
    local = {
        "model_id": MODEL_ID,
        "software_license": "Apache-2.0 (sherpa-onnx)",
        "weights_license": "See bundled LICENSE (FunASR); NOT Apache-2.0",
        "source": BASE,
        "files": MANIFEST,
    }
    (model_dir / MANIFEST_FILENAME).write_text(
        json.dumps(local, indent=2), encoding="utf-8")


def required_paths(model_dir: Path) -> tuple[str, str]:
    return str(model_dir / "model.int8.onnx"), str(model_dir / "tokens.txt")
