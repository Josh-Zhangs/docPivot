# docPivot

日语文档 → Markdown 识别工具（IDP 第一阶段）。全流程本地/私有部署，数据不出网。
**中日双语**：上传后自动判定文档语言（ja/zh/en），按语言加载对应提示词。

架构与规划见 `PLAN.md`。

## 目录

- `backend/` FastAPI 后端（预处理 + Ollama VLM 调用 + SSE + SQLite）
- `frontend/` Vite + React + TS 前端（上传 / 任务列表 / 左图右文对照）

## 前置条件（Mac 侧，一次性）

现有模型（qwen3:30b 等）均为**纯文本模型，不能识图**，需要拉一个视觉模型：

```bash
ollama pull qwen2.5vl:7b     # 先跑通流程（约 6GB）
# 质量对比阶段（M3）可再加：ollama pull qwen2.5vl:32b
```

Mac 的 Ollama 已通过 Tailscale 暴露：`http://100.91.216.0:11434`（已写入 `backend/.env`）。

## 启动（Windows 侧）

### 后端

```bash
cd backend
python -m venv .venv
.venv/Scripts/activate
pip install -r requirements.txt
uvicorn app.main:app --reload --port 8000
```

### 前端

```bash
cd frontend
npm install
npm run dev        # http://localhost:5173
```

打开浏览器访问 http://localhost:5173 ，拖入图片即可识别。

## 常用检查

- 后端可达性：`curl http://localhost:8000/api/health`
- Mac Ollama 可达性：浏览器打开 `http://localhost:8000/api/models`（会代理查询远端模型列表，`ok:false` 说明 Tailscale 不通）
- 识别结果与原图持久化在 `backend/docpivot.db` 与 `backend/uploads/`

## 配置

所有配置在 `backend/.env`：模型名、Ollama 地址、超时、图片最长边等。改完重启后端生效。

### 双语识别（中日）

| 配置 | 默认 | 说明 |
|---|---|---|
| `LANG_DETECT` | `1` | 识别前用「512px 缩略图 + 1 个 token」判定语言（实测平均 1.5s、6/6 正确），再加载对应提示词。设为 `0` 可跳过（批量跑单语种任务时省这一步） |
| `LANG_DEFAULT` | `ja` | 判定失败/未知语言时回退的提示词 |

提示词在 `backend/app/services/prompt.py`：`PROMPT_JA`（主力，已回归 70+ 张真实单据）、
`PROMPT_ZH`（规则与日语版一一对应，仅语法与示例本地化）。
**改规则必须两侧同步**——只改一边会让两种语言的输出形态不一致，评测时无法对比。

判定结果落库在 `tasks.lang`，结果区显示「日本語 / 中文」徽章，便于按语言分组核对。
