"""Build VoiceTray (PyInstaller onedir) and verify the artifact.

Usage:  .venv/Scripts/python.exe scripts/build.py
Output: dist/VoiceTray/VoiceTray.exe + build-report.json
"""
from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SPEC = ROOT / "VoiceTray.spec"
DIST = ROOT / "dist" / "VoiceTray"
EXE = DIST / "VoiceTray.exe"


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def run_exe(*args, timeout=300):
    return subprocess.run([str(EXE), *args], cwd=str(DIST),
                          capture_output=True, text=True, timeout=timeout,
                          encoding="utf-8", errors="replace")


def rotate(path: Path):
    """Move an old output/work dir aside instead of deleting it.

    PyInstaller's COLLECT step removes the whole previous dist tree; on this
    host that bulk delete is blocked by the workspace safety guard. A rename
    gives PyInstaller a clean target with no deletion at all.
    """
    if not path.exists():
        return None
    dst = path.parent / f"{path.name}_prev_{int(time.time())}"
    path.rename(dst)
    return str(dst)


def main() -> int:
    report: dict = {"built_at": datetime.now(timezone.utc).isoformat(),
                    "spec": str(SPEC), "checks": {}}
    (ROOT / "dist").mkdir(exist_ok=True)
    (ROOT / "build").mkdir(exist_ok=True)
    report["rotated"] = {
        "dist": rotate(DIST),
        "work": rotate(ROOT / "build" / "VoiceTray"),
    }
    t0 = time.time()
    print("[build] running PyInstaller ...", flush=True)
    r = subprocess.run(
        [sys.executable, "-m", "PyInstaller", "--noconfirm",
         str(SPEC), "--distpath", str(ROOT / "dist"),
         "--workpath", str(ROOT / "build")],
        cwd=str(ROOT), capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=1200)
    report["build_seconds"] = round(time.time() - t0, 1)
    if r.returncode != 0:
        print(r.stdout[-6000:])
        print(r.stderr[-6000:])
        report["checks"]["pyinstaller"] = {"ok": False,
                                           "returncode": r.returncode}
        (ROOT / "build-report.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2), "utf-8")
        return 1
    report["checks"]["pyinstaller"] = {"ok": True}
    print(f"[build] done in {report['build_seconds']}s", flush=True)

    report["exe"] = str(EXE)
    report["exe_size"] = EXE.stat().st_size if EXE.is_file() else 0
    report["bundle_size"] = sum(
        p.stat().st_size for p in DIST.rglob("*") if p.is_file()) \
        if DIST.is_dir() else 0
    report["checks"]["exe_exists"] = {"ok": EXE.is_file()}

    # PyInstaller 6 onedir puts datas under _internal/; locate rather than
    # hard-code so a different PyInstaller layout does not break the check.
    onnx_files = list(DIST.rglob("model.int8.onnx")) if DIST.is_dir() else []
    model_dir = onnx_files[0].parent if onnx_files else DIST / "assets" / "models" / "sensevoice-int8-2024-07-17"
    report["bundled_model_dir"] = str(model_dir)
    expected = {
        "model.int8.onnx": "c71f0ce00bec95b07744e116345e33d8cbbe08cef896382cf907bf4b51a2cd51",
        "tokens.txt": "f449eb28dc567533d7fa59be34e2abca8784f771850c78a47fb731a31429a1dc",
        "LICENSE": "221c6df10b0931a5629adad671ea48fb7747e034c414b6d2bfa275bc3dd4ea17",
    }
    bundles = {}
    for name, digest in expected.items():
        p = model_dir / name
        bundles[name] = {"present": p.is_file(),
                         "sha256_ok": p.is_file() and sha256(p) == digest}
    report["checks"]["bundled_weights"] = {
        "ok": all(v["present"] and v["sha256_ok"] for v in bundles.values()),
        "files": bundles,
    }

    if EXE.is_file():
        for name, args in [
            ("version", ["--version"]),
            ("check_model", ["check-model"]),
            ("selftest_full", ["selftest", "--full"]),
        ]:
            try:
                p = run_exe(*args)
                report["checks"][name] = {
                    "ok": p.returncode == 0,
                    "returncode": p.returncode,
                    "stdout_head": p.stdout[:600],
                    "stderr_head": p.stderr[:600],
                }
                if name in ("check_model", "selftest_full") and p.returncode == 0:
                    try:
                        data = json.loads(p.stdout)
                        report["checks"][name]["data"] = data
                    except ValueError:
                        pass
            except Exception as e:
                report["checks"][name] = {"ok": False, "error": repr(e)}
    else:
        for name in ("version", "check_model", "selftest_full"):
            report["checks"][name] = {"ok": False, "error": "exe missing"}

    report["all_ok"] = all(v.get("ok") for v in report["checks"].values())
    (ROOT / "build-report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), "utf-8")
    print(json.dumps({k: v.get("ok") for k, v in report["checks"].items()},
                     indent=2))
    print("ALL_OK =", report["all_ok"])
    return 0 if report["all_ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
