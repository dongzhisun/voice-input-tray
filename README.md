# voice-input-tray

不依赖 DSH 的 Windows 离线语音输入托盘程序：**按住右 Alt 说话，松开后把完整转写插入按下按键时所在的输入框**，桌面软件与 Chromium 网页输入位置都适用。

> 版本 0.1.0 · 验证环境 Windows 11 x64 / Python 3.14.3 / sherpa-onnx 1.12.40 / SenseVoiceSmall int8

## 特性

- **完全离线**：识别由本机 sherpa-onnx CPU 推理完成，不调用云端接口；断网仍可用。
- **单进程**：无 engine worker 子进程、无 IPC。低层键盘钩子只检测、不吞按键。
- **原目标确认**：按下时记录 Win32 + UIA 复合指纹，推理完成后重新比对；目标变化则拒绝插入，不会把文字打进错误位置。
- **Esc 取消**：底层 native decode 不可中断，因此用 `task_id` + `generation` 令牌在结果返回时丢弃已取消的任务。
- **剪贴板保护**：插入走剪贴板 + `Ctrl+V`，粘贴后延迟恢复用户原有全部可读取格式。
- **不留录音**：音频只写有界内存缓冲区，磁盘上不产生录音文件；转写文本也不落盘。
- **不流式**：SenseVoiceSmall 是 offline CTC，首版只承诺松开后插入完整结果。

## 快速开始

### 1. 准备环境

需要 Windows 10/11 x64 与 Python 3.14.3（本机验证组合；`sherpa-onnx 1.12.40` 也提供 cp311 的 Windows wheel，但本项目未在该组合上验证）。

```bat
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
```

### 2. 补齐模型权重

**模型权重不随本仓库分发**：`model.int8.onnx` 单文件约 228 MB，超过 GitHub 100 MB 的单文件限制，且权重许可证与代码许可分开。程序会在首次运行时自动从 hf-mirror 的官方转换仓库下载并做 SHA-256 校验，也可以手工触发：

```bat
.venv\Scripts\python.exe -c "from voicetray import config, manifest; manifest.ensure_model(config.default_model_dir())"
.venv\Scripts\python.exe -m voicetray check-model
```

校验口径（`assets/models/sensevoice-int8-2024-07-17/`）：

| 文件 | 字节数 | SHA-256 |
|---|---|---|
| `model.int8.onnx` | 239233841 | `c71f0ce00bec95b07744e116345e33d8cbbe08cef896382cf907bf4b51a2cd51` |
| `tokens.txt` | 315894 | `f449eb28dc567533d7fa59be34e2abca8784f771850c78a47fb731a31429a1dc` |
| `LICENSE` | 71 | `221c6df10b0931a5629adad671ea48fb7747e034c414b6d2bfa275bc3dd4ea17` |

大小或摘要任一不符，程序会拒绝加载，不会带病运行。`assets/test_wavs/` 下的两个官方测试音频随仓库分发，供测试使用。

### 3. 运行与自检

```bat
REM 启动托盘应用
.venv\Scripts\python.exe -m voicetray run
REM 打印版本
.venv\Scripts\python.exe -m voicetray --version
REM 含真实模型推理的完整自检（输出 UTF-8 JSON）
.venv\Scripts\python.exe -m voicetray selftest --full
REM 校验模型文件与 SHA-256
.venv\Scripts\python.exe -m voicetray check-model
```

### 4. 测试、打包与验收

```bat
REM 125 个用例（需要模型权重）
.venv\Scripts\python.exe -m pytest -q
REM PyInstaller onedir 产物 + 6 项校验
.venv\Scripts\python.exe scripts\build.py
REM 12 条黑盒条款自动取证
.venv\Scripts\python.exe scripts\acceptance.py
```

`build.py` 会把旧产物重命名为 `_prev_<timestamp>` 而不是删除，输出 `dist/VoiceTray/` 与 `build-report.json`；`acceptance.py` 输出 `acceptance-report.json` 与 `ACCEPTANCE.md`。这些运行时产物都被 `.gitignore` 排除。

## 目录结构

```
voicetray/            应用源码（12 个核心模块 + 包入口）
tests/                pytest 用例
scripts/              构建脚本与黑盒验收脚本
assets/test_wavs/     官方中英文测试音频
voicetray_main.py     PyInstaller 入口
VoiceTray.spec        PyInstaller onedir 配置
USER_GUIDE.md         用户使用手册
DESIGN.md             12 条黑盒验收基线
DESIGN_v2.md          实现阶段 R1-R10 修订记录
ACCEPTANCE.md         验收结论与人工验收清单
```

`assets/models/` 由程序运行时生成，不在版本控制中。

## 已知边界

- 首版不提供逐字流式输出；快捷键固定为右 Alt，取消键固定为 Esc，均不可配置。
- Windows UIPI 会阻止低完整性进程向更高完整性窗口注入按键；目标以管理员运行时，请以管理员身份运行本程序。
- 剪贴板中的 GDI 句柄类格式（位图、图元文件、调色板）无法逐字节还原，会显式报告而不是静默忽略。
- 少数自绘应用取不到 UIA `RuntimeId`，此时目标确认降级为 Win32 窗口指纹。
- 真人按住讲话、网页输入框插入、托盘长期驻留属于人工验收项，见 `ACCEPTANCE.md`，自动化不代人工声称通过。
- 无单实例保护，请勿重复启动。

## 许可说明

- **`sherpa-onnx` 软件为 Apache-2.0**；SenseVoice 模型权重的许可证由 FunASR 单独规定，随权重保留其 `LICENSE` 原文，本仓库**不宣称权重为 Apache-2.0**，也不宣称与 DSH 的资产相同。
- 本仓库代码目前**未单独指定开源许可证**，因此默认保留所有权利。若需要允许他人使用或再分发，请先补充 LICENSE 文件。
