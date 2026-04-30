# VerPadProxy（基于 mitmproxy 的个人媒体中枢）

<div align="center">

**在受限网络环境里，把「可配置的 HTTP 入口」升级为可控的本地阅览台**

*面向具备 Wi‑Fi 手动代理能力的终端（例如部分 **受限的教育平板**），由 **安卓宿主设备**（Termux / root 可选）承载 mitmproxy Addon，将指定主机流量改写为本仓库提供的 Web UI。*

</div>

---

## 目录

- [一句话定位](#一句话定位)
- [免责声明与合规边界](#免责声明与合规边界)
- [痛点与适用画像](#痛点与适用画像)
- [系统全景：概念拓扑](#系统全景概念拓扑)
- [请求链路：从浏览器到本地文件](#请求链路从浏览器到本地文件)
- [PDF：多级渲染兜底流水线](#pdf多级渲染兜底流水线)
- [视频：兼容性探测 → HLS 实时转码 → 进度条对齐](#视频兼容性探测--hls-实时转码--进度条对齐)
- [字幕：内封 / 外挂协同策略](#字幕内封--外挂协同策略)
- [模块与仓库地图](#模块与仓库地图)
- [快速开始 · 桌面侧（Windows）](#快速开始--桌面侧windows)
- [快速开始 · 安卓宿主（Termux）](#快速开始--安卓宿主termux)
- [环境变量参考](#环境变量参考)
- [媒体目录约定](#媒体目录约定)
- [能力矩阵（特性开关）](#能力矩阵特性开关)
- [延伸阅读](#延伸阅读)

---

## 一句话定位

本项目是一套 **HTTP(S) 透明改写层**：在客户端已配置 **HTTP 代理** 的前提下，将被劫持域名下的浏览会话，替换为自建的 **PDF / 音视频 / 图文阅览与上传管理界面**，媒体实体存放在宿主文件系统的 `payload/` 树中，由 `redirect_addon.py` 动态编排响应。

---

## 免责声明与合规边界

- 本项目 **仅供学习研究与个人合规自用**。
- **禁止**用于规避授权控制、渗透测试未授权目标、干扰考试或教务系统、以及任何违反当地法律法规与机构规章的行为。
- 仓库内默认主机名、密码仅为占位演示；**上线前务必替换**为你的环境与强口令。
- 因使用本项目产生的后果由使用者自行承担，贡献者与维护者不承担连带责任。

---

## 痛点与适用画像

| 维度 | 说明 |
|------|------|
| **客户端约束** | 部分 **受限的教育平板** 仅开放浏览器与有限的网络策略，但往往允许配置 **Wi‑Fi 手动 HTTP 代理**。 |
| **宿主侧能力** | **安卓设备** 可通过 Termux 运行 Python 生态与 `mitmdump`；若具备 root，还可辅助热点网关诊断与持久化脚本（可选）。 |
| **体验目标** | 在同一「可被允许的入口」内完成：PDF 分页阅览、视频（含高阶编码）可观可看、音乐与封面、图片与文本、上传下载与权限细分。 |

---

## 系统全景：概念拓扑

下列示意图描述 **两种典型组网**：热点直连（安卓设备作接入点）与局域网共存（平板与安卓设备在同一上游 LAN）。端口以默认 `2345` 为例。

```mermaid
flowchart TB
  subgraph 客户端侧["客户端侧（示例：受限的教育平板）"]
    B["系统浏览器 / WebView"]
    PxyCfg["Wi‑Fi：手动 HTTP 代理"]
    B --- PxyCfg
  end

  subgraph 无线接入["无线接入层"]
    AP["安卓设备 · 个人热点\n(逻辑接口常见 ap0 / wlan 协同)"]
    LAN["上游局域网 AP\n(安卓设备与平板同网段场景)"]
  end

  subgraph 宿主运行时["安卓宿主 · Termux 运行时"]
    MD["mitmdump\n监听 0.0.0.0:2345"]
    ADD["redirect_addon.py\n(+ user_auth.py)"]
    FS["payload/\nPDF · 视频 · 音乐 · …"]
    MD --> ADD --> FS
  end

  PxyCfg -->|"HTTP 流量走代理"| MD
  B -->|"关联至热点 SSID"| AP
  B -->|"或关联至同一 LAN"| LAN
  AP --- MD
  LAN --- MD

  style 客户端侧 fill:#1a1a2e,color:#eee
  style 宿主运行时 fill:#16213e,color:#eee
  style 无线接入 fill:#0f3460,color:#eee
```

**读图要点**

- 平板 **不需要**安装本项目；它只需 **信任代理路径** 并把 HTTP 会话指向 **安卓设备上 mitmdump 正在监听的地址:端口**。
- HTTPS 若也要进入 Addon 逻辑，需要客户端侧 **导入 mitmproxy CA**（涉及额外信任模型，生产环境请谨慎评估）。

---

## 请求链路：从浏览器到本地文件

```mermaid
sequenceDiagram
  participant 平板浏览器 as 平板浏览器
  participant mitmdump as mitmdump :2345
  participant Addon as redirect_addon
  participant Auth as user_auth
  participant FS as 本地 payload/

  平板浏览器->>mitmdump: GET http://配置的目标主机/...
  mitmdump->>Addon: HTTP 流触发 Addon hook
  Addon->>Addon: 匹配 MITM_REDIRECT_HOSTS 策略
  alt 需要登录且未登录
    Addon->>Auth: 校验会话 / 跳转登录页
    Auth-->>Addon: 放行或拒绝
  end
  Addon->>FS: 映射 URL → 实体文件或虚拟播放列表
  FS-->>Addon: 字节流 / 元数据
  Addon-->>mitmdump: 200 + HTML / HLS / PNG / …
  mitmdump-->>平板浏览器: 改写后的响应体
```

---

## PDF：多级渲染兜底流水线

目标是在 **PyMuPDF 不可用或编译失败**（常见于部分 ARM Termux 场景）时，仍能提供可读体验：`poppler` / `mupdf-tools` / 浏览器端 `pdf.js` 形成降级梯队。

```mermaid
flowchart LR
  A["PDF 请求到达"] --> B{"PyMuPDF\n可用？"}
  B -->|是| C["进程内栅格化缓存"]
  B -->|否| D{"pdftoppm\n(poppler)"}
  D -->|可用| E["外部管线渲染 PNG"]
  D -->|否| F{"mutool draw\n(mupdf-tools)"}
  F -->|可用| G["MuPDF 栅格化 PNG"]
  F -->|否| H["pdf.js 前端渲染\n(/assets/pdfjs)"]

  C --> Z["分页输出至浏览器"]
  E --> Z
  G --> Z
  H --> Z

  style H fill:#533483,color:#fff
```

---

## 视频：兼容性探测 → HLS 实时转码 → 进度条对齐

对浏览器原生 `<video>` **不友好** 的组合（如 **HEVC Main10 / MKV 封装 / 非常规格式**），服务端侧使用 **ffmpeg** 进行 **实时 HLS（m3u8 + ts）** 输出；前端采用 **hls.js** 播放。  
为实现「整片时长」级别的拖拽体验，Addon 侧维护 **虚拟全长播放列表**、**分段生成状态** 与 **跳转（seek）时重启转码起点** 等协作逻辑（详见 `redirect_addon.py` 中 HLS / transcode 相关路由）。

```mermaid
flowchart TD
  V1["视频页面请求"] --> V2["ffprobe：容器/编码探测"]
  V2 --> V3{"原生播放兼容？"}
  V3 -->|是| V4["直连文件或简单响应"]
  V3 -->|否| V5["启动 / 续跑 ffmpeg → HLS"]
  V5 --> V6["浏览器 hls.js 拉流"]
  V6 --> V7{"用户拖拽进度条"}
  V7 --> V8["video_trans_jump：按分段对齐重启转码窗口"]
  V8 --> V6
  V6 --> V9["播放结束：可选清理缓存目录"]

  style V5 fill:#e94560,color:#fff
  style V8 fill:#533483,color:#fff
```

---

## 字幕：内封 / 外挂协同策略

```mermaid
flowchart TB
  S0["字幕需求"] --> S1{"外挂字幕文件\n(.srt / .vtt 等)"}
  S1 -->|存在| S2["以 track 方式并联挂载"]
  S1 -->|不存在| S3["ffprobe 枚举内封轨道"]
  S3 --> S4{"文本型字幕"}
  S4 -->|是| S5["抽取并转为 WebVTT\n(/subtitle_internal…)"]
  S4 -->|否\n如 PGS 位图| S6["转码管线内嵌 burn-in\n叠加至视频帧"]

  S2 --> OK["播放器呈现"]
  S5 --> OK
  S6 --> OK

  style S6 fill:#0f3460,color:#fff
```

---

## 模块与仓库地图

| 组件 | 职责 |
|------|------|
| `redirect_addon.py` | mitmproxy Addon：路由聚合、静态资源、`/hls/*`、PDF/视频/音频页面组装 |
| `user_auth.py` | 账户体系、登录握手、特性权限枚举 |
| `assets/pdfjs/` | 离线 pdf.js 运行时（终极兜底） |
| `assets/hlsjs/` | 离线 hls.js（HLS 播放） |
| `termux/*.sh` | 一键环境、`verpadproxy.sh` 运维入口、守护进程脚本等 |
| `payload/` | 媒体内容树（仓库层仅占位 `.gitkeep`，不自携带版权素材） |

---

## 快速开始 · 桌面侧（Windows）

**前置**：Python 3.10+、`mitmproxy`；PDF 体验建议安装 `pymupdf`。

```powershell
pip install mitmproxy pymupdf

mkdir payload\PDF, payload\视频, payload\音乐, payload\private, payload\upl
# 将你的电子书、影片、音频放入对应目录

$env:MITM_REDIRECT_HOSTS = "example.com:8080"
$env:MITM_SHARE_DIR      = "$PWD\payload"
$env:MITM_DATA_DIR       = "$PWD"
mitmdump -s .\redirect_addon.py --listen-host 0.0.0.0 --listen-port 2345
```

在 **受限的教育平板** 上：Wi‑Fi → 手动代理 → **主机填运行 mitmdump 的机器在其网络上的 IPv4** → **端口 2345**。浏览器访问你在 `MITM_REDIRECT_HOSTS` 中配置的 **HTTP** 入口主机名，应落到本仓库登录与首页流程。

---

## 快速开始 · 安卓宿主（Termux）

**前置**：在 **安卓设备** 安装 Termux（推荐 F-Droid 构建）；授予存储访问权限；逐步说明见 [`termux/README_TERMUX.md`](termux/README_TERMUX.md)。

推荐将工程置于共享存储下的 `VerPadProxy/`，首次执行：

```bash
termux-setup-storage
cd ~/storage/shared/VerPadProxy/scripts/termux
bash setup.sh
```

日常启动（示例）：

```bash
bash ~/storage/shared/VerPadProxy/scripts/termux/start.sh
```

若你已验证 **root 路径下启动更稳定**，可参考仓库内 `start_same_as_pc_root.sh` 或 `verpadproxy.sh root-start`（以你设备实测为准）。

**一键查看「热点场景 / 局域网场景」代理应填写的宿主 IPv4：**

```bash
bash /sdcard/VerPadProxy/scripts/termux/verpadproxy.sh info
```

**强烈建议安装的增强依赖（按需）：**

```bash
pkg install -y mupdf-tools poppler ffmpeg
```

---

## 环境变量参考

| 变量 | 含义 |
|------|------|
| `MITM_REDIRECT_HOSTS` | 命中劫持的主机列表（逗号分隔，可含端口）。占位示例：`example.com:8080`。滥用 `*` 将扩大暴露面 |
| `MITM_SHARE_DIR` | 媒体根目录（内含 `PDF/`、`视频/`、`音乐/`、`private/`、`upl/`） |
| `MITM_DATA_DIR` | 用户库、日志等运行期数据目录 |
| `MITM_USERS_FILE` | 用户 JSON 路径（可指向 `$MITM_DATA_DIR` 下） |
| `MITM_BOOTSTRAP_PASSWORD` | **首次**创建 `admin` 时使用的引导口令；生产环境务必显式设定 |
| `MITM_LOG_QUIET` | `1` 时压缩控制台噪声 |
| `MITM_PYMUPDF_PYTHON` | 指定用于 PyMuPDF 子进程的 Python 路径（高级） |

---

## 媒体目录约定

```text
payload/
├── PDF/       # 电子书 PDF
├── 视频/      # 影片；外挂字幕可与视频同目录并按约定文件名匹配
├── 音乐/      # 音频；可选 cover.jpg 作为封面
├── private/   # 私密分区（受权限模型保护）
└── upl/       # 上传落盘目录（受权限模型保护）
```

---

## 能力矩阵（特性开关）

管理员可为账户配置细粒度开关（示例枚举）：`fe_pdf`、`fe_video`、`fe_music`、`fe_upload`、`fe_browse`、`fe_private`、`fe_upl` 等——**首页入口与路径 ACL 解耦**，避免出现「关闭浏览导致 PDF/视频入口一并消失」这类逻辑缠绕（实现细节见 `redirect_addon.py` 与 `user_auth.py`）。

---

## 延伸阅读

- Termux 细化步骤、热点 CA、自启与排障：[`termux/README_TERMUX.md`](termux/README_TERMUX.md)
- 许可证：[LICENSE](LICENSE)

---

<div align="center">

**VerPadProxy — 把许可范围内的本地阅读体验，收敛成清晰、可审计的一条 HTTP 代理链路**

</div>
