# VoiceTray 黑盒验收报告

- 生成时间：2026-11-12T04:05:16.391139+00:00
- Runtime：3.14.3
- 自动通过：12/12
- pytest：.....................................................                    [100%]

| # | 条款 | 自动结论 | 关键证据 |
|---|---|---|---|
| 1 | 独立运行 | PASS | pytest, model_dir_bundled, dsh_import_scan, frozen_bundle |
| 2 | 模型下载与校验 | PASS | corrupt_model, manifest, tests |
| 3 | 离线运行 | PASS | zh, en, offline_text, socket_creation_blocked |
| 4 | 按住/释放 | PASS | hook_error, sent_down, sent_up, raw_events |
| 5 | 中文/英文 | PASS | zh, en |
| 6 | 原目标确认 | PASS | before, same_window_diff, after_switch_diff, uia_available |
| 7 | 剪贴板保护 | PASS | insert_result, entry_text, clipboard_original_formats, clipboard_after_formats |
| 8 | Esc 和迟到结果 | PASS | pytest, tests |
| 9 | 无历史录音 | PASS | audio_files, ok |
| 10 | 托盘和设置 | PASS | tray_icon_created, tray_visible, settings_window_created, overlay_noactivate |
| 11 | 错误处理 | PASS | corrupt_model, pytest, tests |
| 12 | 打包和真实 UI 验收 | PASS | ok, all_ok, checks, exe |

## 逐条说明

### 1. 独立运行 — PASS

独立性证据 = 进程依赖清单 + 自带权重/runtime + 断网运行；本机未安装 DSH，无法构造「关闭 DSH」真实对照。

### 2. 模型下载与校验 — PASS

SHA-256 与 hf-mirror 的 X-Linked-Etag 一致，已实测闭环。

### 3. 离线运行 — PASS

以 socket 创建被拒绝模拟断网，真实识别仍成功。

### 4. 按住/释放 — PASS

真实 WH_KEYBOARD_LL + SendInput 注入右 Alt，验证 down=WM_SYSKEYDOWN / up=WM_KEYUP 的不对称事件对；真人按住讲话仍需人工验收。

### 5. 中文/英文 — PASS

sherpa-onnx 1.12.40 + SenseVoiceSmall int8，官方测试音频。

### 6. 原目标确认 — PASS

UIA RuntimeId 复合指纹；真实切换窗口后比对结果见 after_switch_diff。网页内跨输入框切换依赖 UIA 可用性。

### 7. 剪贴板保护 — PASS

真实 Windows 剪贴板全格式保存/恢复 + 真实 SendInput Ctrl+V 插入 Tk Entry。GDI 句柄类格式无法逐字节还原，见 skipped_formats。

### 8. Esc 和迟到结果 — PASS

native decode 实测不可中断，靠 task_id + generation 事后丢弃迟到结果。

### 9. 无历史录音 — PASS

音频只存 AudioBuffer 内存，源码无任何写音频文件调用。

### 10. 托盘和设置 — PASS

真实创建托盘图标、设置窗口、状态浮窗；托盘长期驻留与人工交互仍需人工验收。

### 11. 错误处理 — PASS

所有故障路径均有明确提示且不崩溃。

### 12. 打包和真实 UI 验收 — PASS

自动部分见本地运行 `scripts/build.py` 生成的 `build-report.json`（运行产物，不随仓库分发）；真人按键/麦克风/网页输入框为 MANUAL，脚本不代替人工声称通过。

## 真实 UI 人工验收清单（不可自动化）

- [ ] 双击 dist/VoiceTray/VoiceTray.exe，确认托盘图标出现且无控制台黑框
- [ ] 在记事本输入框按住右 Alt 说一句中文，松开后确认完整文本插入
- [ ] 在 Chromium 网页输入框重复一次，确认插入位置正确
- [ ] 录音中切到另一个窗口再松开，确认不插入并出现「目标已变化」提示
- [ ] 录音中按 Esc，确认不插入任何文本
- [ ] 复制一段文字后使用语音输入，确认剪贴板内容未被覆盖
- [ ] 断开网络后重复一次，确认仍可离线识别
- [ ] 托盘菜单打开设置，修改麦克风/语言后保存并重新识别
