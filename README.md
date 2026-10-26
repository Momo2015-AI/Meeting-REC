<div align="center">

# Meeting-REC

**Windows 会议实时监听 · 转写 · 翻译 · 纪要工具**

捕获系统混音 → 实时语音识别 → 大模型翻译 → 置顶双语字幕窗 → Markdown 纪要与会议总结

[![Python](https://img.shields.io/badge/python-3.10%2B-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)
[![Platform](https://img.shields.io/badge/platform-Windows-0078D6?logo=windows&logoColor=white)](https://learn.microsoft.com/windows/)
[![Downloads](https://img.shields.io/github/downloads/Momo2015-AI/Meeting-REC?color=blue)](https://github.com/Momo2015-AI/Meeting-REC/releases)

</div>

---

## 📌 这是什么

Meeting-REC 是一个**完全跑在你自己电脑上**的会议记录工具。

它做一件事：**把正在电脑上播放的会议声音实时变成中文双语字幕，并在会议结束时自动产出一份结构化的 Markdown 会议纪要。**

- 🎧 **监听系统输出**（不是麦克风）——线上会议、本地播放、演示录像通吃，参会者无需任何改动
- 🔒 **音频不出本机**——采集、识别、转写全部在本地完成，只有**翻译文本**会送到你配置的推理服务
- 🪟 **置顶字幕窗**——始终置顶、半透明、可拖动，不打断你的其他窗口
- 🧠 **自动总结**——点「停止」即生成五段式会议纪要（概要 / 要点 / 决议 / 待办 / 风险）
- 🌐 **多后端回退**——主翻译服务额度耗尽或限流时，自动切到备用服务商，会议不中断

> **隐私设计**：仓库内不包含任何密钥、账号、端点或机器信息。
> 密钥只放在你的本机 `secrets/` 目录，该目录已被 `.gitignore` 排除。
> 语音模型体积较大（约 140 MB），同样不入库，提供一键下载脚本。

---

## ✨ 功能特性

| 能力 | 说明 |
|---|---|
| **系统音频环录** | 基于 WASAPI Loopback，抓取默认输出设备的混音，无需虚拟声卡 |
| **流式语音识别** | Vosk 引擎本地推理，16 kHz 单声道，实时出字（partial），句子完成即定稿（final） |
| **实时翻译** | 任意 OpenAI 兼容 `/chat/completions` 端点，独立工作线程，翻译卡顿不阻塞识别 |
| **多后端回退** | 主后端连续失败自动熔断，切换备用服务商；60 秒后自动探测恢复 |
| **模型回退链** | 同一后端内多个模型按顺序降级，抵御单模型限流 |
| **双语字幕窗** | tkinter 置顶窗，原文/译文成对显示，可拖动、可调透明度 |
| **手动开始** | 默认不自动采集，点击「开始」后才监听系统音频 |
| **实时落盘** | 每一句原文与译文实时写入 Markdown，中途崩溃也不丢内容 |
| **音频留档** | 原始录音保存为 WAV（16 kHz / 16 bit / 单声道），可事后重新识别校正 |
| **自动总结** | 点击「停止」即调用大模型生成结构化纪要，同时写入 `summary.md` |
| **补生成总结** | 额度恢复后可对历史会话一键补写总结，无需重新开会 |
| **清屏** | 只清空界面显示，**不删除**任何已写入的文件内容 |
| **独立进程启动** | 可挂机运行，终端关闭不受影响，日志落盘便于排障 |

---

## 🏗️ 架构

```
                    ┌─────────────────────────────────────────────┐
                    │              采集层 (capture.py)            │
  系统混音 ───────► │  soundcard / WASAPI Loopback                │
  (默认输出设备)     │  16 kHz · 单声道 · 100 ms 分块              │
                    └──────────────────┬──────────────────────────┘
                                       │ PCM frames
                    ┌──────────────────▼──────────────────────────┐
                    │              识别层 (asr.py)                 │
                    │  ctypes 直绑 libvosk.dll（无需 vosk Python 包）│
                    │  → partial 实时中间结果                      │
                    │  → final  完整语句                           │
                    └──────────────────┬──────────────────────────┘
                                       │ 文本事件
                    ┌──────────────────▼──────────────────────────┐
                    │              翻译层 (translate.py)          │
                    │  独立线程 · 超时控制 · 模型链降级            │
                    │  后端链：primary → alt-1 → alt-2            │
                    │  熔断 + 自愈探测                             │
                    └──────────────────┬──────────────────────────┘
                                       │ (原文, 译文) 事件
          ┌────────────────────────────┼────────────────────────────┐
          │                            │                            │
  ┌───────▼────────┐         ┌─────────▼─────────┐      ┌───────────▼──────────┐
  │  界面层 (ui.py) │         │  落盘 (transcriber)│      │  总结 (summary)      │
  │  置顶字幕窗     │         │  transcript.md     │      │  五段式 Markdown      │
  │  开始/停止/清屏 │         │  原文译文成对      │      │  summary.md          │
  └────────────────┘         │  meeting.wav       │      └──────────────────────┘
                             └───────────────────┘
```

### 关键设计取舍

| 决策 | 原因 |
|---|---|
| **ctypes 直绑 DLL 而不用 vosk Python 包** | 官方包在 Python 3.14 上依赖链不完整（缺 `srt`/`requests`），直绑可绕过且启动更快 |
| **翻译与识别分线程** | 翻译遇到限流排队时，识别不能被拖住——否则字幕会卡顿 |
| **落盘先于展示** | 先写文件再更新界面，界面卡顿或崩溃不会丢纪要 |
| **预检失败不禁用翻译** | 启动时探测失败只记录状态，首句仍真实重试，避免误判导致全程无译文 |
| **同秒重开会话加后缀** | 目录名精确到秒时同秒重开会覆盖上一场，加 `_2`/`_3` 后缀杜绝覆盖 |
| **手动开始而非自动采集** | 打开工具不代表立刻录音，避免无意录下私人音频 |
| **翻译带上下文 / 批处理** | 携带前几句原文稳定术语与指代；密集发言时合并请求，省钱省时，漏行自动逐句回退 |
| **推理模型关思考** | 火山方舟 deepseek/seed 系列默认先内部推理再翻译，长句会烧满配额甚至整场零译文；`extra_body = { thinking = { type = "disabled" } }` 强制关闭，实测 1-3 秒出译文 |
| **长会议完整总结** | 转写过长时自动分块小结后归并，不再截断丢后半场 |
| **设备热拔插重连** | 耳机拔插/默认设备切换导致环录中断后，自动重连新输出设备，录音无缝续写 |
| **whisper 离线重识别** | 一键用 whisper.cpp 重转写本场 WAV，修正 Vosk small 模型的识别误差 |
| **单实例拉起** | 重复启动 `run.py` 不会开第二个窗口，自动把已有字幕窗调至前台 |

---

## 🚀 快速开始

### 方式一：一条命令拉起（Windows）

```powershell
git clone https://github.com/Momo2015-AI/Meeting-REC.git
cd Meeting-REC
python scripts/download_models.py --all
copy config.toml config.local.toml      # 按需修改端点/模型
python run.py
```

### 方式二：完整部署（推荐）

<details>
<summary><b>1. 安装依赖</b></summary>

```powershell
# 建议用 venv，而不是 --user 装进系统 Python
python -m venv .venv
.venv\Scripts\python.exe -m pip install numpy "soundcard==0.4.6" cffi
```

> **踩坑笔记（2026-10-23）**：在一台 Windows 10 + Python 3.14 的公司电脑上，
> `--user` 装进系统解释器的库被 DLP 文件加密策略批量改写（`.py` → `.IPGSD`），
> 之后 `import soundcard` 变成空壳 namespace 包，报
> `module 'soundcard' has no attribute 'default_speaker'`。
> 解决：改用项目内 venv（新写入的文件不受该策略影响），或用管理员白名单目录。
> 排查方法：`import soundcard; print(soundcard.__path__)` 出现
> `_NamespacePath(...)` 即说明 `__init__.py` 已被加密破坏。

> 仓库内置 `vendor/` 离线 wheel（`soundcard`、`pycparser`），离线环境可用：
> `.venv\Scripts\python.exe -m pip install --no-deps vendor\pycparser.whl vendor\soundcard-0.4.6-py3-none-any.whl`

</details>

<details>
<summary><b>2. 下载语音模型</b></summary>

```powershell
python scripts/download_models.py --all     # 中英模型都下
python scripts/download_models.py           # 只下英文模型
python scripts/download_models.py --cn      # 只下中文模型
```

模型来自公开的 Hugging Face 镜像，解压后目录名须与 `config.toml` 中的 `model_dir` 一致。

> 识别引擎运行时（`models/lib/` 下的 DLL）**已随仓库入库**，无需另行下载。

</details>

<details>
<summary><b>3. 配置翻译后端与密钥</b></summary>

```powershell
copy secrets\llm_api_key.txt.example secrets\llm_api_key.txt
# 打开 secrets\llm_api_key.txt，把你的 API Key 粘贴进去（一行一个）
```

然后编辑 `config.local.toml`（本机私有配置，不入库）：

```toml
[translate]
base_url = "https://你的服务端点/v1"
model    = "你的模型标识"
target_lang = "zh"        # 英文会议 → 中文纪要

[[translate.alt_backends]]                    # 可选：备用后端，额度耗尽时自动接管
name = "alt-1"
base_url = "https://备用服务端点/v1"
model = "备用模型标识"
```

> 密钥优先级：`api_key_file` > `api_key_env`（环境变量）> `api_key`（配置内联，不推荐）。
> 推荐用 `api_key_file`，避免密钥随配置一起被误提交。

</details>

<details>
<summary><b>4. 启动</b></summary>

```powershell
python run.py                    # 悬浮字幕窗（推荐）
python run.py --no-ui            # 无界面，控制台输出
python run.py --no-ui --duration 1800   # 控制台模式跑 30 分钟
python run.py --list-devices     # 列出可用音频设备，排查采集问题
python launch.py --detach        # 以独立进程挂机运行
```

</details>

### 使用界面

| 元素 | 行为 |
|---|---|
| **▶ 开始** | 开始监听系统音频。**打开工具不会自动录音**，必须主动点击 |
| **■ 停止** | 结束本场会议，关闭录音文件并自动生成会议总结 |
| **清屏** | 只清空字幕界面显示，已写入的 Markdown / WAV **完全保留** |
| **打开目录** | 打开本次会话的输出文件夹 |
| **拖动标题栏** | 移动字幕窗位置 |
| **`Esc`** | 退出应用（退出时自动收尾，纪要不丢） |

顶部状态栏从左到右：采集状态（● 待开始 / 采集中 / 总结中 / 已停止）→ 语种方向 → 翻译后端徽标（翻译已就绪 / 仅转写）。

---

## 📄 输出产物

每次会议在 `output/meeting_YYYYmmdd_HHMMSS/` 生成一个目录：

```
output/meeting_20261022_143015/
├── transcript.md     完整纪要：头部元信息 + 逐句原文/译文成对 + 会议总结
├── summary.md        独立会议总结（五段式）
└── meeting.wav       原始录音（16 kHz / 16 bit / 单声道）
```

`transcript.md` 结构示例：

```markdown
# 会议双语纪要

- 开始时间：2026-10-22 14:30:15
- 语种方向：EN → ZH
- 采集方式：系统输出环录（WASAPI Loopback）

## 逐句转写

**1** [00:00:03]
> The 261 kilowatt hour outdoor cabinet only supports Modbus TCP, RTU and CAN protocols.

261 千瓦时户外柜仅支持 Modbus TCP、RTU 和 CAN 协议。

## 会议总结

## 会议概要
...
```

**同一秒内连续开两场**会自动追加 `_2` / `_3` 后缀，绝不覆盖上一场记录。

---

## ⚙️ 配置说明

完整参数见 `config.toml`（通用模板）。常用项：

| 配置项 | 默认 | 说明 |
|---|---|---|
| `asr.model_dir` | 英文 small 模型 | 中文会议改 `models/vosk-model-small-cn` |
| `asr.source_lang` | `en` | 会议发言语言（`en` / `zh`） |
| `translate.target_lang` | `zh` | 译文语言 |
| `translate.base_url` | 占位 | 任意 OpenAI 兼容端点 |
| `translate.model` | 占位 | 模型标识；配 `fallback_models` 做模型级降级 |
| `translate.timeout` | `20.0` | 单次翻译超时（秒），太小易误判失败 |
| `translate.system_prompt` | 通用译员 | 行业术语/风格约束，按需改写 |
| `[[translate.alt_backends]]` | 示例占位 | 备用后端，可重复配置多个 |
| `summary.enabled` | `true` | 停止时是否自动总结 |
| `summary.system_prompt` | 五段式 | 会议纪要结构，可按你的规范改写 |
| `ui.autostart` | `false` | 是否自动开始采集 |
| `audio.save_audio` | `true` | 是否留档 WAV |

**配置文件加载优先级**：命令行 `--config` > `config.local.toml` > `config.toml`。

---

## 🧪 测试

项目自带 8 组测试脚本，覆盖从识别到生命周期到后端回退的完整链路：

```powershell
python tests\smoke_test.py             # 识别 / 翻译 / 管线 冒烟
python tests\feature_test.py           # 配置 / MD 结构 / 停止总结 / 清屏
python tests\exit_test.py              # 各种退出方式下总结都不丢
python tests\lifecycle_test.py         # 开始 / 停止 / 清屏 / 重开 会话生命周期
python tests\backend_fallback_test.py  # 翻译后端回退、熔断与模型降级
python tests\ui_test.py                # 字幕窗渲染与事件分发
python tests\real_capture_test.py      # 真实放音 → 环录 → 识别端到端
```

`real_capture_test.py` 会真实占用音频设备，请先关闭正在运行的实例。

---

## 🧩 如何移植 / 二次开发

### 移植到其他平台

| 平台 | 难度 | 说明 |
|---|---|---|
| **Windows** | ✅ 已支持 | 采集依赖 `soundcard`（WASAPI），识别依赖 `libvosk.dll` |
| **macOS** | ⚙️ 需改造 | 采集换成 `sounddevice` + BlackHole/Loopback 虚拟声卡；`models/lib/` 换成 `libvosk.dylib`；`start.bat` 改 `start.sh` |
| **Linux** | ⚙️ 需改造 | 采集用 `pulseaudio` 的 `module-loopback`；UI 换 GTK/Qt 或改用 `pywebview`；打包为 AppImage |

跨平台改造点集中在两处，其余模块（识别封装、翻译回退、落盘、总结）**与平台无关，无需改动**：

```
需要替换：src/capture.py   （平台音频采集）
需要替换：src/ui.py        （可选，tkinter 在三大平台都可用）
无需改动：src/asr.py  src/translate.py  src/transcriber.py  summarize.py
```

### 接入自己的推理服务

只要服务暴露 `POST {base_url}/chat/completions` 且兼容 OpenAI 协议即可，包括：

- 本地部署：**Ollama**、vLLM、LM Studio、llama.cpp server
- 云端服务：任何提供 OpenAI 兼容 API 的推理平台
- 自建网关：Nginx 反代 + 鉴权，或企业内网统一模型网关

```toml
[translate]
base_url = "http://localhost:11434/v1"
api_key  = "ollama"
model    = "qwen2.5:7b-instruct"
```

### 换用其他识别引擎

`src/asr.py` 是唯一的识别耦合点，只要实现下列接口即可替换：

```python
class ASREngine:
    def __init__(self, model_dir: str): ...
    def start(self) -> None: ...
    def accept(self, pcm_bytes: bytes) -> str | None: ...  # 返回 partial 文本
    def result(self) -> str: ...                          # 返回 final 文本
    def stop(self) -> None: ...
```

可替换为 Whisper / faster-whisper / FunASR / 云端 ASR 等。

### 定制会议纪要结构

改 `config.local.toml` 中的 `summary.system_prompt` 即可，例如换成"行动项 + 责任人 + 截止日期"三段式。纪要只依据转写内容生成，提示词里明确约束可防止模型臆造。

### 增加导出格式

`src/transcriber.py` 中的 `_write_translation()` 是唯一写盘入口，在其后追加 DOCX / PDF / Notion / 企业微信 导出即可，不影响既有链路。

---

## ❓ 常见问题

<details>
<summary><b>字幕窗打开了，但一直没反应？</b></summary>

1. 确认点过「▶ 开始」（打开工具不会自动录音）
2. 运行 `python run.py --list-devices` 查看默认输出设备是否正确
3. 确认系统音量不为 0，且会议声音走的是**默认输出设备**

</details>

<details>
<summary><b>识别没有结果 / 识别成乱码？</b></summary>

- `asr.model_dir` 与 `asr.source_lang` 必须匹配（英文会议用英文模型）
- 采样率固定 16 kHz，不要改 `audio.sample_rate`
- 语速过快或重音过重时，识别准确率会明显下降

</details>

<details>
<summary><b>翻译一直失败？</b></summary>

- 检查 `secrets/` 下的密钥文件是否非空
- 某些平台的 `GET /models` 返回的是**全量模型目录**，不代表你的账号已开通该模型。
  用 `python check_ark.py` 之类脚本做**真实调用探测**才能确定可用性
- 检查 `summary.system_prompt` / `translate.system_prompt` 是否超出长度限制

</details>

<details>
<summary><b>会议中途退出，纪要丢了吗？</b></summary>

不会。逐句内容在识别完成时**就已实时写入** `transcript.md`；只有"总结"部分可能因强制杀进程而缺失，可用补生成脚本修复：

```powershell
python summarize.py --all
```

</details>

<details>
<summary><b>清屏会不会删掉记录？</b></summary>

不会。「清屏」只操作界面控件，磁盘文件完全不动。

</details>

<details>
<summary><b>录音占多大空间？</b></summary>

16 kHz / 16 bit / 单声道 ≈ **115 MB/小时**。不需要留档时把 `audio.save_audio` 设为 `false`。

</details>

---

## ⚖️ 合规与使用须知

- **录音前请征得与会者同意**，遵守所在司法辖区的录音、隐私与数据出境法规
- 本工具捕获的是**系统输出音频**，只应在你有权录制的内容上使用
- 音频识别在本机完成；**转写文本会发送到你配置的推理服务**，请自行确认该服务的数据处理条款
- 仓库不包含任何密钥、账号或机器信息；请不要把 `secrets/`、`config.local.toml`、`output/` 提交到版本库
- 识别模型的许可遵循其各自发布页说明

---

## 📄 License

[MIT](LICENSE) © Meeting-REC contributors

---

<div align="center">

**如果这个工具对你有用，欢迎点一个 ⭐ Star 和提 Issue。**

</div>
