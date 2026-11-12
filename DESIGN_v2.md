# 实现阶段设计修订（v2 增量，不覆盖 DESIGN.md）

**日期**：2026-11-12　**前提**：`DESIGN.md` 的 12 项黑盒验收条款与「方案可改、黑盒不变」的裁定继续有效。本文件只记录编码与打包过程中由实测暴露、必须修订的实现细节。

---

## 一、实现阶段新增的三处修订

| # | 问题（实测） | 原实现 | 修订后 | 影响 |
|---|---|---|---|---|
| R1 | Tkinter `after()` 从 ASR / 托盘 / 模型线程调用时抛 `RuntimeError: main thread is not in main loop`（集成测试实测） | 工作线程直接 `root.after(0, cb)` 回投主线程 | 新增线程安全投递队列 `VoiceTrayApp._calls`，`post(fn, *args)` 从任意线程入队；Tk `after` 泵每次 tick 调 `_process_posted()` 统一出队执行 | 解码结果、托盘菜单、模型加载回调不会再静默丢失；所有 `VoiceState` 变更仍严格单线程 |
| R2 | PyInstaller 6 onedir 把 `datas` 放进 `_internal/`；`app_dir()` 返回 exe 所在目录 → 随包权重找不到，`check-model` 返回 `ready:false` | frozen 时 `Path(sys.executable).parent` | frozen 时优先 `sys._MEIPASS` | exe 能自带 239MB 权重离线运行，不依赖开发目录 |
| R3 | 设置窗口保存会把当时的绝对 `model_dir` 写进 `%APPDATA%`；exe 移动或换版本后该路径失效 | 无容错 | `Config.load()` 检测到配置里的模型目录不存在时回退到随包目录并给出 warning | 免安装目录迁移后不致完全不可用 |
| R4 | 剪贴板 `restore()` 中任一格式 `SetClipboardData` 失败即整体抛异常，而 `EmptyClipboard()` 已先清空剪贴板 → **用户内容全丢**，直接违反黑盒条款 7 | 单事务式全量恢复，一失败全放弃 | 关键文本格式（`CF_UNICODETEXT`/`CF_TEXT`/`CF_OEMTEXT`/`CF_LOCALE`）优先、逐格式尽力恢复、`RestoreReport.failures` 如实上报；`InsertResult` 新增 `restore_failures` / `restore_failure_formats` | 即使 exotic 格式失败，用户文本也不丢；且不再把「已成功插入」误报成失败状态 |
| R5 | frozen exe 的 stdout 走 Windows ANSI(cp936)，中文 JSON 被按 UTF-8 解成乱码；`selftest --full` 的中文识别结果不可读 | 直接 `print(json.dumps(...))` | `main()` 首行 `_force_utf8_stdio()` 强制两个流为 UTF-8 | `--version` / `check-model` / `selftest --full` 输出机器可读且中文正确 |
| R6 | 同一进程内 `tk.Tk()` → `destroy()` → 再 `tk.Tk()` 抛 `invalid command name "tcl_findLibrary"` | 每个用例/实验各建根窗口 | pytest 改 module-scope 单根窗口 + autouse 状态重置；验收脚本全程单例根窗口、只用 `Toplevel` 做实验且不销毁根 | 集成测试与真实 UI 实验不再偶发崩溃 |
| R7 | 真实剪贴板被其他进程并发修改时，单次往返比对偶发 `False`，验收不可复现 | 单次布尔判定 | selftest 连做 3 次，输出 `clipboard_attempts` 明细与 `clipboard_stable`；竞争如实记录但不误判为硬失败 | 诊断信息充分，验收稳定 |
| R8 | PyInstaller COLLECT 会删除旧 dist 树（1387 个文件），触发本工作区批量删除安全拦截导致构建中断 | 让 PyInstaller 自己清理 | `build.py` 构建前把旧 `dist/VoiceTray` 与 `build/VoiceTray` 重命名为 `_prev_<ts>`，并去掉 `--clean` | 全链路不做批量删除，构建可通过 |
| R9 | Chromium 网页窗口 `hwndFocus == 顶层 hwnd`（无独立焦点控件），且焦点回落瞬间会在 0/有效值间跳变；把它当硬指纹字段会产生**假阴性**，把合法插入误判成「目标已变化」 | `focus_hwnd` 无条件参与 `fingerprint()` 与 `compare()` | `_effective_focus_hwnd()`：`focus_hwnd == hwnd` 或为 0 时归一化为 0（不可观测）；仅真正独立的子 HWND 参与比对 | 网页场景靠 `hwnd/pid/tid/title/class/uia_runtime_id` 判定，原生控件仍保留区分能力 |
| R10 | **`SendInput(Ctrl+V)` 只投递消息，目标应用异步读取剪贴板**；原 `insert_text` 在 paste 后 `time.sleep(0.06)` 就恢复剪贴板，而 GUI 主线程 sleep 期间根本不 pump 消息 → 目标读到的是已还原的原文，**插入实际无效**。实测：前台窗口、剪贴板还原均正确，但 `Entry.get()` 为空 | 单函数内 `set → paste → sleep → restore` | 拆成 `paste_text()`（返回 `PendingInsert`）+ `finish_insert()`；托盘侧用 `root.after(350ms)` 延迟恢复，并在新任务/退出时 `_flush_pending_restore()`；同步 `insert_text()` 增加 `pump(duration)` 供验收脚本驱动消息循环 | 真实 UI 下 Ctrl+V 能落到目标输入框；剪贴板最终仍完整还原 |

R1、R4、R9、R10 是涉及运行时正确性的四个缺陷，属于**编码与打包阶段新发现**，不在最初 smoke test 的三条硬阻塞里。R10 最严重：它不会让任何单元测试失败，却会让首版在真实桌面上「按了键、识别正确、什么都没插入」。发现路径是 `scripts/acceptance.py` 的真实剪贴板 + 真实 Ctrl+V 进 Tk Entry 黑盒实验，随后用一次性诊断脚本（临时产物，未随仓库分发）定位到剪贴板恢复时序。修复后的回归证据：

```
real_target_experiment   : same_window_diff = []  /  after_switch_diff = [hwnd,pid,tid,title,class_name,focus_hwnd,uia_runtime_id]
real_clipboard_insert    : entry_text = "语音输入验收文本"  /  clipboard_identical = true
```

---

## 二、模块清单与职责

| 模块 | 职责 | 关键边界 |
|---|---|---|
| `state.py` | `Phase` 状态机 + `task_id` / `generation` 令牌；`accept_result` 是唯一插入闸门 | native decode 不可中断，取消只能事后拒绝 |
| `manifest.py` | 模型单文件下载、大小 + SHA-256 校验、写本地 manifest | 权重许可证随包，不宣称 Apache-2.0 |
| `target.py` | Win32 前台窗口 + UIA 焦点元素复合指纹；`compare()` 容错比较 | Chromium 网页控件靠 `RuntimeId` 区分；UIA 单边缺失不算目标变化 |
| `hotkey.py` | 低层键盘事件纯解析 + detect-only `WH_KEYBOARD_LL` 线程 | 实测 down=`WM_SYSKEYDOWN(0x104)` / up=`WM_KEYUP(0x101)`；`LPARAM` 必须 `c_ssize_t` |
| `audio.py` | 有界内存 `AudioBuffer` + 回调式 `Recorder` | 音频不落盘；原生 16kHz 设备优先；WDM-KS 不支持 blocking |
| `engine.py` | `sherpa-onnx` OfflineRecognizer 封装 + `read_wav` | `OfflineStream` 无 cancel/reset；不伪装流式 |
| `clipboard.py` | 全格式剪贴板快照/恢复 + `SendInput` Ctrl+V 插入 | `paste_text()` / `PendingInsert` / `finish_insert()` 三段式，粘贴后延迟 350ms 再恢复；GDI 句柄类格式无法逐字节还原，显式记为 `skipped` |
| `config.py` | JSON 配置的逐字段容错读写 | 坏配置不阻止启动 |
| `flow.py` | 可注入的纯逻辑编排：按下→解码→目标确认→插入 | 无 Tk / 无 Win32，便于 TDD |
| `ui.py` | 状态浮窗（`WS_EX_NOACTIVATE`）+ 设置窗口 + 托盘图标 | 浮窗绝不抢焦点，否则目标指纹失效 |
| `app.py` | 单进程装配、线程划分、`post` 队列、托盘/设置/自检 | 无 engine worker 子进程、无 IPC |
| `cli.py` | `run` / `selftest` / `check-model` / `--version` | frozen 下重定向 `comtypes.gen` 到可写目录，`run` 隐藏控制台 |

---

## 三、线程模型（修订后）

```
main/Tk   : _pump() → _process_posted() → hook 事件解码 → state 变更 → 插入
hook      : WH_KEYBOARD_LL detect-only → KeyboardHook._queue
ASR       : flow.decode() → post(_on_decoded)        [不碰 Tk / 不碰 state]
model     : verify/download/load → post(_engine_ready)
tray      : pystray 菜单 → post(action)
```

`post()` 是唯一合法的跨线程回投方式。任何工作线程直接调用 Tk API 都视为缺陷。

---

## 四、验证入口

```bash
.venv/Scripts/python.exe -m pytest --tb=short -q        # 单元测试 + 集成测试
.venv/Scripts/python.exe -m voicetray --version
.venv/Scripts/python.exe -m voicetray check-model
.venv/Scripts/python.exe -m voicetray selftest --full   # 含中英文真实推理
.venv/Scripts/python.exe scripts/build.py               # PyInstaller onedir 构建 + 产物验证
.venv/Scripts/python.exe scripts/acceptance.py          # 12 条黑盒条款取证
```

产物：`build-report.json`、`acceptance-report.json`（均为本地运行产物，不随仓库分发）与仓库内的 `ACCEPTANCE.md`。

---

## 五、边界（沿用并补充）

- 真人按住右 Alt 讲话、网页输入框插入、托盘长期驻留属于**人工验收**，自动化只覆盖「模拟按键 + 合成/官方音频 + 真实剪贴板 Ctrl+V + 目标指纹比对」这一层，报告里明确标 MANUAL，不代人工声称通过。
- 「关闭 DSH」对照在本机无法构造（全盘未检出 DSH）；独立性以进程依赖、自带权重/runtime、断网运行三项证明。
- 首版只承诺松开后插入完整转写；SenseVoiceSmall 是 offline CTC，VAD 分段仍是逐段离线解码。
- 剪贴板中的位图/图元文件等 GDI 句柄格式无法逐字节保存，恢复时会报告 `skipped_formats`；文本与常规自定义格式完整还原。
