"""
docPivot 后端全局配置
--------------------
所有可变配置集中在 .env 文件中，代码不写死任何环境相关的值。
优先级：.env > 环境变量 > 代码默认值
"""
import os
from pathlib import Path

from dotenv import load_dotenv

# backend/ 目录（config.py 位于 backend/app/ 下，向上两级）
BASE_DIR = Path(__file__).resolve().parent.parent

# 加载 backend/.env
load_dotenv(BASE_DIR / ".env")

# ---------------------------------------------------------------------------
# Ollama / 模型配置
# ---------------------------------------------------------------------------
# 日本 Mac 的 Tailscale 地址（Ollama 监听端口）
OLLAMA_BASE_URL: str = os.getenv("OLLAMA_BASE_URL", "http://100.91.216.0:11434").rstrip("/")
# 视觉模型名（Mac 上需先 ollama pull，见 README）
OLLAMA_MODEL: str = os.getenv("OLLAMA_MODEL", "qwen2.5vl:7b")
# 单次识别超时（秒）。32B 大模型首 token 可能较慢，给足余量
OLLAMA_TIMEOUT: int = int(os.getenv("OLLAMA_TIMEOUT", "300"))

# ---------------------------------------------------------------------------
# 语言识别（决定加载哪套提示词）
# ---------------------------------------------------------------------------
# 开启后：识别前用「缩略图 + 1 个 token」探测文档语言（ja/zh/en），再选对应提示词。
# 关掉则一律用 LANG_DEFAULT，适合批量跑单语种任务时省掉这一步开销
LANG_DETECT: bool = os.getenv("LANG_DETECT", "1").lower() not in ("0", "false", "no", "off")
# 探测失败/返回未知语言时的回退语言（日语版提示词对英文单据同样适用）
LANG_DEFAULT: str = os.getenv("LANG_DEFAULT", "ja")

# ---------------------------------------------------------------------------
# 上传与图像预处理
# ---------------------------------------------------------------------------
# 单文件大小上限（MB）
MAX_UPLOAD_MB: int = int(os.getenv("MAX_UPLOAD_MB", "20"))
# 预处理后图片的最长边（像素）。过大图会显著拖慢 VLM 且无质量收益
MAX_IMAGE_EDGE: int = int(os.getenv("MAX_IMAGE_EDGE", "2000"))
# 原图保存目录
UPLOAD_DIR: Path = Path(os.getenv("UPLOAD_DIR", str(BASE_DIR / "uploads")))
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

# SQLite 数据库文件
DB_PATH: Path = BASE_DIR / "docpivot.db"

# ---------------------------------------------------------------------------
# CORS：允许的前端来源（Vite dev server 默认 5173）
# ---------------------------------------------------------------------------
CORS_ORIGINS: list[str] = [
    o.strip() for o in os.getenv("CORS_ORIGINS", "http://localhost:5173").split(",") if o.strip()
]
