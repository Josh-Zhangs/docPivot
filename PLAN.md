# docPivot — 文档智能处理平台 · V1 规划/架构文档

> 版本：v0.1（2026-11-02 起草）
> 状态：**待评审**——本文档审阅确认后再开始编码
> 已确认决策：OCR 采用 **VLM 为主**；V1 输出 **Markdown 还原**；前端 **Vite + React + TS**；识别对象为 **手机拍照 + 屏幕截图**

---

## 1. 项目定位

docPivot 是一个本地化文档智能处理工具的第一阶段：**把图片（日语）还原成可编辑的 Markdown**。
- 全流程本地/私有部署：识别在本地模型上完成，数据不出网。
- 战略意义：这是 IDP（文档智能处理）的地基。后续阶段在此基础上叠加「结构化字段抽取 → 校验 → 入库/导出」，最终形成可向公司立项的完整方案。

### V1 范围（做什么）
- 上传本地图片（PNG/JPG，单图 + 批量）
- 调用本地 VLM 识别日语文档，输出 **Markdown**（保留标题层级、表格、列表）
- 前端左右对照（左原图 / 右结果），支持复制、导出 .md
- 识别历史记录（SQLite），可回看
- 手机拍照的预处理：EXIF 方向矫正、缩放、对比度增强

### V1 明确不做（防止范围膨胀）
- ❌ 结构化字段抽取（JSON schema）→ 留给 V2
- ❌ 手写体承诺 → 只求"能识别一部分"，不做验收指标
- ❌ PDF 直接解析（先支持图片；PDF 后续在前端转图或后端补）
- ❌ 多用户/权限系统

---

## 2. 总体架构

```
┌─────────────────────────────  Windows 笔记本（开发/客户端） ─────────────────────────────┐
│                                                                                          │
│  ┌─────────────── React 前端 (Vite+TS, :5173) ───────────┐   ┌──────────────────────┐  │
│  │  上传/拖拽 ──▶ 任务列表 ──▶ 对照视图(原图|Markdown) ──▶ 导出 │  │   测试图片 samples/    │  │
│  └──────────────────────┬────────────────────────────────┘   └──────────────────────┘  │
│                         │ HTTP / SSE                                                    │
│  ┌──────────────────────▼──────────── FastAPI 后端 (:8000) ──────────────────────────┐  │
│  │  routers/ocr.py        接收图片、下发任务、SSE 推流                                  │  │
│  │  services/image_prep   EXIF 矫正 / 限边缩放 / 对比度增强                            │  │
│  │  services/prompt       VLM 识别 Prompt 模板（日语、保版式、禁翻译）                  │  │
│  │  services/vlm_client   Ollama HTTP 封装（base_url 可配）                            │  │
│  │  db.py                 SQLite：任务表（状态/耗时/结果 md/模型名）                    │  │
│  └──────────────────────┬────────────────────────────────────────────────────────────┘  │
└─────────────────────────┼────────────────────────────────────────────────────────────────┘
                          │ SSH 隧道 localhost:11434 → 日本 Mac:11434
┌─────────────────────────▼────────────────────────────────────────────────────────────────┐
│  Apple M5 Max / 128GB（训练与推理主机）                                                    │
│    Ollama serve ──▶ qwen2.5vl / qwen3-vl（视觉模型，识别主力）                              │
│    （预留）mlx-lm LoRA 微调环境 → 未来替换/增强识别模型                                     │
└──────────────────────────────────────────────────────────────────────────────────────────┘
```

设计要点：
1. **后端不写死 Ollama 地址**：`OLLAMA_BASE_URL` 走 .env。本机 Ollama、Mac 隧道、未来 vLLM 都能无缝切换。
2. **SSE 流式**：VLM 生成 Markdown 是逐 token 的，前端流式渲染体验远好于转圈等待。
3. **识别与业务解耦**：`prompt.py` 独立成模块，后续做字段抽取时只换 prompt、不动架构。

---

## 3. 技术选型与理由

| 层 | 选型 | 理由 |
|---|---|---|
| OCR 识别 | **本地 VLM（Ollama，如 qwen2.5vl:7b/32b）** | 一次完成文字+版式+表格理解；M5 Max 128GB 算力充裕；私有化 |
| OCR 兜底 | Tesseract（jpn） | 已有经验；做准确率对照基线（V1 先不接，V2 再加对照页） |
| 后端 | FastAPI + Uvicorn | 已熟练；async + SSE 支持好；Pydantic 模型 |
| 图像预处理 | Pillow + OpenCV-python | EXIF 方向、缩放、CLAHE 对比度、轻量去噪 |
| 前端 | Vite + React 18 + TypeScript | 已拍板；TS 保证长期项目类型安全 |
| Markdown 渲染 | react-markdown + remark-gfm（表格） | GFM 表格渲染必须 |
| 存储 | SQLite（后端同目录 docpilot.db） | 零运维；V1 记录任务足够 |
| 包管理 | 后端 venv + pip；前端 pnpm/npm | 常规 |

**模型候选**（Mac 上拉取后实测对比，选型数据将沉淀为文档）：
- `qwen2.5vl:7b`（~6GB，速度快，先跑通）
- `qwen2.5vl:32b` / `qwen3-vl` 系列（~20GB，质量上限，M5 Max 可跑）
- 备选：`minicpm-v` 系列对文档场景口碑也不错

---

## 4. 目录结构（规划）

```
docPivot/
├── docs/                        # 文档（本文档所在）
├── backend/                     # FastAPI 后端
│   ├── app/
│   │   ├── main.py              # 应用入口、CORS、路由注册
│   │   ├── config.py            # 读取 .env（OLLAMA_BASE_URL、模型名等）
│   │   ├── routers/
│   │   │   └── ocr.py           # POST /api/ocr  GET /api/ocr/{id}/stream  GET /api/tasks
│   │   ├── services/
│   │   │   ├── image_prep.py    # EXIF/缩放/对比度（中文注释齐全）
│   │   │   ├── prompt.py        # 识别 prompt 模板（独立可替换）
│   │   │   └── vlm_client.py    # Ollama /api/chat 封装（流式）
│   │   └── db.py                # SQLite 初始化与任务表操作
│   ├── requirements.txt
│   └── .env.example
├── frontend/                    # Vite + React + TS
│   └── src/
│       ├── api/client.ts        # 后端 API 封装（fetch + SSE）
│       ├── components/
│       │   ├── UploadPanel.tsx  # 拖拽/多选上传
│       │   ├── TaskList.tsx     # 任务状态列表
│       │   └── CompareView.tsx  # 左原图|右Markdown 对照 + 复制/导出
│       ├── App.tsx
│       └── main.tsx
├── samples/                     # 测试图片（拍照/截图样本，可放脱敏样本）
├── README.md                    # 启动说明
└── PLAN.md                      # 本文档
```

---

## 5. API 设计草案

| Method | Path | 说明 |
|---|---|---|
| POST | `/api/ocr` | multipart 上传 1..N 张图，返回 task_id 列表（立即入库 status=queued） |
| GET | `/api/ocr/{task_id}/stream` | SSE：推送进度（preprocessing → recognizing → done）与逐 token Markdown |
| GET | `/api/tasks` | 历史任务列表（状态、耗时、模型） |
| GET | `/api/tasks/{task_id}` | 单任务详情（含结果 md、原图路径） |
| DELETE | `/api/tasks/{task_id}` | 删除任务及其文件 |

**识别 Prompt 核心约束**（写入 prompt.py，后续迭代）：
- 你是日语文档 OCR 引擎。逐字转录图片内容，禁止翻译、禁止总结、禁止补全不存在的内容
- 输出 Markdown：标题层级/表格/列表/复选框尽量还原原版式
- 无法辨认的字段用 `[判読不能]` 占位，不要猜

---

## 6. 里程碑（按每周 ~15h 切）

| 阶段 | 内容 | 预估 |
|---|---|---|
| **M0 环境** | Mac 装/验 Ollama → 拉 qwen2.5vl:7b → SSH 隧道打通（curl 验证）；Windows 建 backend/frontend 脚手架 | 0.5 周 |
| **M1 单图闭环** | 上传 1 张 → 预处理 → VLM 识别（SSE 流式）→ 前端对照视图 + 复制/导出 | 1.5 周 |
| **M2 批量与历史** | 多图排队、任务列表、SQLite 历史、手机拍照预处理调优 | 1 周 |
| **M3 质量沉淀** | 抽 20 张样本做人工比对，记录识别问题；模型 7b vs 32b 对比报告（未来立项素材）；导出 .md/.docx | 1 周 |

M0 需要你先做的一件事（代码之外）：
1. Mac 上：`brew install ollama`（如未装）→ `ollama pull qwen2.5vl:7b`
2. Mac 上开隧道（反向隧道示例，在 **Mac** 上执行）：`ssh -N -R 11434:localhost:11434 <你的Windows可达中转>`；若两机在同一网段更简单——直接在 Windows `ssh -L 11434:localhost:11434 user@<mac-ip>`
3. Windows 验证：`curl http://localhost:11434/api/tags` 能列出模型即通

---

## 7. 风险与待确认项

| # | 风险/待确认 | 对策 |
|---|---|---|
| 1 | SSH 隧道断线导致识别中断 | 后端对 Ollama 调用做超时+重试，前端显示失败原因；后续可换 Tailscale 等常驻方案 |
| 2 | 手机拍照倾斜/光照差，识别率下降 | M2 做 CLAHE + 限边缩放；效果不足再上 OpenCV 透视矫正 |
| 3 | VLM 偶发"幻觉补全"（把模糊字猜成合理词） | Prompt 已禁猜+占位符；M3 人工比对量化幻觉率 |
| 4 | 32b 模型体积大，首拉时间长 | 先 7b 跑通全流程，32b 在 M3 做质量对比 |
| 5 | 混合截图（UI 截图含英文/代码） | Prompt 支持"以出现语言原样转录"，不强制日语 |

---

## 8. 下一步（确认本文档后）

1. 你确认/修改本方案（尤其是 V1 边界和里程碑顺序）
2. 我搭 M0：前后端脚手架 + .env + Mac 环境验证清单
3. 按 M1 → M3 顺序迭代，每个里程碑结束输出变更总结
