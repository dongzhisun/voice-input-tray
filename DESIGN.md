# 黑盒验收基线与修订设计（v2，方案解锁后）

**日期**：2026-11-12　**前提**：用户裁定「方案可以修改，只要黑盒能满足要求即可」，因此实现细节可以调整，但下列黑盒条款逐条必须满足。

---

## 一、黑盒验收条款（12 项，逐条可测）

| # | 条款 | 可观测行为 | 验证方式 |
|---|---|---|---|
| 1 | 独立运行 | 不启动/不连接 DSH，应用能完成一次完整转写插入 | 进程树 + 无 DSH 依赖 |
| 2 | 模型下载与校验 | 模型缺失时能下载，SHA-256 不匹配时拒绝加载并报错 | manifest 单测 + 实测下载 |
| 3 | 离线运行 | 断网状态下仍可识别并插入 | 断网后跑 smoke test |
| 4 | 按住/释放 | 按住右 Alt 开始录音，松开后插入完整文本 | 真实 UI |
| 5 | 中文/英文 | 中英文语音都能正确转写 | 官方 test_wavs |
| 6 | 原目标确认 | 录音期间切换窗口/输入框后**不插入**，并提示 | 目标快照比对 |
| 7 | 剪贴板保护 | 插入前后用户剪贴板内容一致 | 剪贴板序号 + 内容 |
| 8 | Esc 和迟到结果 | Esc 取消后不插入；取消后到达的 ASR 结果被丢弃 | 状态机单测 |
| 9 | 无历史录音 | 音频只存内存，磁盘无录音文件 | 目录扫描 |
| 10 | 托盘和设置 | 托盘可见、可退出；设置可改热键/设备/语言 | 真实 UI |
| 11 | 错误处理 | 模型损坏/设备不可用/推理异常均有明确提示且不崩溃 | 故障注入 |
| 12 | 打包和真实 UI 验收 | PyInstaller 产物可启动，真实按键+录音+插入跑通 | 构建 + UI 验收 |

---

## 二、相对原规格的修订（均为实现细节，黑盒不变）

| 项 | 原规格 | 修订后 | 理由（实测） |
|---|---|---|---|
| runtime | Python 3.11 + 1.12.40 cp311 wheel | **本机 3.14.3 隔离 venv + 1.12.40 cp314 wheel** | 本机无可用 3.11 桌面版；PyPI 1.12.40 有 cp314 win_amd64 wheel；实测导入/推理均通过 |
| 模型来源 | 官方 GitHub release archive + SHA-256 | **hf-mirror 官方转换仓库单文件** | github.com / raw.githubusercontent.com 本机 HTTP 000 超时；镜像 HEAD 的 `X-Linked-Etag` 就是 LFS SHA-256，实测与下载文件完全一致 |
| 「原目标确认」实现 | Win32 焦点快照 | **HWND+PID+TID+标题 + UIA 焦点元素 RuntimeId 复合指纹** | `GetGUIThreadInfo` 对 Chromium 网页不可观测；UIA 实测能拿到 `RuntimeId`/`ClassName`/`ProcessId` |
| 麦克风 | 默认设备 | **显式设备选择 + 16kHz 原生设备优先 + 回调模式** | `default input = [-1,1]`；WDM-KS 不支持 blocking read；device=16 回调模式实测可用 |
| 快捷键 | 右 Alt | **右 Alt（不变）**，但 keydown/keyup 按 vkCode + 消息族分别判定 | 实测 down = `WM_SYSKEYDOWN`(0x104)、up = `WM_KEYUP`(0x101)，不对称 |
| DSH 对照 | 「关闭 DSH 离线运行」 | 本机未安装 DSH，改以「进程不引用 DSH 任何资产」+ 断网运行验收 | 全盘未检出 DSH |

**不变的部分**：单进程（无 engine worker 子进程、无 IPC）、KVM 式原生 Hook 只检测不吞事件、`task_id` + `generation token` 拒绝迟到结果、native decode 不可中断时仍拒绝回调、首版只承诺松开后插入完整转写、不宣称流式、内存音频不落盘。

---

## 三、架构（单进程，多线程）

```
主线程      Tkinter 隐藏根窗口：状态浮窗 / 设置窗口 / after() 事件泵
托盘线程    pystray（仅图标与菜单，命令投递到主线程）
Hook 线程   WH_KEYBOARD_LL + 自建消息循环，只检测不吞；解析右 Alt / Esc
音频线程    sounddevice 回调，16k 单声道 float32 写入内存 ring buffer
ASR 线程    sherpa-onnx OfflineRecognizer，一次任务一次 decode
主线程      剪贴板保护 + 目标确认 + SendInput 插入
```

### 取消语义（不变）

```
begin(task_id=uuid4, generation=g)   # 按下
cancel()  → generation += 1          # Esc / 松开前异常 / 窗口切换
decode 完成后 on_result(task_id, g, text):
    if g != current_generation or task_id != current_task_id: DROP
    if target_fingerprint_mismatch: DROP + 提示
    else: insert(text)
```

native decode 实测**不可中断**（`OfflineStream` 只有 `accept_waveform`/`result`，无 cancel/reset），因此必须靠事后 token 校验丢弃迟到结果，不能靠中止线程。

### 原目标确认指纹

按下瞬间记录 `TargetSnapshot`：
`hwnd` / `pid` / `tid` / `window_title` / `class_name` / `uia_runtime_id` / `uia_name` / `clipboard_seq`

松开并推理完成后重新采样，逐项比对；**任一不一致则不插入**，状态浮窗提示「目标已变化，已取消插入」。UIA 不可用时降级为 Win32 四项比对，并在设置里标注降级。

---

## 四、模型 manifest（实测值，可复现）

```
dir    assets/models/sensevoice-int8-2024-07-17/
model  model.int8.onnx  size 239233841
       sha256 c71f0ce00bec95b07744e116345e33d8cbbe08cef896382cf907bf4b51a2cd51
       （与镜像 HEAD 的 X-Linked-Etag 完全一致）
tokens tokens.txt       size 315894
       sha256 f449eb28dc567533d7fa59be34e2abca8784f771850c78a47fb731a31429a1dc
LICENSE                 size 71
       内容 "Ref to https://github.com/modelscope/FunASR?tab=readme-ov-file#license"
```

**许可边界**：sherpa-onnx 软件是 Apache-2.0；模型权重许可证由 FunASR 单独规定，仓库 LICENSE 只是引用链接。随包保留该 LICENSE 原文，**不宣称权重为 Apache-2.0**，也不宣称与 DSH 资产相同。

### 离线推理实测（本机，1.12.40）

```
zh.wav 5.59s → "开放时间早上9点至下午5点。"  lang=<|zh|>  decode 610ms
en.wav 7.15s → "The tribal chieftain called for the boy and presented him with 50 pieces of code."  lang=<|en|>  decode 849ms
```

---

## 五、已知边界（不隐瞒）

- 真实 UI 验收（真人按住右 Alt 讲话、网页输入框插入）需要交互，自动化只能覆盖到「模拟按键 + 合成音频 + 目标指纹比对」这一层。
- 「DSH 关闭」对照在本机无法构造；独立性以进程依赖与断网运行证明。
- UIA 在部分自绘应用中可能取不到 RuntimeId，此时降级为 Win32 指纹，网页内同一窗口跨输入框切换的区分能力弱于原生控件。
- 首版不承诺讲话过程中逐字输出；SenseVoiceSmall 是 offline CTC，VAD 分段仍是逐段离线解码。
