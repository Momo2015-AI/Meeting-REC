# Meeting-REC · 会议实时监听与翻译（本地管线）

> 仓库：<https://github.com/Momo2015-AI/Meeting-REC> ｜ License: MIT

在**你自己的电脑上**捕获系统混音（WASAPI Loopback），实时转写并翻译成双语字幕与会议纪要。
音频全程不出本机；仅翻译文本按需送到所配置的 LLM 服务。

> **能力边界说明**：WorkBuddy 本身无法直接收听你电脑的声音输出（无音频输入通道）。
> 本工具正是在你本机建立采集链路，跑起来后由你掌控；WorkBuddy 可用于后续的纪要整理、术语校订与报告输出。

---

## 1. 架构

```
系统音频输出 ──► WASAPI Loopback 环录 ──► 重采样 16k/单声道 ──► Vosk 流式识别
                    (soundcard)              (mediafoundation)        │
                                                                   ├─► partial 实时中间结果 ──► 悬浮字幕窗
                                                                   └─► final 完整语句 ──► LLM 翻译 ──► 双语纪要 transcript.md
                                                                                          └─► meeting.wav（可回溯重识别）
```

| 环节 | 实现 | 说明 |
|---|---|---|
| 采集 | `soundcard`（Media Foundation WASAPI） | 抓默认输出设备的混音，线上会议/本地播放通吃 |
| 识别 | `libvosk.dll`（ctypes 直绑） | 不依赖 vosk Python 包，规避 Python 3.14 依赖缺口 |
| 翻译 | OpenAI 兼容 `/chat/completions` | 独立工作线程 + 启动预检，翻译卡顿不阻塞识别 |
| 呈现 | `tkinter` 置顶暗色字幕窗 | 可拖动、半透明、Esc 退出、点击「开始」后才采集 |
| 落盘 | `transcript.md` + `meeting.wav` | 双语纪要原文译文**逐句成对**写入，WAV 供事后校正 |

## 2. 目录结构

```
Meeting-REC/
├─ run.py                 主入口（字幕窗 / 无界面两种模式）
├─ launch.py              独立进程启动器（DETACHED_PROCESS + 日志）
├─ config.toml            全部配置项
├─ requirements.txt       Python 依赖
├─ start.bat              Windows 一键启动（字幕窗）
├─ start_headless.bat     Windows 一键启动（控制台）
├─ check_ark.py           方舟候选模型真实调用自检（--set 写回配置）
├─ scan_ark_all.py        并发扫描方舟全量目录模型
├─ summarize.py           独立总结器（可对已有 transcript.md 重新生成 summary）
├─ scripts/
│  └─ download_models.py  下载 Vosk 中/英文语音模型
├─ src/
│  ├─ config.py           TOML 配置加载
│  ├─ capture.py          WASAPI 环录
│  ├─ asr.py              Vosk ctypes 封装
│  ├─ translate.py        翻译后端（OpenAI 兼容 + 多级回退熔断）
│  ├─ transcriber.py      管线编排 + 纪要落盘
│  └─ ui.py               悬浮字幕窗
├─ models/
│  ├─ lib/                libvosk.dll + MinGW 运行时（4 个 DLL，已入库）
│  ├─ vosk-model-small-cn/         中文模型（68 MB，需自行下载）
│  └─ vosk-model-small-en-us-0.15/ 英文模型（71 MB，需自行下载）
├─ tests/
│  ├─ smoke_test.py       识别/翻译/管线三项冒烟
│  ├─ real_capture_test.py 真实放音→环录→识别端到端
│  ├─ ui_test.py          字幕窗渲染、事件分发与译文配对
│  ├─ feature_test.py     英→中配置 / MD 结构 / 停止总结 / 清屏（29 项）
│  ├─ exit_test.py        退出路径回归：总结在各种退出方式下都不丢（12 项）
│  ├─ lifecycle_test.py   开始/停止/清屏/重开 会话生命周期（27 项）
│  ├─ backend_fallback_test.py  翻译后端回退与熔断（14 项）
│  └─ test.wav            Vosk 官方测试音频
├─ vendor/                离线安装用 wheel
├─ secrets/
│  ├─ llm_api_key.txt     主翻译后端密钥（独立存放，不进 config.toml）
│  └─ ark_api_key.txt     火山方舟（豆包）备用后端密钥
└─ output/meeting_*/      每次会议一个目录
   ├─ transcript.md       完整纪要：头部元信息 + 逐句双语 + 会议总结
   ├─ summary.md          单独的会议总结（停止时生成）
   └─ meeting.wav         原始录音（16 kHz / 16 bit / 单声道）
```

## 3. 环境（本机已就绪）

| 组件 | 位置 / 版本 |
|---|---|
| 解释器 | `C:\Users\132854\AppData\Local\Programs\Python\Python314\python.exe`（3.14.7） |
| numpy | 2.5.2（已装） |
| cffi | 2.1.1（由托管 Python 复制，cp314 ABI 通用） |
| soundcard | 0.4.6（已装） |
| Vosk 运行时 | `models/lib/`（libvosk.dll 26 MB + libstdc++/libgcc/libwinpthread） |
| 识别模型 | 中文 / 英文 small 模型已下载解压 |

重装依赖（换机器时用，soundcard/pycparser 轮子已在 `vendor/`）：

```bash
python -m pip install --user numpy
python -m pip install --user --no-deps vendor/pycparser-*.whl vendor/soundcard-*.whl
python -m pip install --user cffi        # 或从托管 Python 的 site-packages 复制 cffi/ + _cffi_backend*.pyd
```

> 本机 cffi 2.1.1 是从托管 Python（`.../binaries/python/versions/3.14.3/Lib/site-packages`）复制到共享 user site 的，
> cp314 ABI 对 3.14.3 与 3.14.7 通用。

模型缺失时重新下载（HuggingFace 镜像，速度优于官网）：

```bash
# 中文模型
curl -L -o models/vosk-model-small-cn.zip \
  https://huggingface.co/guloooovoooo/vosk-model-small-cn/resolve/main/vosk-model-small-cn.zip
# 英文模型
curl -L -o models/vosk-model-small-en-us-0.15.zip \
  https://huggingface.co/ambind/vosk-model-small-en-us-0.15/resolve/main/vosk-model-small-en-us-0.15_c_.zip
```
解压后目录名须为 `models/vosk-model-small-cn` / `models/vosk-model-small-en-us-0.15`（与 `config.toml` 对应）。

## 4. 从 GitHub 克隆部署

```bash
git clone https://github.com/Momo2015-AI/Meeting-REC.git
cd Meeting-REC

# 1) 安装依赖（Windows）
python -m pip install --user numpy
python -m pip install --user --no-deps vendor/pycparser.whl vendor/soundcard-0.4.6-py3-none-any.whl
python -m pip install --user cffi

# 2) 下载语音模型（约 140 MB，仓库内不含）
python scripts/download_models.py --all

# 3) 配置密钥：复制示例文件后填入真实 Key
copy secrets\\llm_api_key.txt.example secrets\\llm_api_key.txt
copy secrets\\ark_api_key.txt.example secrets\\ark_api_key.txt

# 4) 启动
python run.py
```

> `models/lib/` 下的 Vosk 运行时 DLL（约 53 MB）**已随仓库入库**，无需另行下载。
> `secrets/`、`output/`、`logs/` 与语音模型均未入库（见 `.gitignore`）。

## 5. 快速开始

```bash
# 方式一：悬浮字幕窗（推荐）
python run.py

# 方式二：控制台输出（适合脚本/服务器）
python run.py --no-ui --duration 1800

# 检查音频设备
python run.py --list-devices
```

字幕窗操作：**默认不自动采集**，需点击「开始」（工具栏或字幕区中央的大按钮）后才监听系统音频；再次点击「停止」结束本场并触发总结；拖动标题区移动窗口；`Esc` 退出（退出时自动收尾并写入纪要）。
每次会议产出 `output/meeting_YYYYmmdd_HHMMSS/transcript.md` 与 `meeting.wav`（同秒内连续开两场会自动加 `_2` 后缀，绝不覆盖上一场）。

> ⚠️ **不要在应用运行时清理 `output/` 目录**——正在写入的会话文件会被删掉
> （`transcript.md` 消失、只剩 `meeting.wav`）。清理前先退出应用。

顶部状态栏从左到右：采集状态（● 待开始/采集中/总结中…/已停止）→ 语种方向（EN → ZH）→ 翻译后端徽标（翻译已就绪/仅转写）。

顶部按钮：**开始/停止**（控制采集）、**清屏**（只清界面，不动文件）、**打开目录**（打开本次会话文件夹）。

> 未点击「开始」前，程序**不会创建会话目录、不会打开系统音频设备**——完全无副作用。

### 4.1 清屏（只清屏幕，不删文字）

点「清屏」后，字幕窗内的显示内容被清空，但：

- `transcript.md` **完全不受影响**——已识别的每一句原文与译文都还在文件里；
- `meeting.wav` 录音不受影响；
- **清屏之后新识别的内容仍会继续写入 MD**（记录条数持续累积）；
- 清屏后到达的迟到译文会独立成行显示（其原文行已从界面移除）。

即：清屏是**视图操作**，不是删除操作。

### 4.2 停止后自动总结

点「停止」后，程序会：

1. 结束采集、关闭录音、写完 MD 尾部统计；
2. 读取 MD 中的「逐句转写」正文（超长时按 `summary.max_chars` 截断）；
3. 调用 LLM 生成结构化 Markdown 总结（**独立线程，不卡住界面**，状态栏显示「● 总结中…」）；
4. 总结同时写入两处：`transcript.md` 末尾的「## 会议总结」章节，以及独立的 `summary.md`；
5. 弹出总结预览窗，可一键复制全文。

总结固定五段结构：**会议概要 / 核心要点 / 决议事项 / 待办事项 / 风险与关注点**。
提示词要求"不得臆造人名、日期、数字或决议"，缺失信息标注「（转写未明确）」。

**退出时同样会生成总结**：按 `Esc` 或关闭窗口时，程序会**同步等待总结落盘**再退出
（总结跑在守护线程里，若不等待会随进程一起被杀导致丢失）。

**总结失败会在 MD 里留痕**：若后端返回额度不足/限流/超时，MD 末尾会写入一段
「⚠️ 总结生成失败（时间）+ 原因」，转写正文不受影响。额度恢复后可一键补生成：

```bash
python summarize.py                    # 列出所有缺总结的会话
python summarize.py <会话目录名>        # 补生成指定会话
python summarize.py --all              # 批量补生成
```

**模型回退链**：`translate.fallback_models` 与 `summary.fallback_models` 分别列出翻译与总结
模型的尝试顺序，首选失败（余额不足/限流/渠道不可用/超时）自动降级到下一档，
并在字幕窗内以黄色提示降级过程。

实测（2026-10-22，`api.agnes-ai.cn`）：

| 模型 | 预扣 | 实测表现 |
|---|---|---|
| `agnes-2.5-flash` | 低 | ✅ 0.76–0.9s / 句，最稳定，**首选** |
| `agnes-3.0-flash` | 中 | ✅ 约 7s / 句，flash 超时时降级到它 |
| `agnes-2.5-pro` | 高 | ❌ 403 `insufficient_user_quota`（预扣 ￥0.0138 > 余额 ￥0.0126），链尾兜底 |

> ⚠️ **额度提示（重要）**：总结是"长输入 + 长输出"请求，预扣金额远高于逐句翻译。
> 翻译可能仍正常但总结失败——这是额度问题，不是程序缺陷。
> 本机该 key 的额度已接近耗尽，长期使用需**充值**或**更换 key**；
> 需要彻底避免外网消耗时，改用本地 Ollama（见 §6）。

## 6. 配置（config.toml）

| 键 | 默认 | 说明 |
|---|---|---|
| `audio.sample_rate` | 16000 | 采集采样率，模型要求 16 kHz |
| `audio.chunk_ms` | 100 | 每次读取帧长（越小延迟越低、CPU 略高） |
| `audio.save_audio` | true | 同时落盘 WAV 供事后校正（≈115 MB/小时） |
| `asr.model_dir` | 中文模型 | 英文会议改 `models/vosk-model-small-en-us-0.15` |
| `asr.source_lang` | zh | 会议发言语言，决定翻译提示词 |
| `asr.partial_interval` | 0.3 | 中间结果刷新间隔（秒） |
| `translate.enabled` | true | 关掉则仅转写不翻译 |
| `translate.backend` | openai_compatible | `none` = 不翻译 |
| `translate.base_url` | https://api.agnes-ai.cn/v1 | OpenAI 兼容端点；本地 Ollama 填 `http://localhost:11434/v1` |
| `translate.api_key_file` | secrets/llm_api_key.txt | 密钥文件路径（相对项目根），**优先于** `api_key` |
| `translate.api_key_env` | — | 从环境变量读密钥，优先于 `api_key` |
| `translate.api_key` | unused | 兜底：直接写 key（不推荐，密钥会进版本库） |
| `translate.model` | agnes-2.5-flash | 实测 0.76 s；本地模型建议 7B 级指令模型 |
| `translate.target_lang` | zh | 译文语言 |
| `translate.timeout` | 20.0 | 单次翻译超时（秒）——限流排队时别设太小 |
| `translate.fallback_models` | 见 config.toml | 翻译模型回退链（首选 → 备用档位） |
| `summary.enabled` | true | 停止后自动生成总结 |
| `summary.timeout` | 60.0 | 总结调用超时（秒，长文本需要更久） |
| `summary.max_chars` | 12000 | 送入总结的转写文本上限（超出截断，防超上下文） |
| `summary.language` | zh | 总结输出语言 |
| `summary.fallback_models` | 见 config.toml | 总结模型回退链（首选 → 备用档位） |
| `summary.system_prompt` | 见 config.toml | 总结结构与约束（禁止臆造等） |
| `ui.autostart` | false | 是否打开字幕窗即自动采集（默认**否**，须点「开始」） |
| `ui.opacity` | 0.92 | 字幕窗透明度 |
| `ui.font_size` | 13 | 字号 |

**密钥优先级**：`api_key_file` > `api_key_env` > `api_key`。本机默认走 `secrets/llm_api_key.txt`，改端点时同步换密钥文件即可，配置文件里不落明文密钥。

**语种切换**：当前配置为**英文会议 → 中文纪要**（`asr.model_dir` = 英文模型，`asr.source_lang` = `en`，`translate.target_lang` = `zh`）。中文会议改回 `models/vosk-model-small-cn` + `source_lang = "zh"` + `target_lang = "en"` 即可（config.toml 内已备好注释行）。

## 7. 接入翻译后端

**本机当前配置（已验证可用）**：云端 OpenAI 兼容端点 `https://api.agnes-ai.cn/v1`，模型 `agnes-2.5-pro`（实测 1.7 s 延迟），密钥在 `secrets/llm_api_key.txt`。

**本地 Ollama（音频与文本都不出本机）**

```bash
ollama pull qwen2.5:7b-instruct
ollama serve
```

改用本地时把 `config.toml` 里三行换成注释里的那组（`base_url` / `api_key` / `model`）。

**任意云端 API**

```toml
translate.base_url = "https://api.deepseek.com/v1"
translate.api_key_file = "secrets/deepseek_key.txt"
translate.model = "deepseek-chat"
```

**备用后端：火山方舟（免费额度兜底）**

主后端额度耗尽或被限流时，程序自动切到备用后端继续翻译，会议不中断。
`config.toml` 里已预置该段（`enabled = true`），**只要 Key 和模型都就绪即自动生效**。

开通步骤（约 3 分钟）：

1. 注册火山引擎并完成**实名认证**（个人即可，不实名无法创建 Key）；
2. 打开 <https://console.volcengine.com/ark> → 「API 密钥管理」→ 创建 API Key（形如 `ark-…`，**只显示一次**）；
3. 把 Key 存到 `secrets/ark_api_key.txt`（格式见 `secrets/ark_api_key.txt.example`）；
4. **左侧「开通管理」→ 开通模型**（这一步不能省）：每个模型开通即送 **50 万 token 免费推理额度**；
5. 扫描本账号真实可用的模型：

```bash
python scan_ark_all.py     # 全量扫描 137 条目录，并发真实调用判定「可用/需开通/已下线」
python check_ark.py --set  # 候选列表自检，并自动把可用模型写回 config.toml
```

> ⚠️ **关键点：方舟的 `GET /models` 返回的是全量模型目录**（137 条），
> 里面绝大多数**当前账号并未开通**。只看目录会误判"模型可用"，
> 实际调用会返回 `404 ModelNotOpen`。所以本程序的预检在拿到 `/models` 之后
> **必定补一次真实的 chat 探测**（`max_tokens=1`），以实际可调用性为准。

**本机实测（2026-10-22，账号 2115784484）**：

| 模型 | 实测结果 | 定位 |
|---|---|---|
| `deepseek-v4-1-flash-economy-261017` | 6/6 成功，翻译质量好（术语完整保留） | ✅ 首选 |
| `doubao-seed-2-1-lite-260915` | 6 次仅 2 次成功（方舟多节点开通未同步） | ⚠️ 次级 |
| 其余 135 个 | `ModelNotOpen` 或已下线 | — |

```toml
[[translate.alt_backends]]
name = "ark"
enabled = true
base_url = "https://ark.cn-beijing.volces.com/api/v3"
api_key_file = "secrets/ark_api_key.txt"
model = "deepseek-v4-1-flash-economy-261017"
fallback_models = ["deepseek-v4-1-flash-economy-261017", "doubao-seed-2-1-lite-260915"]
```

要点：

- 方舟是 **OpenAI 兼容**协议（`POST /api/v3/chat/completions`），无需改任何代码，只改配置；
- `model` 可填**模型 ID**（如上）或**推理接入点 ID**（`ep-…`），后者由「在线推理 → 创建推理接入点」获得；
- 官方另提供**安心体验模式**：只消耗赠送额度、接近耗尽即停服，**不会产生意外费用**；
- **开通模型属于管控面操作，需要 AK/SK 签名**，`ark-` 开头的推理 Key 做不了这件事，只能在控制台点；
- 部分模型开通后**方舟多节点状态未同步**，表现为时好时坏——这类模型放回退链次级位，不要放首选。

**容错机制（四级）**

1. **启动预检**：依次探测主后端与各备用后端（3 s 超时）。拿到 `/models` 后**必定补一次真实 chat 探测**（避免"目录里有但未开通"的假阳性）。全部不可用才**停用翻译并降级为纯转写**，绝不让识别主循环卡在超时上；
2. **运行期模型降级**：同一后端内首选模型失败（额度不足/限流），自动降到下一档模型；
3. **运行期跨服务商降级**：该后端模型链全部失败，自动切到备用后端（如豆包）；
4. **熔断与自愈**：某后端连续失败 2 次即被跳过（不让每句都白等一个超时）；主后端每 60 s 获准试一次，额度恢复后**自动切回**主后端。

> 失败计数**按后端独立统计**。备用后端（比如豆包还没开通，一直返回 404）
> **不会**把健康的主后端一起拖停——翻译只在所有后端都失败时才降级为纯转写。

字幕窗顶部徽标会实时显示「翻译已就绪 / 仅转写」，降级过程以黄色提示显示在字幕区内。

## 8. 测试

```bash
python tests/smoke_test.py         # 4/4：真实音频识别、翻译 HTTP、管线落盘、设备枚举
python tests/real_capture_test.py  # 真实放音 → 环录 → 识别（会发出测试语音）
python tests/ui_test.py            # 字幕窗渲染、事件分发与译文配对
python tests/feature_test.py       # 29/29：英→中配置、MD 结构、停止总结、清屏
python tests/exit_test.py          # 12/12：退出路径下总结不丢（含"不等待就退出"的反例对照）
python tests/backend_fallback_test.py  # 14/14：跨服务商后端回退 + 预检假阳性 + 熔断自愈
python tests/lifecycle_test.py     # 27/27：点击才采集、状态机、设备释放、可再次开始
python check_ark.py                # 方舟候选模型自检（--set 可写回配置）
python scan_ark_all.py             # 方舟全量扫描：137 条目录并发真实调用判定可用性
```

> 音频输出设备为独占资源：`real_capture_test.py` 须**单独运行**，若前一个 `run.py` 进程仍占用设备会导致捕获为空。
> 另注：测试会从扬声器播放约 8 秒英文语音，介意音量可先静音。

已验证结果（2026-10-22，本机）：

- 官方测试音频识别通过，输出与音频内容一致；
- 真实环录捕获 12.9 s 系统音频并成功产出识别文本；
- 字幕窗 900×200 置顶、双语分行、中间结果实时刷新均正常；
- 译文插入位置正确：即使译文**乱序返回**也能精确归位到对应原文句正下方；
- **GUI 模式真实验证**：播放测试音频 → 环录捕获 → 英文识别 → 中文翻译 → 原文译文成对落盘，全链路通过；
- **停止→总结闭环**：LLM 生成的五段式总结正确保留型号与标准号（EVE 261 kWh、Modbus TCP、SNMP、VDE-AR-N 4105），不确定信息标注「（转写未明确）」；
- **清屏语义验证**：清屏后界面为空、MD 字符数完全不变、清屏后的新记录仍继续写入 MD；
- **退出路径回归（12/12）**：`stop(wait_summary=True)`、`stop()+wait_for_summary()`、重复 stop、空会话均行为正确；并用"不等待直接退出"作反例对照，证明等待路径的必要性；
- **模型回退链实测**：首选模型因额度不足返回 403 时，自动降级到 `agnes-2.5-flash` 成功生成总结，降级过程在字幕窗内可见；
- **失败留痕**：总结失败时 MD 末尾写入「⚠️ 总结生成失败 + 原因」，转写正文不受影响，可用 `summarize.py` 补生成。

## 9. 已知限制

| 限制 | 影响 | 应对 |
|---|---|---|
| soundcard 偶发 `data discontinuity` 警告 | 极短音频断点，个别词可能丢失 | 不影响整体；关键内容以 `meeting.wav` 回溯重识别 |
| Vosk small 模型为小词表 | 生僻人名/术语准确率有限 | 专业术语走 `translate.system_prompt` 提示，或改用大模型做后处理校正 |
| 翻译为逐句异步 | 整句识别完成后才发起翻译（约 +1.7 s） | 已是独立线程，不阻塞下一句识别 |
| 环录抓的是**输出**设备 | 耳机/外放切换会改变捕获源 | `--list-devices` 查看当前环录对象 |
| 中文模型不含代码混说优化 | 中英混说时中文模型识别英文较弱 | 中英混合会议建议用英文模型 + 中文提示词 |
| 云端翻译需联网 | 断网时自动降级为纯转写 | 预检会提示「仅转写」；也可改用本地 Ollama |

## 10. 故障排查

| 现象 | 原因 | 处理 |
|---|---|---|
| `未找到 libvosk.dll` | DLL 未放置 | 确认 `models/lib/` 下 4 个 DLL 齐全 |
| `模型目录不存在` | 模型未解压或路径不符 | 检查 `config.toml` 的 `asr.model_dir` 与实际目录名一致 |
| `无法打开环录设备` | 无输出设备 / 设备被占用 | `--list-devices` 排查；先放一段音再启动 |
| 识别无输出 | 音频源未走系统输出（如仅网页静音） | 确认会议在播放声音；戴耳机时环录对象可能变化 |
| 翻译一直为空 | 后端未启动/模型名错/key 错 | 用 `curl` 试 `POST /v1/chat/completions`；日志中 `disabled_reason` 有原因 |
| 徽标显示「仅转写」 | 预检未通过 | 看启动日志 `[init] 翻译预检：` 后的原因；密钥缺失检查 `secrets/llm_api_key.txt` |
| `VoskError: 加载 libvosk.dll 失败` | DLL 依赖缺失 | 四个 DLL 必须在同一目录 |
| 窗口打开但无字幕 | 未点击「开始」 | **默认不自动采集**，点工具栏或中央的「▶ 开始」按钮 |
| 点了开始但没反应 | 系统无输出设备 / 设备被占用 | 看日志有无「音频采集失败」；`--list-devices` 排查 |
| 识别不出内容但有录音 | 环境噪音盖过语音 | 录到的 `meeting.wav` 若 RMS 远高于语音即为此因；换降噪耳机或调高音源音量 |

## 11. 合规

- 会议录音与转写**须经参会各方知情同意**；对外会议（客户澄清、投标答疑）建议开场声明用途；
- 音频不出本机；使用云端翻译时仅发送转写文本，请在 system prompt 中声明不得输出原文以外的敏感信息；
- `meeting.wav` 属会议原始录音，按公司信息安全与档案管理要求设定保存期限与访问权限。
