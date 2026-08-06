# AI VTuber 框架

一个面向 B 站直播的 AI 虚拟主播框架:实时读取弹幕 / 礼物 / SC,经**三队列调度 + 升舱机制**排序,LLM 生成回复,TTS 合成语音,VTube Studio 驱动表情。全链路已在实际直播中跑通。

> **本仓库是"机制 / 框架"的公开纯净版,不含任何主播人格 IP,也不含任何密钥。** 人格、音色、Live2D 模型都由你自己准备。

## 特性

- **三队列调度 + 升舱** — 弹幕与礼物分六队列,高分消息即时响应;滑动窗口弹幕均分门槛,超阈值自动把 Q2/Q3 高价值消息提拔到 Q1,解决直播场景下的**优先级倒置**(高价值礼物被低价值弹幕洪峰淹没)
- **流水线并行** — 播放中后台合成下一条;LLM 走网络可并行,延迟被隐藏在 TTS 合成耗时之后,瓶颈只剩 TTS + 播放
- **情绪驱动表情** — LLM 一次性返回情绪标签,切对应 VTS 表情并叠加身体晃动参数
- **画面评论(可选)** — 屏幕截图 → VLM 描述 → LLM 生成主播对画面的即时评论
- **中控台(可选)** — Flask + SocketIO,实时可视化全流程状态

## 架构

```
弹幕 / 礼物 / SC ──► 评分(本地权重 + LLM 打分)──► 六队列(Q1/Q2/Q3 × 弹幕/礼物)
                                                        │
                                                        ▼
                             滑动窗口升舱 ◄── 主循环调度 ── 播放 + VTS 表情
                                                        │
                                                        ▼
                                      LLM 回复 ──► TTS 合成 ──► 播放(后台预合成下一条)
```

## 快速开始

```bash
# 1. 安装依赖
pip install -r requirements.txt

# 2. 配置(填上你自己的密钥)
cp .env.example .env

# 3. 准备人格:编辑 persona.py 的 SYSTEM_PROMPT(当前是占位模板)

# 4. 准备音色(可选,或用 CosyVoice 官方音色)
python create_voice.py reference.wav

# 5. 准备 Live2D 模型:导入 VTube Studio,配置表情文件、hotkey,
#    并把 vts_client.py 里的 PLUGIN_NAME 改成你的模型插件名

# 6. 启动
python main.py
```

## 需要你自己准备的

| 项 | 说明 |
|---|---|
| API 密钥 | 全部走 `.env`,仓库内没有任何密钥 |
| 人格 | `persona.py` 是模板,替换成你自己的角色 |
| 音色 | `create_voice.py` 一键克隆,或直接用官方音色 |
| Live2D 模型 | 版权自持,在 VTS 里配置表情 / hotkey / 插件名 |
| B站登录 | `bilibili_cookies.json` 自备,不入库 |

## 目录

```
├── main.py               # 入口:串起弹幕 + 调度 + AI + TTS + VTS
├── scheduler.py          # 三队列调度器(流水线 + 升舱)★ 核心
├── score_queue.py        # 有界排序队列
├── scorer.py             # 本地权重 + LLM 评分
├── danmaku_bot.py        # B站弹幕/礼物/SC 接收
├── ai_chat.py            # LLM 对话(带重试)
├── tts_engine.py         # CosyVoice 云端 TTS
├── expression_engine.py  # 情绪分类
├── vts_client.py         # VTube Studio HTTP 交互
├── vision_module.py      # 画面评论(可选)
├── dashboard.py          # 中控台(可选)
├── wbi_sign.py           # B站 WBI 签名工具
├── persona.py            # 人格模板(自填)
└── config.py             # 从 .env 读配置,无密钥
```

## License

[MIT License](LICENSE) — Copyright (c) 2026 Tempest

## 免责声明

本项目仅供学习研究。使用前请遵守各 API 服务商条款、VTube Studio 与 Live2D 模型授权协议、B 站社区规范,并自行承担使用风险。
