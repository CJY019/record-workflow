# 录音笔记 · Record Workflow

将课程、讲座或会议录音整理成带时间戳的转录稿、总结和问答记录。提供中文网页界面和命令行入口，面向个人本机使用。

```text
音频 / 视频
    ↓ FFmpeg 转为 16 kHz 单声道
    ↓ Silero VAD 检测与合并语音片段
    ↓ Qwen3-ASR 本地转录 + 可选 ForcedAligner 时间戳
    ↓ 可选 pyannote 说话人区分
    ↓ 大模型总结与多轮问答，可结合课件文字和页面图片
转录稿 / SRT 字幕 / Markdown 笔记 / 总结版本 / 问答记录
```

## 功能

- 批量上传录音，或将多个文件按顺序合并为一节课。
- 本地语音识别，支持中文、英文和自动语言检测。
- 按项目或课程管理任务，可关联本机文件夹。
- 导入 PDF、PPT、Word、图片和文本资料，为总结与问答提供上下文。
- 项目资料库：自动收录项目文件夹里的既往资料与同项目其他录音的转录稿、笔记，问答和总结时按相关度检索片段参考并注明出处（配置项 `library`）。
- 切换大模型、总结模板，保存多个总结版本和独立问答对话。
- 按处理阶段重跑，支持仅重新总结已有转录稿。
- 手机和平板适配、独立后台上传、分片重试和上传进度。
- 本地转录与远程总结分别排队，总结支持限流并发和模型自动回退。
- 本地 Markdown 与公式渲染依赖，支持多行公式和实时问答渲染。

版本变化见 [更新记录](CHANGELOG.md)，开发、测试和发布流程见 [贡献指南](CONTRIBUTING.md)。

## 环境准备

以下命令在 **Windows PowerShell** 中执行。本项目在 Windows、Python 3.12 和 NVIDIA CUDA 环境验证。

| 依赖 | 用途 |
| --- | --- |
| Git | 获取源码 |
| uv | 创建 Python 环境、安装依赖 |
| FFmpeg | 音频 / 视频解码，需加入 PATH |
| NVIDIA 显卡与驱动 | 加速转录。当前默认使用 CUDA，可配置 CPU |
| LibreOffice，可选 | 将 Office 课件转换为 PDF |
| 大模型 API 凭据，可选 | 总结、问答和自动对话标题 |
| Hugging Face token，可选 | 使用需要授权的说话人区分模型 |

安装 uv 后重新打开 PowerShell。FFmpeg 安装后运行 `ffmpeg -version` 确认可用。Office 课件功能需要安装 LibreOffice，并保证 `soffice` 可在 PATH 中找到，或在本地配置里填写路径。[uv 安装文档](https://docs.astral.sh/uv/getting-started/installation/)、[FFmpeg 下载](https://ffmpeg.org/download.html)、[LibreOffice 下载](https://www.libreoffice.org/download/download-libreoffice/)

## 快速开始

### 1. 获取源码与安装依赖

```powershell
git clone https://github.com/CJY019/record-workflow.git
cd record-workflow
uv venv --python 3.12

# NVIDIA CUDA 12.8，先安装与已验证环境一致的 PyTorch
uv pip install torch==2.11.0 torchaudio==2.11.0 --index-url https://download.pytorch.org/whl/cu128
uv pip install -r requirements.txt

.venv\Scripts\python.exe -c "import torch; print(torch.__version__); print('CUDA available', torch.cuda.is_available())"
```

CPU 用户将上面的 PyTorch 安装命令替换为下面这条，并在本地配置中设置 `device: cpu`。其他硬件对应的安装命令可从 PyTorch 官方页面选择。[PyTorch 安装说明](https://pytorch.org/get-started/previous-versions/)

```powershell
uv pip install torch==2.11.0 torchaudio==2.11.0 --index-url https://download.pytorch.org/whl/cpu
```

### 2. 创建本地配置

```powershell
Copy-Item .env.example .env
Copy-Item config.yaml config.local.yaml
```

程序优先读取完整的 `config.local.yaml`，文件不存在时读取 `config.yaml`。本地配置是整份配置，请从示例复制后编辑。修改配置或 `.env` 后重启服务。

仅做本地转录时，可直接使用下方命令行示例的 `--no-summary`。

需要总结与问答时，完成以下配置。

1. 编辑 `.env`，填写自己的 `OPENAI_API_KEY`。连接兼容服务时，将 `OPENAI_BASE_URL` 改为服务商提供的 API 根地址。
2. 编辑 `config.local.yaml`，将 `llm.providers.openai.models` 中的 `your-model-id` 替换为该账号实际可用的模型 ID，并修改对应显示名称。
3. 将 `summary.model`、`summary.chat_model` 和 `summary.title_model` 都改为 `openai:实际模型ID`。
4. 兼容服务仅支持 Chat Completions 时，将 `wire_api` 改为 `chat`。使用纯文字模型时，将 `vision` 改为 `false`。

例如，假设服务商提供的模型 ID 为 `my-model`，则在复制的配置中修改这些字段，其余字段继续使用复制的值。

```yaml
llm:
  providers:
    openai:
      label: My Provider
      format: openai
      wire_api: responses
      base_url_env: OPENAI_BASE_URL
      key_env: OPENAI_API_KEY
      max_tokens: 16000
      vision: true
      models:
        my-model: My Model

summary:
  model: openai:my-model
  chat_model: openai:my-model
  title_model: openai:my-model
```

OpenAI 配置使用 Responses API。兼容服务的接口模式、模型 ID、输出长度与图片能力以服务商文档为准。[OpenAI 官方接口说明](https://developers.openai.com/api/docs/guides/migrate-to-responses)

### 3. 启动网页

```powershell
.venv\Scripts\python.exe launcher.py
```

启动器会打开浏览器，默认访问地址为 `http://127.0.0.1:8000`。关闭控制台窗口或按 `Ctrl+C` 可停止服务。也可以使用下面的命令直接启动服务。

```powershell
.venv\Scripts\python.exe -m app.main
```

### 4. 处理第一份录音

1. 在网页中新建项目，例如「示例课程」。
2. 上传自己有权处理的录音。多个文件可以分别处理，也可以选择合并。
3. 填写课程名、专业术语等背景信息，选择语言和总结模板。
4. 按需上传课件。网页处理会生成总结，需先完成 API 配置。
5. 等待处理完成，查看转录稿、总结与问答页面，下载 Markdown 或字幕文件。

首次转录会通过 ModelScope 下载 Qwen3-ASR 和对齐模型到 `models/`，需要联网并预留数 GB 空间。后续可复用下载的模型。默认使用 1.7B 转录模型与 0.6B 对齐器，显存不足时可按下方说明调整。

上传开始后，每份任务固定自己的录音、课件、项目和设置。可以通过「另建任务」继续上传其他录音，侧栏的「上传任务」可查看各自进度；切换应用内页面不会取消上传。上传仍依赖当前浏览器页面，刷新或关闭页面会提示确认。

网页任务使用两条队列：本地转码、切段、转录和说话人区分依次执行，远程总结默认同时处理两份（`pipeline.summary_workers`）。前一份录音进入总结后，下一份即可开始本地处理。页面分别显示转录、总结的排队位置与当前使用的模型。重启后按已写入的阶段检查点恢复；仅重新总结直接进入总结队列。

## 日常使用

### 命令行

```powershell
# 仅做本地转录
.venv\Scripts\python.exe cli.py "lecture.m4a" --no-summary -o out

# 生成课程笔记，补充背景信息
.venv\Scripts\python.exe cli.py "lecture.m4a" --context "示例课程 第三讲" --prompt lecture -o out

# 多个文件按文件名顺序合并，自动检测语言
.venv\Scripts\python.exe cli.py "part-01.m4a" "part-02.m4a" --merge --language auto

# 会议纪要，区分两位说话人
.venv\Scripts\python.exe cli.py "meeting.mp3" --diarize --num-speakers 2 --prompt meeting

# 对已有任务重新总结，将 JOB_ID 替换为运行时输出的任务 ID
.venv\Scripts\python.exe cli.py --rerun JOB_ID --prompt brief

# 从已有任务的 ASR 阶段开始重跑
.venv\Scripts\python.exe cli.py --rerun JOB_ID --from-stage asr --no-summary
```

默认每个输入文件对应一个任务。`-o out` 将转录 Markdown 与已生成的总结复制到 `out/`，完整产物保存在任务目录。运行 `cli.py --help` 查看全部参数。

### 项目文件夹与输出

在项目菜单中选择「关联项目文件夹」，每份录音会在所选目录下获得独立子文件夹。默认任务位于 `data/jobs/<job_id>/`，`data/registry.json` 保存位置索引。

```text
任务目录/
├── 原始录音/
├── 课件资料/
├── 转录稿/
│   ├── 转录稿.md
│   ├── 转录稿.txt
│   └── 字幕.srt
├── 总结/             各个总结版本
├── 问答记录/         每个对话对应一个 Markdown 文件
├── 笔记.md           当前启用的总结
└── .work/            元数据、处理缓存、JSON 转录等内部文件
```

项目菜单可重命名、移动任务或关联目录。移动任务前需等待处理完成。处理失败后，可在网页选择相应阶段重跑。

### 总结模板与模型

内置 `lecture` 课程笔记、`meeting` 会议纪要、`brief` 简要总结。在 `app/prompts/` 新建 Markdown 文件即可增加模板。

在 `llm.providers` 下增加服务商，在其 `models` 下增加模型。模型选择值统一采用 `服务商ID:模型ID`。网页会尝试检测模型可用性，部分兼容服务未提供模型列表接口时仍可手动选择。

在 `summary.fallback_models` 中按顺序填写备用模型 ID。默认模型调用失败、正文首字超时（`first_token_timeout_sec`）、正文停顿超时（`idle_timeout_sec`）或整次调用超时（`attempt_timeout_sec`）后，会关闭该次连接并切换到下一个模型；失败的半篇输出不会保存。SDK 不额外重复重试同一模型。手动选择后备链中的模型时从该位置开始，选择其他模型时会先尝试所选模型，再使用默认后备链。长转录已完成的分段笔记会保留，后续分段继续使用成功的后备模型。总结版本记录实际生成模型。

Markdown 和公式渲染依赖随程序提供，平板无需额外访问 CDN。支持 `\(...\)`、`\[...\]`、`$...$`、`$$...$$`；代码块中的公式示例保留原文。

使用 Anthropic 时，在 `.env` 填写 `ANTHROPIC_API_KEY`，在本地配置的 `anthropic.models` 中填写真实模型 ID，并将三个 `summary` 模型字段设为 `anthropic:实际模型ID`。接入使用 Bearer token 的 Messages 兼容服务时，可将其 `key_env` 指向自己定义的、以 `AUTH_TOKEN` 结尾的环境变量。

通过 CC Switch 管理 Claude 配置时，可给对应服务商设置 `credentials_from: claude_settings`。程序仅在当前 Claude 配置与该服务商地址一致时读取最新密钥；切换到其他服务商不会替换原服务商的凭据。密钥变化会更新 API 客户端和模型可用性缓存。首次连接或更换地址时仍需同步该服务商的 `base_url` 和保存的环境变量。

### 说话人区分

1. 在模型页面申请访问并接受使用条件。
2. 创建有该模型读取权限的 Hugging Face token，填写到 `.env` 的 `HF_TOKEN`。
3. 在网页开启说话人区分，或在 CLI 使用 `--diarize`。已知人数时可增加 `--num-speakers`。

模型与 token 管理入口见 Hugging Face。[模型页面](https://huggingface.co/pyannote/speaker-diarization-community-1)、[Token 设置](https://huggingface.co/settings/tokens)

### 显存与 CPU 配置

在 `config.local.yaml` 中可调整以下字段。

| 字段 | 调整方式 |
| --- | --- |
| `asr.model` | 改为 `Qwen/Qwen3-ASR-0.6B` 降低资源占用 |
| `asr.batch_size` | 改为 `1` 降低批处理显存占用 |
| `asr.aligner` | 设为 `null`，使用片段级时间戳 |
| `unload_after_use` | 设为 `true`，每阶段结束后释放显存 |
| `device` | 设为 `cpu` 使用 CPU，推理速度取决于硬件 |
| `materials.soffice` | 填写本机 LibreOffice 可执行文件路径 |

### 手机 / 平板访问（经自己的 VPS 中转）

启动器可开一条 SSH 反向隧道，把本机服务映射到 VPS 的回环端口，再由 VPS 上的 nginx 以 HTTPS 和密码对外提供。手机和平板只需浏览器，不必安装 VPN。关闭启动器即断开隧道。

1. 在 `config.local.yaml` 的 `relay` 中填写 `enabled: true`、VPS 的 SSH 登录、私钥路径和对外地址。私钥需能免密登录 VPS。
2. 在 VPS 上配置 nginx，示例如下。`proxy_set_header X-Notes-Relay 1` 用于告诉程序请求来自远程设备，远程设备无法使用打开文件夹等电脑端操作。

```nginx
server {
    listen 8448 ssl;
    server_name your-domain;
    ssl_certificate     /etc/letsencrypt/live/your-domain/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/your-domain/privkey.pem;
    auth_basic "notes";
    auth_basic_user_file /etc/nginx/notes.htpasswd;
    client_max_body_size 2g;
    location / {
        proxy_pass http://127.0.0.1:18765;
        proxy_http_version 1.1;
        proxy_set_header Host $http_host;
        proxy_set_header X-Notes-Relay 1;
        proxy_buffering off;
        proxy_request_buffering off;
        proxy_read_timeout 3600s;
        proxy_send_timeout 3600s;
    }
}
```

3. 用 `htpasswd` 或 `openssl passwd -apr1` 生成密码文件，放行防火墙端口后重载 nginx。

## HTTP API

服务启动后可在 `http://127.0.0.1:8000/docs` 查看交互式 API 文档。

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| GET | `/info` | 模型、模板与默认配置 |
| GET | `/models` | 大模型列表与可用性 |
| GET | `/queue` | 本地处理和远程总结的排队、执行数量 |
| POST | `/jobs` | multipart 上传 `file`，可传多个文件 |
| GET | `/jobs` | 任务列表 |
| GET | `/jobs/{id}` | 状态、阶段与进度 |
| GET | `/jobs/{id}/result` | JSON 转录与总结 |
| GET | `/jobs/{id}/files/{name}` | 下载 `transcript.md/.srt/.txt/.json` 或 `summary.md` |
| POST | `/jobs/{id}/rerun` | JSON 指定 `from_stage` 与模型等参数 |
| GET/POST | `/jobs/{id}/chats` | 列出或创建问答对话 |
| GET | `/jobs/{id}/chats/{cid}` | 读取对话 |
| POST | `/jobs/{id}/chats/{cid}/messages` | JSON `message` 与可选 `model`，流式返回答案 |
| GET | `/jobs/{id}/summaries` | 总结版本列表 |
| POST | `/jobs/{id}/materials` | 上传课件 |
| GET/POST | `/projects` | 列出或创建项目 |
| POST | `/summarize` | 对 JSON `text` 指定的文本生成总结 |

Windows PowerShell 中使用 `curl.exe` 调用。任务创建后，将命令中的 `JOB_ID` 替换为返回的 `id`。

```powershell
curl.exe -F "file=@lecture.m4a" -F "summarize=false" http://127.0.0.1:8000/jobs
curl.exe http://127.0.0.1:8000/jobs/JOB_ID
curl.exe http://127.0.0.1:8000/jobs/JOB_ID/files/transcript.md -o transcript.md
```

## 数据处理与提交检查

原始录音、转码音频和语音转录在本机处理。启用总结或问答时，程序会将转录文字、提问，以及相关课件文字发送至自己配置的大模型服务商。开启 `vision` 时还会发送抽样后的课件页面图片。首次下载模型需要网络访问；网页的 Markdown、公式脚本、样式和字体随程序提供。

应用默认监听回环地址 `127.0.0.1`，提供操作本机项目文件夹的接口，适用于个人电脑上的本机访问。

`.env`、`config.local.yaml`、录音、课件、`data/`、`models/`、`samples/`、Python 环境和缓存已配置为 Git 忽略项。分享项目时请使用 Git 仓库源码或 GitHub 源码下载功能。

提交前运行以下检查。脚本读取 Git 暂存区内容，检查常见凭据格式、私人文件路径和大文件，输出文件名与行号。GitHub Actions 会在推送与 Pull Request 时执行同一检查、Python 语法检查、离线单元测试和浏览器测试。

```powershell
git add app cli.py launcher.py config.yaml .env.example .gitignore requirements.txt requirements-test.txt uv.toml README.md CHANGELOG.md CONTRIBUTING.md LICENSE scripts tests .github
.venv\Scripts\python.exe scripts/check_public_source.py
git diff --cached --stat
```

## 更新已安装的程序

等待当前上传和处理完成后停止服务，在项目目录执行：

```powershell
git switch main
git pull --ff-only
uv pip install -r requirements.txt
.venv\Scripts\python.exe launcher.py
```

本地 `.env`、`config.local.yaml` 和数据目录由 Git 忽略。新版增加的可选配置请参考 `config.yaml`，合并需要的字段到自己的本地配置。

## 常见问题

| 现象 | 处理方式 |
| --- | --- |
| 找不到 FFmpeg | 确认 `ffmpeg -version` 成功，再从新开的终端启动 |
| CUDA 不可用 | 检查 NVIDIA 驱动、PyTorch 安装源与 `torch.cuda.is_available()` |
| 显存不足 | 先将 `batch_size` 设为 `1`，再考虑 0.6B 模型或关闭对齐器 |
| 模型下载失败 | 检查网络、磁盘空间与模型仓库权限，可通过 `HF_ENDPOINT` 指定可信镜像 |
| 总结提示缺少 key | 检查当前配置的 `key_env` 与 `.env` 变量名，修改后重启 |
| 模型调用出现 404 | 检查服务地址、真实模型 ID 和 `wire_api` 模式 |
| Office 课件转换失败 | 检查 LibreOffice 安装与 `materials.soffice` 路径 |
| 已有转录、总结失败 | 修复 API 配置后，从 `summarize` 阶段重跑 |
| 端口被占用 | 访问已有服务，或在本地配置中更改 `server.port` |

## 许可证与第三方组件

本仓库自有源码采用 MIT 许可证，详见 `LICENSE`。模型权重和第三方组件适用各自许可证与使用条件。

本地打包的 Marked 和 KaTeX 保留各自的 MIT 许可证。版本、包来源和摘要记录在 `app/static/vendor/sources.json`，对应许可证与资源放在各自的版本目录中。

Qwen3-ASR 与 ForcedAligner 的模型卡标注 Apache-2.0。PyMuPDF 提供 AGPL 与商业许可，分发包含该组件的应用时应遵循相应条款。[Qwen3-ASR](https://github.com/Qwen/Qwen3-ASR)、[PyMuPDF 许可说明](https://pymupdf.readthedocs.io/en/latest/about.html)
