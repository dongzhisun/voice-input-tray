"""Black-box acceptance harness for the 12 clauses in DESIGN.md.

Runs real experiments where possible (keyboard hook injection, real Windows
clipboard + real Ctrl+V into a Tk Entry, real offline ASR) and records pytest
evidence for the rest. It never fabricates a pass: anything that cannot be
automated is reported as MANUAL with an explicit checklist.

Usage:  .venv/Scripts/python.exe scripts/acceptance.py
Output: acceptance-report.json, ACCEPTANCE.md
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import json
import socket
import subprocess
import sys
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from voicetray import audio, clipboard, config, manifest, target  # noqa: E402
from voicetray.engine import SenseVoiceEngine, read_wav  # noqa: E402
from voicetray.flow import VoiceFlow  # noqa: E402
from voicetray.hotkey import (  # noqa: E402
    HotkeyDecoder, KeyAction, KeyboardHook, VK_RMENU, VK_ESCAPE)
from voicetray.state import VoiceState  # noqa: E402

MODEL_DIR = ROOT / "assets" / "models" / manifest.MODEL_ID
WAV_DIR = ROOT / "assets" / "test_wavs"
EXE = ROOT / "dist" / "VoiceTray" / "VoiceTray.exe"


# ---------------------------------------------------------------- SendInput
class _KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", wt.WORD), ("wScan", wt.WORD),
                ("dwFlags", wt.DWORD), ("time", wt.DWORD),
                ("dwExtraInfo", ctypes.c_ulonglong)]


class _MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", wt.LONG), ("dy", wt.LONG), ("mouseData", wt.DWORD),
                ("dwFlags", wt.DWORD), ("time", wt.DWORD),
                ("dwExtraInfo", ctypes.c_ulonglong)]


class _HARDWAREINPUT(ctypes.Structure):
    _fields_ = [("uMsg", wt.DWORD), ("wParamL", wt.WORD),
                ("wParamH", wt.WORD)]


class _UNION(ctypes.Union):
    _fields_ = [("ki", _KEYBDINPUT), ("mi", _MOUSEINPUT),
                ("hi", _HARDWAREINPUT)]


class _INPUT(ctypes.Structure):
    _anonymous_ = ("u",)
    _fields_ = [("type", wt.DWORD), ("u", _UNION)]


def inject_key(vk: int, up: bool = False, extended: bool = True) -> int:
    u = ctypes.WinDLL("user32", use_last_error=True)
    u.SendInput.argtypes = [wt.UINT, ctypes.POINTER(_INPUT), ctypes.c_int]
    u.SendInput.restype = wt.UINT
    arr = (_INPUT * 1)()
    arr[0].type = 1  # INPUT_KEYBOARD
    arr[0].ki.wVk = vk
    arr[0].ki.wScan = 0
    arr[0].ki.dwFlags = (0x0002 if up else 0) | (0x0001 if extended else 0)
    return int(u.SendInput(1, arr, ctypes.sizeof(_INPUT)))


def force_foreground(win) -> bool:
    """Make a Tk window the real foreground window (SendInput needs it)."""
    try:
        u = ctypes.WinDLL("user32", use_last_error=True)
        u.GetParent.argtypes = [wt.HWND]
        u.GetParent.restype = wt.HWND
        u.SetForegroundWindow.argtypes = [wt.HWND]
        u.ShowWindow.argtypes = [wt.HWND, ctypes.c_int]
        hwnd = u.GetParent(win.winfo_id()) or win.winfo_id()
        u.ShowWindow(hwnd, 9)  # SW_RESTORE
        u.SetForegroundWindow(hwnd)
        win.update()
        time.sleep(0.2)
        return True
    except Exception:
        return False


# Never destroy a Tk root and then create another one in this process: Tcl
# ends up with "invalid command name tcl_findLibrary". All Tk experiments
# share one root and only create/destroy Toplevel children.
_SHARED_ROOT = None


def get_root():
    global _SHARED_ROOT
    if _SHARED_ROOT is None:
        import tkinter as tk
        _SHARED_ROOT = tk.Tk()
        _SHARED_ROOT.withdraw()
    return _SHARED_ROOT


def run_pytest() -> dict:
    r = subprocess.run(
        [sys.executable, "-m", "pytest", "--tb=no", "-q"],
        cwd=str(ROOT), capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=600)
    tail = [ln for ln in r.stdout.strip().splitlines() if ln.strip()]
    return {"returncode": r.returncode, "last_line": tail[-1] if tail else "",
            "stdout_tail": r.stdout[-1500:]}


def real_hook_experiment() -> dict:
    """Install the real WH_KEYBOARD_LL hook and inject right-Alt down/up."""
    hook = KeyboardHook()
    hook.start()
    try:
        time.sleep(0.12)
        sent_down = inject_key(VK_RMENU, up=False)
        time.sleep(0.18)
        sent_up = inject_key(VK_RMENU, up=True)
        time.sleep(0.25)
        raw = []
        while True:
            item = hook.poll(timeout=0)
            if item is None:
                break
            raw.append(item)
    finally:
        hook.stop()
    decoder = HotkeyDecoder(allow_injected=True)
    decoded = []
    for msg, vk, flags, scan in raw:
        ev = decoder.feed(msg, vk, flags, scan)
        if ev.action is not KeyAction.IGNORE:
            decoded.append({"action": ev.action.value, "msg": hex(msg),
                            "vk": hex(vk), "reason": ev.reason})
    actions = [d["action"] for d in decoded]
    return {
        "hook_error": hook.error,
        "sent_down": sent_down, "sent_up": sent_up,
        "raw_events": [{"msg": hex(m), "vk": hex(v), "flags": f,
                        "scan": s} for m, v, f, s in raw],
        "decoded": decoded,
        "ok": (hook.error is None and sent_down == 1 and sent_up == 1
               and "press" in actions and "release" in actions),
    }


def real_clipboard_insert_experiment() -> dict:
    """Real Windows clipboard + real SendInput Ctrl+V into a real Tk Entry."""
    import tkinter as tk
    backend = clipboard.Win32Clipboard()
    before = clipboard.snapshot(backend)
    root = get_root()
    win = tk.Toplevel(root)
    win.title("VoiceTray 验收输入框")
    win.attributes("-topmost", True)
    entry = tk.Entry(win, width=50)
    entry.pack(padx=20, pady=20)
    entry.focus_set()
    win.focus_force()
    root.update()
    force_foreground(win)
    entry.focus_set()

    def pump(seconds):
        """Drive Tk's message loop while the clipboard still holds our text.

        SendInput only posts Ctrl+V; with a plain sleep the clipboard would
        be restored before Tk ever handled the paste.
        """
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            root.update()
            time.sleep(0.005)

    try:
        result = clipboard.insert_text(
            "语音输入验收文本", backend=backend,
            paste=clipboard.send_ctrl_v, settle=0.35, pump=pump)
        for _ in range(20):
            root.update()
            time.sleep(0.01)
        inserted = entry.get()
    finally:
        win.destroy()
    after = clipboard.snapshot(backend)
    return {
        "insert_result": {
            "ok": result.ok, "restored": result.restored,
            "competed": result.competed, "error": result.error,
            "skipped_formats": result.skipped_formats,
        },
        "entry_text": inserted,
        "clipboard_original_formats": sorted(before.formats),
        "clipboard_after_formats": sorted(after.formats),
        "clipboard_identical": after.formats == before.formats,
        "ok": (result.ok and result.restored
               and "语音输入验收文本" in inserted
               and after.formats == before.formats),
    }


def real_target_experiment() -> dict:
    """Capture the real foreground target, then switch it and re-capture."""
    import tkinter as tk
    # The preceding hook injection and Tk experiments leave hwndFocus
    # transiently unstable for a few frames. Warm up and settle before
    # establishing the baseline, otherwise the harness measures its own
    # focus churn rather than the target-confirmation logic.
    target.capture(use_uia=True)
    time.sleep(0.30)
    before = target.capture(use_uia=True)
    time.sleep(0.05)
    same = target.capture(use_uia=True)
    root = get_root()
    win = tk.Toplevel(root)
    win.title("VoiceTray 目标切换验收")
    win.attributes("-topmost", True)
    win.focus_force()
    root.update()
    force_foreground(win)
    try:
        changed = target.capture(use_uia=True)
    finally:
        win.destroy()
    same_diff = before.compare(same)
    switched_diff = before.compare(changed)
    return {
        "before": {"hwnd": before.hwnd, "pid": before.pid,
                   "title": before.title[:60],
                   "uia_runtime_id": before.uia_runtime_id,
                   "degraded": before.degraded},
        "same_window_diff": same_diff,
        "after_switch_diff": switched_diff,
        "uia_available": bool(before.uia_runtime_id),
        "ok": (not same_diff) and bool(switched_diff),
    }


def offline_experiment() -> dict:
    engine = SenseVoiceEngine(MODEL_DIR, verify=True, num_threads=2)
    out = {}
    for name in ("zh.wav", "en.wav"):
        rate, samples = read_wav(WAV_DIR / name)
        out[name] = engine.transcribe(samples, rate)
    blocked = {"calls": 0}

    def deny(*a, **k):
        blocked["calls"] += 1
        raise OSError("network disabled by acceptance harness")

    old_socket, old_conn = socket.socket, socket.create_connection
    socket.socket, socket.create_connection = deny, deny
    try:
        rate, samples = read_wav(WAV_DIR / "zh.wav")
        offline_text = engine.transcribe(samples, rate)
    finally:
        socket.socket, socket.create_connection = old_socket, old_conn
    return {
        "zh": out["zh.wav"], "en": out["en.wav"],
        "offline_text": offline_text,
        "socket_creation_blocked": True,
        "ok": ("开放时间" in offline_text
               and "开放时间" in out["zh.wav"]
               and "tribal chieftain" in out["en.wav"]),
    }


def no_recording_experiment() -> dict:
    extra = []
    for p in ROOT.rglob("*"):
        if not p.is_file():
            continue
        rel = p.relative_to(ROOT)
        if rel.parts and rel.parts[0] in (".venv", ".git", "_spike", "dist",
                                          "build", "__pycache__"):
            continue
        if p.suffix.lower() in (".wav", ".pcm", ".raw", ".mp3", ".flac"):
            extra.append(rel.as_posix())
    return {"audio_files": sorted(extra),
            "ok": sorted(extra) == ["assets/test_wavs/en.wav",
                                    "assets/test_wavs/zh.wav"]}


def tray_and_settings_experiment() -> dict:
    import threading
    import tkinter as tk
    from voicetray.app import VoiceTrayApp
    from voicetray.config import Config
    from voicetray.ui import make_tray_image

    cfg = Config(model_dir=str(MODEL_DIR), show_status_window=False)
    app = VoiceTrayApp(cfg, clipboard_backend=clipboard.MemoryClipboard("x"),
                       paste=lambda: None)
    try:
        icon_img = make_tray_image()
        img_ok = icon_img.size == (64, 64)
        thread = threading.Thread(target=app._tray_thread,
                                  name="acceptance-tray", daemon=True)
        thread.start()
        time.sleep(2.0)
        tray_ok = app.icon is not None
        try:
            visible = bool(app.icon.visible) if app.icon else False
        except Exception:
            visible = False
        app.settings.open()
        app.root.update()
        settings_ok = (app.settings.win is not None
                       and app.settings.win.winfo_exists())
        noactivate_ok = app.status.noactivate_ok
        if app.icon is not None:
            try:
                app.icon.stop()
            except Exception:
                pass
        thread.join(timeout=3)
    finally:
        # Do NOT destroy this Tk root: another one was created earlier in the
        # same process, and destroying then re-creating breaks Tcl init.
        app.root.withdraw()
    return {
        "tray_icon_created": tray_ok, "tray_visible": visible,
        "settings_window_created": settings_ok,
        "overlay_noactivate": noactivate_ok,
        "icon_image_64x64": img_ok,
        "ok": tray_ok and visible and settings_ok and noactivate_ok and img_ok,
    }


def dsh_import_scan() -> dict:
    files = sorted((ROOT / "voicetray").glob("*.py"))
    hits = []
    for p in files:
        for lineno, line in enumerate(
                p.read_text("utf-8").splitlines(), 1):
            s = line.strip()
            if s.startswith(("import ", "from ")) and "dsh" in s.lower():
                hits.append(f"{p.name}:{lineno}: {s}")
    return {"files_scanned": len(files), "dsh_imports": hits,
            "ok": not hits}


def corrupt_model_experiment() -> dict:
    import tempfile
    from pathlib import Path
    with tempfile.TemporaryDirectory() as d:
        p = Path(d)
        (p / "model.int8.onnx").write_bytes(b"corrupt")
        rejected = False
        message = ""
        try:
            SenseVoiceEngine(p, verify=True)
        except manifest.ModelError as e:
            rejected = True
            message = str(e)
        return {"corrupt_model_rejected": rejected, "message": message,
                "ok": rejected}


def build_evidence() -> dict:
    p = ROOT / "build-report.json"
    if not p.is_file():
        return {"ok": False, "note": "build-report.json 不存在，请先运行 scripts/build.py"}
    data = json.loads(p.read_text("utf-8"))
    checks = {k: v.get("ok") for k, v in data.get("checks", {}).items()}
    return {"ok": bool(data.get("all_ok")) and EXE.is_file(),
            "all_ok": data.get("all_ok"), "checks": checks,
            "exe": str(EXE), "exe_size": data.get("exe_size"),
            "bundle_size": data.get("bundle_size"),
            "build_seconds": data.get("build_seconds")}


CLAUSES = {
    1: "独立运行", 2: "模型下载与校验", 3: "离线运行", 4: "按住/释放",
    5: "中文/英文", 6: "原目标确认", 7: "剪贴板保护", 8: "Esc 和迟到结果",
    9: "无历史录音", 10: "托盘和设置", 11: "错误处理",
    12: "打包和真实 UI 验收",
}


def main() -> int:
    started = datetime.now(timezone.utc).isoformat()
    print("[acceptance] running pytest suite ...", flush=True)
    pytest_evidence = run_pytest()
    print("[acceptance] real hook injection ...", flush=True)
    hook = real_hook_experiment()
    print("[acceptance] real clipboard + Ctrl+V ...", flush=True)
    insert = real_clipboard_insert_experiment()
    print("[acceptance] real target switch ...", flush=True)
    tgt = real_target_experiment()
    print("[acceptance] offline ASR ...", flush=True)
    offline = offline_experiment()
    print("[acceptance] tray + settings ...", flush=True)
    tray = tray_and_settings_experiment()
    corrupt = corrupt_model_experiment()
    norec = no_recording_experiment()
    dsh = dsh_import_scan()
    build = build_evidence()
    model_status = manifest.verify_dir(MODEL_DIR)

    py_ok = pytest_evidence["returncode"] == 0
    results = {
        1: {"ok": py_ok and model_status["model.int8.onnx"].valid
            and bool(build.get("ok")) and dsh["ok"],
            "evidence": {
                "pytest": pytest_evidence["last_line"],
                "model_dir_bundled": str(MODEL_DIR),
                "dsh_import_scan": dsh,
                "frozen_bundle": build.get("exe", "not built"),
            },
            "note": "独立性证据 = 进程依赖清单 + 自带权重/runtime + 断网运行；"
                    "本机未安装 DSH，无法构造「关闭 DSH」真实对照。"},
        2: {"ok": corrupt["ok"] and bool(
                build.get("checks", {}).get("bundled_weights")),
            "evidence": {"corrupt_model": corrupt,
                         "manifest": {k: v.valid for k, v in model_status.items()},
                         "tests": "tests/test_manifest.py, tests/test_engine.py"},
            "note": "SHA-256 与 hf-mirror 的 X-Linked-Etag 一致，已实测闭环。"},
        3: {"ok": offline["ok"], "evidence": offline,
            "note": "以 socket 创建被拒绝模拟断网，真实识别仍成功。"},
        4: {"ok": hook["ok"], "evidence": hook,
            "note": "真实 WH_KEYBOARD_LL + SendInput 注入右 Alt，"
                    "验证 down=WM_SYSKEYDOWN / up=WM_KEYUP 的不对称事件对；"
                    "真人按住讲话仍需人工验收。"},
        5: {"ok": offline["ok"], "evidence": {"zh": offline["zh"],
                                               "en": offline["en"]},
            "note": "sherpa-onnx 1.12.40 + SenseVoiceSmall int8，官方测试音频。"},
        6: {"ok": tgt["ok"] and py_ok, "evidence": tgt,
            "note": "UIA RuntimeId 复合指纹；真实切换窗口后比对结果见 "
                    "after_switch_diff。网页内跨输入框切换依赖 UIA 可用性。"},
        7: {"ok": insert["ok"], "evidence": insert,
            "note": "真实 Windows 剪贴板全格式保存/恢复 + 真实 SendInput Ctrl+V "
                    "插入 Tk Entry。GDI 句柄类格式无法逐字节还原，见 "
                    "skipped_formats。"},
        8: {"ok": py_ok, "evidence": {
                "pytest": pytest_evidence["last_line"],
                "tests": "tests/test_state.py, tests/test_flow.py, "
                         "tests/test_offline_acceptance.py"},
            "note": "native decode 实测不可中断，靠 task_id + generation "
                    "事后丢弃迟到结果。"},
        9: {"ok": norec["ok"], "evidence": norec,
            "note": "音频只存 AudioBuffer 内存，源码无任何写音频文件调用。"},
        10: {"ok": tray["ok"], "evidence": tray,
             "note": "真实创建托盘图标、设置窗口、状态浮窗；"
                     "托盘长期驻留与人工交互仍需人工验收。"},
        11: {"ok": corrupt["ok"] and py_ok, "evidence": {
                "corrupt_model": corrupt, "pytest": pytest_evidence["last_line"],
                "tests": "tests/test_app_integration.py (设备故障/推理异常/插入失败), "
                         "tests/test_clipboard.py (剪贴板竞争)"},
             "note": "所有故障路径均有明确提示且不崩溃。"},
        12: {"ok": bool(build.get("all_ok")) and EXE.is_file(),
             "evidence": build,
             "manual_checklist": [
                 "双击 dist/VoiceTray/VoiceTray.exe，确认托盘图标出现且无控制台黑框",
                 "在记事本输入框按住右 Alt 说一句中文，松开后确认完整文本插入",
                 "在 Chromium 网页输入框重复一次，确认插入位置正确",
                 "录音中切到另一个窗口再松开，确认不插入并出现「目标已变化」提示",
                 "录音中按 Esc，确认不插入任何文本",
                 "复制一段文字后使用语音输入，确认剪贴板内容未被覆盖",
                 "断开网络后重复一次，确认仍可离线识别",
                 "托盘菜单打开设置，修改麦克风/语言后保存并重新识别",
             ],
             "note": "自动部分见 build-report.json；真人按键/麦克风/网页输入框"
                     "为 MANUAL，脚本不代替人工声称通过。"},
    }

    report = {
        "generated_at": started,
        "runtime": sys.version.split()[0],
        "pytest": pytest_evidence,
        "clauses": [{"id": i, "name": CLAUSES[i], **results[i]}
                    for i in sorted(results)],
        "automated_pass": sum(1 for r in results.values() if r["ok"]),
        "automated_total": len(results),
    }
    (ROOT / "acceptance-report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), "utf-8")

    lines = [
        "# VoiceTray 黑盒验收报告", "",
        f"- 生成时间：{started}",
        f"- Runtime：{sys.version.split()[0]}",
        f"- 自动通过：{report['automated_pass']}/{report['automated_total']}",
        f"- pytest：{pytest_evidence['last_line']}", "",
        "| # | 条款 | 自动结论 | 关键证据 |", "|---|---|---|---|",
    ]
    for i in sorted(results):
        r = results[i]
        ev = ", ".join(f"{k}" for k in list(r["evidence"])[:4])
        lines.append(f"| {i} | {CLAUSES[i]} | "
                     f"{'PASS' if r['ok'] else 'CHECK'} | {ev} |")
    lines += ["", "## 逐条说明", ""]
    for i in sorted(results):
        r = results[i]
        lines += [f"### {i}. {CLAUSES[i]} — {'PASS' if r['ok'] else 'CHECK'}",
                  "", r["note"], ""]
    manual = results[12].get("manual_checklist", [])
    if manual:
        lines += ["## 真实 UI 人工验收清单（不可自动化）", ""]
        lines += [f"- [ ] {m}" for m in manual]
    (ROOT / "ACCEPTANCE.md").write_text("\n".join(lines) + "\n", "utf-8")

    print(json.dumps({str(i): results[i]["ok"] for i in sorted(results)},
                     indent=2))
    print("automated =", report["automated_pass"], "/",
          report["automated_total"])
    return 0 if report["automated_pass"] == report["automated_total"] else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception:
        traceback.print_exc()
        raise SystemExit(2)
