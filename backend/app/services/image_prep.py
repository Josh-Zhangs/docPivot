"""
图像预处理服务
--------------
V1 预处理三步（均为保守操作，只提升 VLM 输入质量，不引入失真）：
1. EXIF 方向矫正   —— 手机拍照最常见的问题：像素没错但旋转标记导致"横躺"
2. 最长边限幅缩放  —— 2000px 以上对识别质量无增益，只拖慢推理
3. 轻度对比度增强  —— 拍照光照不均时提升文字对比（autocontest 截断 1%，避免过冲）

后续（M2 里程碑）可在此追加 OpenCV 透视矫正、去噪等。
"""
import base64
import io

from PIL import Image, ImageOps

from app.config import MAX_IMAGE_EDGE

# 允许的输入格式（与前端 accept 保持一致）
ALLOWED_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".bmp"}


def preprocess_image(data: bytes) -> str:
    """
    输入原始图片字节，返回预处理后的 base64（不带 data: 前缀），
    直接可用于 Ollama /api/chat 的 images 字段。
    """
    img = Image.open(io.BytesIO(data))

    # 1. EXIF 方向矫正（无 EXIF 时是 no-op）
    img = ImageOps.exif_transpose(img)

    # 2. 统一转 RGB（PNG 透明通道 / 灰度图统一处理，JPEG 化输出）
    if img.mode != "RGB":
        img = img.convert("RGB")

    # 3. 最长边限幅缩放（LANCZOS 缩小，文字边缘保持锐利）
    w, h = img.size
    longest = max(w, h)
    if longest > MAX_IMAGE_EDGE:
        scale = MAX_IMAGE_EDGE / longest
        img = img.resize((int(w * scale), int(h * scale)), Image.LANCZOS)

    # 4. 轻度对比度增强：截断两端 1% 像素做拉伸，改善拍照发灰
    img = ImageOps.autocontrast(img, cutoff=1)

    # 输出 JPEG（质量 90）：体积小、对 OCR 无可见损失
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=90)
    return base64.b64encode(buf.getvalue()).decode("ascii")


def is_allowed(filename: str) -> bool:
    """按扩展名校验是否为受支持的图片类型"""
    suffix = "." + filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    return suffix in ALLOWED_SUFFIXES


def probe_thumb_b64(data: bytes, edge: int = 512) -> str:
    """
    语言探测专用缩略图：判语言不需要看清每一笔，512px 足够。
    实测（qwen2.5vl:7b，M5 Max）：512px 平均 1.5s、6/6 判定正确；
    768px 平均 2.5~4s 且准确率相同——所以取 512。
    （识别主流程另用 MAX_IMAGE_EDGE=2000，两者用途不同，不要合并）
    """
    img = Image.open(io.BytesIO(data))
    img = ImageOps.exif_transpose(img)
    if img.mode != "RGB":
        img = img.convert("RGB")

    w, h = img.size
    longest = max(w, h)
    if longest > edge:
        scale = edge / longest
        img = img.resize((int(w * scale), int(h * scale)), Image.LANCZOS)

    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=80)
    return base64.b64encode(buf.getvalue()).decode("ascii")
