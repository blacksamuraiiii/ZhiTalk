# 固话智连（智话通）

> 传统座机 + FreePBX + AI 语音助手 | OPC息壤杯参赛项目
> 分支：`demo/tftpd64-standalone`（单机演示，脱离路由器）

## 项目简介

将传统固定电话接入 AI 语音助手，实现智能对话、自动外呼、联网查询（天气/股价/资讯）等功能。通过 FreePBX + AudioSocket 协议桥接 SIP 通话与 Python AI 后端，集成 ASR（语音转文字）、LLM（大语言模型）、TTS（语音合成）全链路。

本分支为**单机演示场景**：Win11 笔记本 + ATA190 + 一根网线，tftpd64 同时提供 DHCP 和 TFTP，无需局域网路由器。

> 主分支（`main`）使用 Docker TFTP 容器，适用于局域网 WiFi 部署。详见 main 分支 README。

## 架构

```
ATA190 (192.168.2.100) ←→ FreePBX (Docker) ←→ AI后端 (Docker)
 座机（分机101）         SIP 5060            AudioSocket 9090
  DHCP + TFTP            AMI 5038             Flask 8080
    ↕                                       ↕
  tftpd64 (Windows) ←────────────────── 中屏管理:8080/Freepbx配置
  UDP 67/68/69
  192.168.2.1 (有线网卡)
```

详细架构拓扑图：`docs/固话智联-架构拓扑.html`

## 通话流程

- 正常通话：用户拨 9 → FreePBX 接通 → AudioSocket 连接 AI → AI 开场白 → 人机对话 → 联网搜索（天气/股价）→ 语气词(≤3字)过滤 → 再见/拜拜自动挂机
- LLM 超时回拨：用户说完 5 秒无首字 → 播"好的请稍等"；15 秒无回复 → 播回电语 → 挂机 → AMI 外呼
- 定时任务外拨：APScheduler 触发 → 联网搜索 → LLM 生成 → TTS → AMI Originate 外呼
- 空回复兜底：LLM 返回空内容 → 播"这个问题我暂时无法回答，抱歉"

详细通话流程图：`docs/固话智联-通话流程.html`

## 技术栈

| 组件 | 选型 | 说明 |
|------|------|------|
| ASR | sherpa-onnx 流式 zipformer (int8) | 本地离线，端点检测，免外部服务 |
| TTS | matcha-zh-en-8k（默认）+ edge | 本地离线 + 在线双引擎池 |
| VAD | ten-vad (int8) | sherpa-onnx 自带，2.7MB |
| LLM | 本地 Qwen3.5-4B-MTP-GGUF（llama.cpp WSL）/ DeepSeek 在线可切换 | 本地离线默认，MTP 投机解码 ~48 tok/s，数据不出本机 |
| 联网搜索 | AnySearch MCP 垂直域路由 | weather/general 直搜 + 口语过滤词可配置 |
| 指标采集 | SQLite（call_records + call_logs） | 聚合 KPI + 日志持久化 |
| SIP | FreePBX 17 (Asterisk 21 + PJSIP) | escomputers/freepbx:17-nofail2ban |
| 音频桥 | AudioSocket (TCP 9090) | 1byte type + 2byte len + 320byte slin16 |
| AMI | pyst2 | 外呼控制、状态查询、回拨 |
| Web | Flask | 中屏管理（8 页面 + 20+ API） |
| 定时任务 | APScheduler | 每天/每周(复选)/一次性/间隔 |
| DHCP+TFTP | tftpd64 (Windows) | 单机演示，替代 Docker TFTP 容器 |

## 目录结构

```
202608ZhiTalk/
├── docker-compose.yml          # 双容器编排（FreePBX + AI 后端）
├── Dockerfile                  # AI 后端镜像（models 挂载不打包）
├── config.yaml                 # 运行配置（含 host_ip/ata_ip 可编辑）
├── requirements.txt            # Python 依赖
├── run-llama-server.sh          # WSL 本地 llama-server 启动（--daemon 后台 + 日志落盘）
├── README.md                   # 本文件
├── app/
│   ├── engine/                 # ASR/TTS/LLM/AudioSocket/搜索/指标 引擎
│   │   ├── audiosocket.py      # AudioSocket 协议服务器（三协程 FIFO + 空回复兜底）
│   │   ├── asr.py              # sherpa-onnx 流式 ASR（zipformer）
│   │   ├── tts.py / tts_edge.py # matcha + edge 双引擎
│   │   ├── llm.py              # OpenAI 兼容 LLM（thinking 已禁用）
│   │   ├── search.py           # AnySearch MCP 垂直域路由
│   │   ├── call_history.py     # SQLite 指标采集
│   │   ├── scheduler.py        # 定时任务
│   │   └── ami.py / conversation.py
│   ├── web/                    # Flask 中屏管理
│   │   ├── routes.py           # 路由 + API（含 host_ip 同步 pjsip.conf）
│   │   └── templates/          # HTML（8 页面）
│   ├── main.py                 # 入口
│   └── pregen_cached_audio.py  # 预合成固定话术（7 条）
├── data/                       # 持久化数据（bind mount）
│   ├── tftpboot/               # TFTP 配置下发（dialplan.xml + ATA<MAC>.cnf.xml）
│   ├── asterisk2/              # Asterisk 配置（pjsip.conf + extensions.conf 等）
│   ├── audio/                  # 预合成音频（开场白/结束语/兜底话术等）
│   ├── tts_cache/              # TTS 缓存（matcha + edge 子目录，50MB 自动清理）
│   └── metrics/                # 通话指标 SQLite
├── models/                     # ONNX 模型（挂载进容器，不打包镜像）
│   ├── ASR/                    # zipformer 流式中文
│   ├── TTS/                    # matcha-zh-en-8k
│   └── VAD/                    # ten-vad (int8)
├── win-soft/tftpd64/           # tftpd64 配置（DHCP + TFTP）
│   ├── tftpd32.ini             # Services=3, DHCP pool, Opt66
│   ├── tftpd32.chm             # 帮助文件
│   └── EUPL-EN.pdf             # 许可文件
├── refs/                       # 参考项目
├── docs/                       # 设计文档 + 阶段报告 + 项目日志
└── cisco-ata190固件/           # ATA190 固件仓库（1.2.1 + 1.2.2）
```

## 快速开始

### 1. 网络准备

Windows 有线网卡设静态 IP：`192.168.2.1`，子网掩码 `255.255.255.0`，网关留空。

### 2. 启动 tftpd64

双击 `tftpd64.exe`（需从 [tftpd32官网](http://tftpd32.jounin.net/) 下载放到 `win-soft/tftpd64/` 目录）。

配置检查：
- DHCP 标签：IP pool `192.168.2.100-200`，Option 66 = `192.168.2.1`
- TFTP 标签：Base Directory 指向 `data/tftpboot/`
- Services = 3（TFTP + DHCP）

### 3. 启动 Docker 服务

```bash
# 拉取镜像
docker pull escomputers/freepbx:17-nofail2ban

# 构建 AI 后端
docker compose build ai-backend

# 启动全部服务
docker compose up -d
```

### 3.1 启动本地 LLM（默认推理引擎）

本地推理走 WSL 内 llama-server（Qwen3.5-4B-MTP-GGUF，不依赖在线 API）：

```bash
# WSL 内执行
bash run-llama-server.sh --daemon          # 启动 llama-server（后台 + 日志落盘）
bash ~/.unsloth/llama.cpp/start-bridge.sh  # 启动控制桥（中屏 llama-server 页运维用）
```

容器经 `host.docker.internal:8081` 直连本地模型；中屏 `llm.provider=local_llama` 时生效，可在「AI 配置」切换在线 DeepSeek。

### 4. ATA190 上电

ATA190 网线直连笔记本，通电。tftpd64 日志应显示：
1. DHCP Discover → 分配 192.168.2.100
2. TFTP 请求 → 下载 `ATA34DBFD1893C3.cnf.xml` + `dialplan.xml`
3. 注册到 FreePBX（SIP 5060）

### 5. 测试

座机拨 9 → 听到开场白"您好，我是智话通" → 说话 → AI 回复

### 6. 恢复主分支

```bash
git switch main
docker compose up -d   # 恢复 TFTP 容器
```

## 中屏管理

AI 后端启动后访问 http://localhost:8080：

| 页面 | 功能 |
|------|------|
| 概览 | 系统拓扑 + 状态总览 + 配置摘要（IP 从配置读取） |
| 实时日志 | AudioSocket 对话日志（支持 call_id 回溯） |
| 通话指标 | KPI 卡片（通话数/时长/首包/整轮/P95）+ 明细 |
| FreePBX 配置 | 编辑 host_ip/ata_ip/AMI 凭证（改 host_ip 自动同步 pjsip.conf） |
| 分机管理 | 查看/添加分机 |
| TFTP 管理 | 文件列表/编辑 XML/上传固件/删除 |
| AI 配置 | ASR/TTS/LLM/搜索 参数（含 SEARCH 过滤词） |
| llama-server | 本地 LLM 启停/状态/日志/清除/导出 |
| 定时任务 | 每天/每周/一次性/间隔（星期复选） |
| 回拨管理 | 手动触发回拨测试 |

## 核心功能

| 功能 | 说明 |
|------|------|
| AI 对话 | 拨 9 进入 → AudioSocket 全双工 → ASR→LLM→TTS 三协程流水线 |
| 联网搜索 | 天气/股价/资讯 → AnySearch MCP 垂直域路由 → 注入 LLM |
| 语气词过滤 | ≤3 字语气词（诶/哦/好）不送 LLM，继续听 |
| 道别挂机 | 用户说"再见/拜拜/bye"或 AI 回复含道别词 → 自动挂机 |
| 回拨 | LLM 15 秒无回复 → 回电语 → 挂机 → AMI 外呼 → 继续对话 |
| 定时任务 | 到点联网查询 → LLM 生成 → TTS → 外呼座机 |
| 空回复兜底 | LLM 返回空内容 → 播"这个问题我暂时无法回答，抱歉" |
| 指标采集 | 通话结束写 SQLite，Metrics 页 KPI + Call History 回溯 |

## 关键参数

| 参数 | 值 | 说明 |
|------|-----|------|
| AI 入口 | 拨 9 | 座机拨 9 进入 AI 对话 |
| 回拨入口 | 200 / 201 | 回拨测试 / 回拨可追问 |
| 首字超时 | 5s | LLM 5 秒无首字播"好的请稍等" |
| 总超时 | 15s | LLM 15 秒无回复转回拨 |
| 用户超时 | 5s | 用户 5 秒不说话挂机 |
| 最大轮数 | 20 轮 | 单次通话上限 |
| TTS 缓存 | 50MB | 超限删最旧 30% |
| 回拨重试 | 3 次 × 15min | 无人接听重试策略 |
| ATA190 固件 | 1.2.1(004) | 非安全模式，UDP，秒注册 |

## 常见问题

| 问题 | 解决 |
|------|------|
| 拨 9 直接忙音 | 检查 extensions.conf 的 AudioSocket UUID 是否 `${UUID()}` |
| 通话 32 秒准时挂断 | 检查 pjsip.conf 的 `local_net` 是否只含 `127.0.0.1/32` |
| ATA190 注册慢 | 固件降级到 1.2.1 或检查 XML 的 deviceSecurityMode=0 |
| LLM 频繁空回复 | 检查 llm.py 是否加了 `extra_body={"thinking": {"type": "disabled"}}` |
| 占位语不出 | 重启 ai-backend 容器（config.yaml 改后需重启才能生效） |
| 拨号就发第一个数字 | kpml 3→0 + dialplan.xml |
| 免提听不到 | enableEC 1→0（关回声消除） |
| 指标页全空 | 检查 SQLite 连接是否设 row_factory |

## 外部依赖（不随仓库分发，需自行获取）

### 模型（models/）

模型全部来自 [k2-fsa/sherpa-onnx](https://github.com/k2-fsa/sherpa-onnx) 官方 GitHub release，详见 `models/README.md`。

| 目录 | 模型 | 下载 |
|------|------|------|
| models/ASR/ | zipformer 流式中文 int8 | `asr-models` release |
| models/TTS/ | matcha 中英混读 | `tts-models` release |
| models/VAD/ | ten-vad int8 | `asr-models` release |

### 参考项目（refs/）

| 目录 | 来源 |
|------|------|
| refs/audiosocket_server | https://github.com/silentindark/audiosocket_server |
| refs/AVA-AI-Voice-Agent-for-Asterisk | https://github.com/hkjarral/AVA-AI-Voice-Agent-for-Asterisk |
| refs/Cisco-7900-and-8800-series-freepbx-setup | https://github.com/buba0/Cisco-7900-and-8800-series-freepbx-setup |
| refs/jetson-tts | https://github.com/vieenrose/jetson-tts |

### tftpd64

需从 [tftpd32.jounin.net](http://tftpd32.jounin.net/) 下载 `tftpd64.exe`，放到 `win-soft/tftpd64/` 目录。

## 参赛信息

- 赛事：OPC息壤杯
- 团队：智话通
- 赛道：惠民产品创新 / AI+自选开放场景
- 赛程：8 月预赛 PPT → 9 月复赛 → 11 月决赛

## 详细文档

- [项目日志](docs/项目日志.md) — 开发踩坑记录（含 8/22 优化全记录）
- [阶段报告 v4](docs/固话智联-阶段报告v4（20260829）.md) — 最新阶段报告（本地 LLM + 中屏运维 + 搜索提速）
- [架构拓扑](docs/固话智联-架构拓扑.html) — 系统架构拓扑图
- [通话流程](docs/固话智联-通话流程.html) — 通话流程图（泳道图）
- [方案设计书](docs/固话智联-方案设计书.md) — 整体方案设计
- [TFTPD64 单机演示实施记录](docs/complete/complete-20260817-TFTPD64单机演示.md)

---

*blacksamuraiiii · 2026年8月*