"""Console entry point: run / selftest / check-model.

`--version` and `selftest` are intentionally independent of Tk so the
packaged executable can be verified from a console without a UI session.
"""
from __future__ import annotations

import argparse
import json
import platform
import sys
from pathlib import Path
from typing import Optional

from . import __version__


def _force_utf8_stdio() -> None:
    """The frozen exe's stdout is a pipe using the Windows ANSI code page by
    default; Chinese JSON output then decodes as mojibake in harnesses that
    expect UTF-8. Force UTF-8 on both streams before argparse prints.
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, OSError, ValueError):
            pass


def _prepare_frozen() -> None:
    """comtypes generates UIA typelib wrappers at runtime; give it a writable
    `gen` path when the app is frozen (the bundle directory may be read-only).
    Pre-generated modules already in the bundle stay resolvable afterwards.
    """
    if not getattr(sys, "frozen", False):
        return
    try:
        import os
        import comtypes.gen
        target = (Path(os.environ.get("APPDATA") or ".")
                  / "VoiceTray" / "comtypes_gen")
        target.mkdir(parents=True, exist_ok=True)
        (target / "__init__.py").touch(exist_ok=True)
        comtypes.gen.__path__.insert(0, str(target))
    except Exception:
        pass


def _hide_console() -> None:
    """GUI run should not show a black console; CLI subcommands keep stdout."""
    if not getattr(sys, "frozen", False):
        return
    try:
        import os
        log_dir = Path(os.environ.get("APPDATA") or ".") / "VoiceTray"
        log_dir.mkdir(parents=True, exist_ok=True)
        sys.stdout = open(log_dir / "stdout.log", "a",
                          encoding="utf-8", buffering=1)
        sys.stderr = open(log_dir / "stderr.log", "a",
                          encoding="utf-8", buffering=1)
    except Exception:
        pass
    try:
        import ctypes
        ctypes.windll.kernel32.FreeConsole()
    except Exception:
        pass


def selftest(*, full: bool = False, model_dir=None) -> dict:
    from . import audio, clipboard, config, manifest, target
    from .hotkey import KeyboardHook

    cfg, config_warnings = config.Config.load()
    mdir = Path(model_dir) if model_dir else Path(cfg.model_dir)
    report: dict = {
        "version": __version__,
        "python": sys.version.split()[0],
        "platform": platform.platform(),
        "model_dir": str(mdir),
        "warnings": list(config_warnings),
    }

    try:
        status = manifest.verify_dir(mdir)
        report["model_files"] = {
            name: {"present": s.present, "size_ok": s.size_ok,
                   "hash_ok": s.hash_ok}
            for name, s in status.items()
        }
        report["model_ready"] = all(s.valid for s in status.values())
    except Exception as e:
        report["model_ready"] = False
        report["warnings"].append(f"模型校验异常：{e}")

    try:
        report["devices"] = audio.list_input_devices()
        report["chosen_input_device"] = audio.choose_device(cfg.device)
    except Exception as e:
        report["devices"] = []
        report["chosen_input_device"] = None
        report["warnings"].append(f"音频设备枚举失败：{e}")

    try:
        uia = target._get_uia()
        focus = target._uia_focus() if uia else {}
        report["uia_ok"] = bool(uia)
        report["uia_focus_runtime_id"] = focus.get("uia_runtime_id", "")
    except Exception as e:
        report["uia_ok"] = False
        report["warnings"].append(f"UIA 检查失败：{e}")

    hook = KeyboardHook()
    try:
        hook.start()
        report["hook_ok"] = hook.error is None
        if hook.error:
            report["warnings"].append(hook.error)
    except Exception as e:
        report["hook_ok"] = False
        report["warnings"].append(f"键盘钩子自检失败：{e}")
    finally:
        try:
            hook.stop()
        except Exception:
            pass

    try:
        backend = clipboard.Win32Clipboard()
        attempts = []
        for _ in range(3):
            before = clipboard.snapshot(backend)
            backend.set_text("__voicetray_selftest__")
            clipboard.restore(backend, before)
            after = clipboard.snapshot(backend)
            attempt = {
                "equal": after.formats == before.formats,
                "formats_before": sorted(before.formats),
                "formats_after": sorted(after.formats),
                "seq_before": before.seq, "seq_after": after.seq,
            }
            if not attempt["equal"]:
                attempt["diff"] = {
                    str(k): [len(before.formats.get(k, b"")),
                             len(after.formats.get(k, b""))]
                    for k in sorted(set(before.formats) | set(after.formats))
                    if before.formats.get(k) != after.formats.get(k)
                }
            attempts.append(attempt)
        report["clipboard_attempts"] = attempts
        report["clipboard_roundtrip"] = any(a["equal"] for a in attempts)
        report["clipboard_stable"] = all(a["equal"] for a in attempts)
        if not report["clipboard_stable"]:
            report["warnings"].append(
                "剪贴板往返出现不一致（通常为其他进程并发修改），详见 "
                "clipboard_attempts；insert_text 仍会恢复用户原始快照")
    except Exception as e:
        report["clipboard_roundtrip"] = False
        report["clipboard_stable"] = False
        report["warnings"].append(f"剪贴板自检失败：{e}")

    report["deps"] = {}
    for name in ("sherpa_onnx", "sounddevice", "numpy", "comtypes",
                 "PIL", "pystray", "win32clipboard"):
        try:
            mod = __import__(name)
            report["deps"][name] = getattr(mod, "__version__", "ok")
        except Exception as e:
            report["deps"][name] = f"MISSING: {type(e).__name__}"
            report["warnings"].append(f"依赖缺失：{name}")

    if full and report.get("model_ready"):
        from .engine import SenseVoiceEngine, read_wav
        from .config import app_dir
        wav_dir = app_dir() / "assets" / "test_wavs"
        try:
            eng = SenseVoiceEngine(mdir, verify=True,
                                   num_threads=cfg.num_threads,
                                   language=cfg.language)
            report["engine"] = eng.describe()
            report["recognition"] = {}
            for name in ("zh.wav", "en.wav"):
                p = wav_dir / name
                if p.is_file():
                    rate, samples = read_wav(p)
                    report["recognition"][name] = eng.transcribe(samples, rate)
        except Exception as e:
            report["warnings"].append(f"完整自检失败：{e}")

    return report


def check_model(model_dir=None) -> dict:
    from . import config, manifest
    cfg, _ = config.Config.load()
    mdir = Path(model_dir) if model_dir else Path(cfg.model_dir)
    status = manifest.verify_dir(mdir)
    return {
        "model_dir": str(mdir),
        "ready": all(s.valid for s in status.values()),
        "files": {name: {"present": s.present, "size_ok": s.size_ok,
                         "hash_ok": s.hash_ok, "valid": s.valid}
                  for name, s in status.items()},
        "expected": {name: {"size": spec["size"],
                            "sha256": spec["sha256"]}
                     for name, spec in manifest.MANIFEST.items()},
        "weights_license": "See bundled LICENSE (FunASR), NOT Apache-2.0",
    }


def main(argv=None) -> int:
    _force_utf8_stdio()
    parser = argparse.ArgumentParser(
        prog="voicetray", description="VoiceTray · 不依赖 DSH 的离线语音输入")
    parser.add_argument("--version", action="version",
                        version=f"VoiceTray {__version__}")
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("run", help="启动托盘应用（默认）")
    st = sub.add_parser("selftest", help="环境与依赖自检")
    st.add_argument("--full", action="store_true", help="包含真实模型推理")
    cm = sub.add_parser("check-model", help="校验模型文件与 SHA-256")
    cm.add_argument("--model-dir", default=None)
    args = parser.parse_args(argv)
    _prepare_frozen()

    command = args.command or "run"
    if command == "run":
        from . import config
        from .app import VoiceTrayApp
        _hide_console()
        cfg, warnings = config.Config.load()
        VoiceTrayApp(cfg, warnings).run()
        return 0
    if command == "selftest":
        report = selftest(full=args.full)
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0 if report.get("model_ready") else 1
    if command == "check-model":
        report = check_model(args.model_dir)
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0 if report["ready"] else 1
    parser.print_help()
    return 2
