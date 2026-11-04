"""
数字自检层（Cross-check Validation）
====================================
核心洞察：小票/領収書由 POS 系统打印，数字天然自洽——
  明细和 = 小計、小計(±税) = 合計、お預かり − 合計 = お釣り、
  税组满足 floor(対象額×rate/(100+rate)) ≈ 消費税、
  適格請求書登録番号末位有国税庁校验位算法。

校验对象不是小票（它几乎不会错），而是 OCR 结果：
OCR 认错任何一个数字，自洽立即被破坏 → 几次加法即可定位"哪里可能认错了"。

设计原则：
- 纯规则、零依赖、无模型调用：亚毫秒级
- 宁缺勿报：解析不出关键行 → 对应规则静默跳过；checks_run=0 表示"没校验"
- 只覆盖数字类错误（金额认错/幻觉行/漏行/税额矛盾）；文字错误由人工修正兜底

错误/提示分级（唯一策略，不是逐例补丁）：
- error 只留给"总账链"矛盾：小計(±税±値引)=合計、お預かり−合計=お釣り。
  这是"这笔生意收了多少钱"的最终事实，两者矛盾基本只能是 OCR 认错大数。
- 明细级检查（明细和、税率组、番号校验位）一律 warning。实证依据：
  ① 堂吉诃德 5,838=2×2,919 —— 模型把每件商品输出两行（条码行+商品行），
    金额全对，只是转写格式缺陷；
  ② 無名軒2 的税率区纸面印的就是 341/338/308/305（放大原图逐位核对），
    与总金额 7,840 在实物上就无法调和，但转写完全忠实于原图——
    真实小票也可能印得不自洽。
  总账链一旦自洽，明细矛盾自然只值得"人工看一眼"，不该把整张票标红。
"""
import logging
import re
from dataclasses import dataclass, field

logger = logging.getLogger("docpivot.validation")

# ---------------------------------------------------------------------------
# 文本归一化
# ---------------------------------------------------------------------------

# 全角数字/逗号 → 半角
_Z2H = str.maketrans("０１２３４５６７８９，　", "0123456789, ")

# 金额 token：¥ 前缀（可带两位小数——中文小票金额到分，¥31.20），或含千分位逗号的数字
# （纯裸数字不算金额——避免把口座番号/日期/数量当成金额，宁可漏检不误报）
_MONEY_TOKEN = re.compile(r"[¥￥]\s*[\d,]+(?:\.\d{1,2})?|[\d]+(?:,[\d]{3})+(?:\.\d{1,2})?")

# 中文小票几乎不印 ¥ 符号，金额靠"带小数点的价格"表示（9.9 / 6.53 / 41.38）。
# 这条只在上面的 _MONEY_TOKEN 一无所获时才启用（见 _amounts_in_line），
# 因此对日元单据零影响——日元没有小数位，本来也匹配不到。
# 小数位取 1~2 位：中国超市常印一位小数（永辉实测「9.9 1 9.9」），金额到分时是两位。
# 前后否定数字/小数点：避免把 0.384（数量，三位小数）、1.598 这类切错；
# 后面紧跟计量单位的不是金额——「072 7 氷 1.1kg *224」里的 1.1 是重量（711_4 实测回归）
_DECIMAL_AMOUNT = re.compile(
    r"(?<![\d.])(\d{1,7}\.\d{1,2})(?![\d.]|\s*(?:kg|g|ml|l|cm|mm|m|%|個|枚|本|袋|/\s*kg))",
    re.I,
)

# 假名判据：有假名 → 日文单据。用于区分记账习惯——
# 日式收据必印「小計」且合計含税；中文小票常只印「应收/总计」。
_KANA = re.compile(r"[\u3040-\u30ff]")

# 商品名判据：假名或汉字（含简繁）。用于区分"商品行"与"编码/数量注记行"
_NAME_CHAR = re.compile(r"[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff]")

# ---- 中文小票的裸整数金额（实测：库兹明/广州大排挡/秋记）----
# 中国餐饮单的金额常是**不带 ¥ 也不带小数的整数**：「莫斯科沙拉 1份 18 18」「纸巾 3 4包 12」。
# 只认 ¥/小数时这些行全部漏掉，明细和只加了一小部分 → 报出一个单据上
# 不存在的"幽灵合计"（用户实测反馈：87.60/323.80/296.95 均不存在）。
# 启用条件（宁缺勿报，四道闸）：
#   ① 仅当整张单据无假名（日文的裸数字是点数/条码/数量，绝不能当金额）；
#   ② 行内有商品名（假名或汉字）；
#   ③ 行内 ≥2 个数字（「-做法:少冰 1」这类单数字注记行排除）；
#   ④ 行尾是 1~4 位数字、且前面是空白（排除「人数:1」「X10BP…3527」这类：
#      前面是冒号/字母说明它是编号的一部分，且长度上限挡住长编号）。
_BARE_TRAILING = re.compile(r"(?<=\s)(\d{1,4})\s*$")
_BARE_ANY_NUM = re.compile(r"\d+(?:\.\d+)?")

# 退货/退菜段：中式餐饮单常把"退掉的菜"单独成段印在点单明细之后
# （实测餐饮杭州：「----退货明细----」下面「%浇汁秋葵 1份 26.00」）。
# 这段的行是**另一笔交易的回退**，不属于本单应收——计进去会让明细和虚高
# （653.60 含退货 vs 总金额 313.80；剔除退货的 9 项恰好 313.80）。
_RETURN_SECTION = re.compile(r"退货|退菜|退单|退款|作废|取消明细")

# 干扰模式：时间(19:47)、税率(8%)、日期(2025年/12月/13日)、点数(4点)、
# 番号/电话/口座/金額关键词行——这些行不参与明细
_NOISE = [
    re.compile(r"\d{1,2}:\d{2}(?::\d{2})?"),         # 时间（含秒：23:00:21）
    re.compile(r"\d{1,2}\s*%"),                      # 税率百分比
    re.compile(r"\d{4}\s*年|\d{1,2}\s*月|\d{1,2}\s*日"),  # 日期
    re.compile(r"\d{4}[/\-]\d{1,2}[/\-]\d{1,2}"),    # 日期（2024/7/27、2024-07-27）
    re.compile(r"\d+\s*点"),                         # 点数
    re.compile(r"\d{2,4}-\d{3,4}-\d{3,4}"),          # 电话
]
_EXCLUDE_LINE = re.compile(
    r"銀行|口座|支店|番号|No\.\s*\d|TEL|電話|#\d|^#|^\s*-{3,}|金額|郵便|〒|电话|客服"
    # 中文单据的编号类字段（裸数字易被误当金额）：交易号/单号/订单/流水/工号/机号/
    # 凭证/会员卡/卡号/积分/小票号——这些行的数字是编号不是钱
    r"|交易号|单号|订单|流水|工号|机号|凭证|会员卡|卡号|积分|小票号"
)
_DISCOUNT = re.compile("値引|割引|优惠|折扣|立减|圆整|抹零")

# レジ袋（购物袋）行：日本收据上它的价格几乎总不带 ¥ 符号（实测 711_2：「-201 レジ袋 L  5」），
# 普通金额提取只认 ¥/千分位逗号会漏掉这 5 日元，造成明细和与小計差个位数的假警报。
# 仅对该关键词行放行"行尾裸数字"作为金额，不影响其他行的防误报设计。
_BAG = re.compile(r"レジ袋|レジバッグ")
_TRAILING_NUM = re.compile(r"(\d+)\s*$")

# 数量×单价的附属说明段：两种印刷顺序都要认——
#   「(3個 × ¥780)」数量在前（無名軒实测） / 「(¥116 X 2個)」价格在前（711_2 ヨークベニマル实测）。
# 它不是一件独立商品，其中的单价更不能参与求和。
# 实测 711_2：(¥116 X 2個) 单独成行，旧正则只认数量在前的顺序，
# 116 被当成一件商品计入，导致 1286+116=1402 的假警报。
# 写法注意：相邻字符串隐式拼接不能跨行"越过"变量名（+ _QTY_UNIT 后换行再写字符串
# 会被解析为「变量 字符串」相邻而报语法错），所以收尾的字符串必须与 _QTY_UNIT 同行。
_QTY_UNIT = r"(?:個|点|枚|本|杯|切|袋|箱|食|人|皿|コ)?"
_QTY_PRICE = re.compile(
    r"[（(]\s*(?:\d+\s*" + _QTY_UNIT + r"\s*[×xX＊*]\s*[¥￥]?\s*[\d,]+\s*(?:円)?"
    r"|[¥￥]?\s*[\d,]+\s*(?:円)?\s*[×xX＊*]\s*\d+\s*" + _QTY_UNIT + r")\s*[）)]"
)

# 关键词（OCR 可能错字，用宽松子串）；中日双语——换语言只加关键词，不动逻辑
# 总计：中国超市小票在"优惠前"的合计（总计 46.18 − 商户优惠 4.80 = 应收 41.38），语义等价小計
_SUB_KEYS = ("小計", "小 計", "小计", "总计")
# 買上：ヨークベニマル等收据把合计行印作「お買上計」（实测 711_2，不含"合計"两字）；
# 应付总额/实付金额/应收/应付：中文 POS 常用；价税合计：中国增值税发票的含税总额；
# 总额：华润万家把合计印作「收款总额」（注意"优惠总计"由 _is_subtotal 的折扣排除挡住）
_TOT_KEYS = ("合計", "合 計", "合计", "買上", "买上", "应付总额", "实付金额", "价税合计", "应收", "应付", "总额", "总金额")
_TENDER_KEYS = ("お預り", "お預かり", "おあずか", "实收", "实付", "收款")
# 日式付款关键词单列：只有日式收据会把它和「合計」拼成一个词（お預り合計），
# 中文没有这种现象（「应收 … 实收 …」是同一行两个不同字段，必须都认）
_JP_TENDER_KEYS = ("お預り", "お預かり", "おあずか")
_CHANGE_KEYS = ("お釣り", "お钓", "釣銭", "找零", "找赎", "找回")
# 汇总区折扣行关键词 + 行尾裸数字（可带负号）的金额
# 优惠/折扣/立减：中文 POS 的减免行（「会员优惠 -¥1.00」）；
# 圆整/抹零：中文小票的凑整行（「圆整差额 0.4」，广州大排挡实测）——
# 不是商品，按折扣归类（记负）后 R1 的"明细和−折扣=总额"形态即可容纳
_DISCOUNT_KW = ("値引", "割引", "优惠", "折扣", "立减", "圆整", "抹零")
_SUMMARY_DISCOUNT_NUM = re.compile(r"(-?\d+)\s*$")

# 疑似粘连 token：¥ 后 8 位以上连续数字。真实小票价格几乎不可能这么大（¥1,000 万级），
# 实测堂吉诃德（7b 输出）：「皮なし有色明太子 ¥267611004531JAN」＝ 价格 ¥267 与 JAN 条码 611004531
# 被模型粘连成一个 token。这类串不参与明细求和（剔除后其余金额恰好与小計吻合），
# 但输出本身有质量缺陷，要以 warning 提示用户重新识别或人工修正。
_MERGED_TOKEN = re.compile(r"[￥¥]\s*\d{8,}")
# 分单位的过滤阈值：8 位以上数字 = ≥¥10,000,000 = ≥10^9 分
_MERGED_LIMIT = 1_000_000_000

# 税率组：
# 対象額行——「（税率8%対象額 ¥2,919）」（传统内税格式）与「8%対象額 ¥2,919」（堂吉诃德外税格式）
_TAX_BASE = re.compile(r"[（(]?\s*(?:税率|内税)?\s*(\d{1,2})\s*%\s*対象額\s*[¥￥]?\s*([\d,]+)")
# 税额行（带税率）——「8%税額 ¥233」或「（内消費税 8% ¥233）」
_TAX_AMT_RATE = re.compile(r"(?:[（(]\s*)?(?:内消費税等?|消費税等?)?\s*(\d{1,2})\s*%\s*税額\s*[¥￥]?\s*([\d,]+)")
# 税额行（括号格式，可无税率）——「（内消費税等 ¥233）」「（税額合計 ¥233）」
_TAX_AMT_PAREN = re.compile(r"[（(]\s*(?:内消費税等?|消費税等?|税額合計)\s*(?:(\d{1,2})\s*%\s*)?[¥￥]?\s*([\d,]+)")
_INVOICE = re.compile(r"\bT\d{13}\b")

# 条码/编号行的判据：剔除金额后只剩「6 位以上数字（可带 JAN/CD 标记）」。
# 实测堂吉诃德（7b 输出）：每件商品输出两行——
#   「4562347040034JAN ¥195」＋「* 長ねぎ ¥195」
# 前者是商品行的条码注记而非独立明细，计入求和会让明细合计恰好翻倍
# （5,838 = 2×2,919）。用"结构规则"根治，而不是给这家店打补丁。
# ⚠ 不能放宽成"不含日文就跳过"：中文 POS 习惯把商品名与「数量×单价」分行印，
#   「2.00 x 2  ¥4.00」剔金额后剩 "2.00x2"，是**真正的明细金额行**（中文小票实测）。
_CODE_LINE = re.compile(r"^\d{6,}(?:JAN|CD)?$", re.I)


def _is_code_line(line: str) -> bool:
    """纯编码行（条码/JAN/编号 + 金额，无商品名）→ 不是独立明细行"""
    s = _MONEY_TOKEN.sub(" ", line.translate(_Z2H))
    s = re.sub(r"[\s.\-]", "", s)   # 条码可能印作 4903-0856-1414-7
    return bool(_CODE_LINE.match(s))


# Markdown 列表符号/标题符号：模型常把明细行写成「* 長ねぎ ¥195」，
# 符号不是内容的一部分，解析前剥掉（实测堂吉诃德：15 项明细全带 *，
# 剥之前被整行排除，导致 items=2、明细合计 237 的假警报）
_BULLET = re.compile(r"^[#*+\-]+\s+")

# 括号段：「（税額合計 ¥233）」这类辅助说明行，其中的"合計"等关键词
# 不是真正的汇总行（实测堂吉诃德：税額合計 233 被当成合計，真合計 3,152 反被跳过）
_PARENS = re.compile(r"[（(][^（）()]*[）)]")

# 税率组税额的匹配容差：按商品逐行四舍五入累计时与按总额计算会有个位数偏差。
# 内部单位为分 → ±2 元/円 = ±200 分
_TAX_TOLERANCE = 200


def _to_int(s: str) -> int:
    """金额统一换算成最小货币单位「分」存储：
    日元小票无小数（¥2,919 → 291900），中文小票到分（¥31.20 → 3120）。
    统一单位后，求和/容差/展示对两种货币习惯无感兼容。"""
    s = s.replace(",", "")
    if "." in s:
        yuan, _, fen = s.partition(".")
        return int(yuan) * 100 + int((fen + "00")[:2])
    return int(s) * 100


def _amounts_in_line(line: str, bare_int: bool = False) -> list[int]:
    """提取一行中的金额（保持出现顺序）；只认 ¥ 前缀或千分位逗号数字"""
    s = line.translate(_Z2H)
    # 先剔除"数量×单价"附属段，避免单价被当作商品金额。
    # 这样 (3個 × ¥780) 整行剔除后为空 → 不是明细行；
    # 「よだれ鶏 ¥2,340 (3個 × ¥780)」同一行时也只留下 2,340。
    s = _QTY_PRICE.sub(" ", s)
    for pat in _NOISE:
        s = pat.sub(" ", s)
    if _EXCLUDE_LINE.search(s):
        return []
    found = [_to_int(m.group().lstrip("¥￥ ")) for m in _MONEY_TOKEN.finditer(s)]
    # 粘连串剔除：¥ 后 8 位以上连续数字不可能是真实价格（见 _MERGED_TOKEN 注释）
    found = [v for v in found if v < _MERGED_LIMIT]
    # 中文小票兜底：没有 ¥/千分位 → 用"带 1~2 位小数的价格"当金额（日元单据不会走到这里）
    if not found:
        found = [_to_int(m.group(1)) for m in _DECIMAL_AMOUNT.finditer(s)]
    # 中文小票再兜底：连小数都没有的**裸整数金额**（见 _BARE_TRAILING 注释）
    if not found and bare_int and (m := _BARE_TRAILING.search(s)) and _NAME_CHAR.search(s):
        if len(_BARE_ANY_NUM.findall(s)) >= 2:
            found = [_to_int(m.group(1))]
    if not found and _BAG.search(s) and (m := _TRAILING_NUM.search(s)):
        found = [int(m.group(1)) * 100]   # レジ袋行的裸数字价格（分单位，见 _BAG 注释）
    if _DISCOUNT.search(s) and found:
        found[-1] = -found[-1]      # 値引/割引行记负数，参与求和
    return found


def _is_subtotal(line: str) -> bool:
    """小計行判据：含小計类关键词，且不是折扣行——
    「优惠总计 3.00」里的"总计"是小计关键词，但它是折扣汇总（华润万家实测），
    当成小计会把 3.00 当成小計、把 25.87 的明细衬托成 22.87 的假警报。"""
    s = line.translate(_Z2H)
    if not any(k in s for k in _SUB_KEYS):
        return False
    return not any(k in s for k in _DISCOUNT_KW)


def _is_total(line: str) -> bool:
    """
    合计行判据：含合计类关键词，且不是折扣行、不是日式付款行。
    - 「优惠合计 3.0」是折扣汇总，不是合计（永辉实测）；
    - 「お預り合計 ¥10,000」是付款额，不是合计（日式收据常见）。
    中文的「应收 1865.6 件数 27 实收 1865.60」这种一行多字段，**允许**判为合计：
    因为取值走 _amount_after（关键词后第一个金额），不会拿错数字。
    """
    s = line.translate(_Z2H)
    if not any(k in s for k in _TOT_KEYS):
        return False
    if any(k in s for k in _DISCOUNT_KW):
        return False
    if any(k in s for k in _JP_TENDER_KEYS):
        return False
    return True


def _amount_after(s: str, keywords: tuple[str, ...], bare_int: bool = False) -> int | None:
    """
    取「关键词之后出现的第一个金额」（分单位）。
    为什么需要它：中文 POS 常把多个字段印在同一行——
      「应收 1865.6 件数 27.0 实收 1865.60 找零 0.00」（永辉实测）
    按"行内第几个金额"取值必然串位（会拿到件数/找零），按关键词定位才稳。

    bare_int：中文单据的合计常印成裸整数（「品项合计 2390」「金额合计 1003」），
    此时取**关键词后最后一个**裸整数，而不是第一个——因为合计行上
    「合计 22 1508.22」这类写法里第一个数字是件数（sam2 实测）。
    """
    hits = [s.find(k) for k in keywords if k in s]
    if not hits:
        return None
    tail = s[min(hits):]
    # 先剔掉时间/日期等干扰：否则「收款时间：2024/7/27 23:00:21」的 21
    # 会被当成实收金额（来优品实测 实收 ¥21）
    for pat in _NOISE:
        tail = pat.sub(" ", tail)
    if _EXCLUDE_LINE.search(tail):
        return None
    if m := _MONEY_TOKEN.search(tail):
        return _to_int(m.group().lstrip("¥￥ "))
    if m := _DECIMAL_AMOUNT.search(tail):
        return _to_int(m.group(1))
    if bare_int:
        nums = re.findall(r"(?<![\d.])(\d{1,7})(?![\d.])", tail)
        if nums:
            return _to_int(nums[-1])
    return None


@dataclass
class Parsed:
    """从小票 Markdown 中解析出的自检要素"""
    items: list[tuple[int, str]] = field(default_factory=list)   # (金额, 原行)
    subtotal: int | None = None
    total: int | None = None
    tender: int | None = None        # お預り（付款额）
    change: int | None = None        # お釣り
    tax_bases: list[tuple[int, int]] = field(default_factory=list)   # (基数, 税率%)
    tax_amounts: list[tuple[int, int | None]] = field(default_factory=list)  # (税额, 税率|None)
    invoice_no: str | None = None
    summary_discounts: int = 0       # 小計之后、合計之前的値引/割引合计（负数）
    item_discounts: int = 0          # 明细区里的折扣行合计（负数）：折扣不是商品，不计入明细求和
    merged_prices: list[str] = field(default_factory=list)  # 疑似价格与条码粘连的原行


def _parse(md: str) -> Parsed:
    p = Parsed()
    # --- 抬头区：インボイス番号 ---
    if m := _INVOICE.search(md):
        p.invoice_no = m.group()

    # 裸整数金额只对"无假名的单据"启用：日文里的裸数字是点数/条码/数量，
    # 中文单据的裸数字才可能是钱（_BARE_TRAILING 注释）
    allow_bare_int = not _KANA.search(md)
    reached_summary = False   # 是否已进入小計/合計之后的汇总区
    in_return = False         # 是否进入"退货/退菜"段（段内行不计入本单明细）
    prev_item_had_amount = False   # 紧邻上一行是否给出了明细金额（用于识别"续行"）
    for raw in md.splitlines():
        line = raw.strip()
        if not line:
            continue
        # 剥掉列表/标题符号：「* 長ねぎ ¥195」→「長ねぎ ¥195」（--- 水平线无尾随空格不受影响）
        line = _BULLET.sub("", line).strip() or line
        # 括号段剥离后的"主干"：关键词匹配只在主干上做，
        # 避免「（税額合計 ¥233）」里的"合計"被误判为汇总行
        core = _PARENS.sub(" ", line)
        amts = _amounts_in_line(line, bare_int=allow_bare_int)

        if p.invoice_no is None and (m := _INVOICE.search(line)):
            p.invoice_no = m.group()

        # 退货/退菜段开始：段内商品是另一笔交易的回退，不计入本单明细
        # （中式餐饮单的固定结构，位置在点单明细之后、合计之前）
        if _RETURN_SECTION.search(core):
            in_return = True
            continue

        # 汇总区字段：一行可能印多个字段，所以**各自独立取值**（不 continue），
        # 取值一律走 _amount_after（关键词后第一个金额）：
        #   「合計 ¥3,152 (内消費税 ¥233)」→ 3,152 而不是税额
        #   「应收 1865.6 … 实收 1865.60 找零 0.00」→ 三个字段各归各位
        got_summary = False
        if p.subtotal is None and _is_subtotal(core):
            if (v := _amount_after(core, _SUB_KEYS, allow_bare_int)) is not None:
                p.subtotal = v
                got_summary = True
        if p.total is None and _is_total(core):
            if (v := _amount_after(core, _TOT_KEYS, allow_bare_int)) is not None:
                p.total = v
                got_summary = True
        if p.tender is None and any(k in core for k in _TENDER_KEYS):
            if (v := _amount_after(core, _TENDER_KEYS, allow_bare_int)) is not None:
                p.tender = v
                got_summary = True
        if p.change is None and any(k in core for k in _CHANGE_KEYS):
            if (v := _amount_after(core, _CHANGE_KEYS, allow_bare_int)) is not None:
                p.change = v
                got_summary = True
        if got_summary:
            reached_summary = True
            continue

        # 小計之后、合計之前的値引/割引行（会员折扣等，价格常为裸数字无 ¥）：
        # 解释 小計 与 お買上計 的差额。实测 711_3：「マイバーグ持参値引 -3」，
        # 3,252 − 3 = 3,249 完全自洽，不认识这行就会误报"小計与合計不符"。
        # 注意：只在汇总区生效——明细区的値引行（▲値引 -208）不能按此处理，
        # 否则明细和会因缺价格行而误报（711_3 的商品价格印作 *421，无法作为金额提取）。
        if reached_summary and p.total is None and any(k in core for k in _DISCOUNT_KW):
            # 优先用已提取的金额（_amounts_in_line 已把 优惠/値引 行记负数）：
            # 「会员优惠 -¥1.00」→ -100 分；行尾裸数字路径兜底日式「値引 -3」→ -300 分
            if amts:
                p.summary_discounts += amts[-1]
            elif m := _SUMMARY_DISCOUNT_NUM.search(line):
                p.summary_discounts += int(m.group(1)) * 100
            continue

        # 税率组（在原始行上匹配：税行以「8%」或「（」开头，不受符号剥离影响）
        for m in _TAX_BASE.finditer(line):
            p.tax_bases.append((_to_int(m.group(2)), int(m.group(1))))
        for m in _TAX_AMT_RATE.finditer(line):
            p.tax_amounts.append((_to_int(m.group(2)), int(m.group(1))))
        for m in _TAX_AMT_PAREN.finditer(line):
            rate = int(m.group(1)) if m.group(1) else None
            p.tax_amounts.append((_to_int(m.group(2)), rate))

        # 明细行：小計之前、含金额、非标题行、非纯编码行（干扰行已在 _amounts 内排除）
        # 取行内**最后一个**金额：明细行是「商品名 …… 金额」右对齐，金额在行尾；
        # 行中的 0.82kg / 7.96/kg 是数量单价，不能当金额。
        if not reached_summary and not in_return and amts and not line.startswith("#") and not _is_code_line(line):
            # 一件商品常被印成两行（名称行 + 编码/数量行），中文 POS 尤其如此：
            #   「1.程购物袋3角 0.30」
            #   「2500206 1*0.30 0.30」   ← 编码+数量×单价+金额，与上一行金额重复
            # 若紧邻上一行已给出金额、而本行没有商品名（纯数字/编码），本行是上一件的
            # 注记，不能再计入一次（否则明细合计恰好翻倍）。
            # 反例（必须保留）：名称行不带价格、下一行才给价格时（「农夫山泉…」+「2.00 x 2 ¥4.00」），
            # 因为 prev_item_had_amount 为 False，价格行照常计入。
            if prev_item_had_amount and not _NAME_CHAR.search(line):
                prev_item_had_amount = True   # 连续多行注记也只跳过金额，状态保持
            elif _DISCOUNT.search(line):
                # 折扣不是商品：单独累计，不参与明细求和。
                # 否则「优惠合计 3.0」「-商品优惠 3.0」会被当成两条商品把合计拉低（永辉实测）
                p.item_discounts += amts[-1]
                prev_item_had_amount = True
            else:
                p.items.append((amts[-1], line))
                prev_item_had_amount = True
        elif not reached_summary and not amts:
            prev_item_had_amount = False      # 名称行（无金额）→ 下一行的价格应计入
        # 粘连串检测：¥ 后 8 位以上连续数字（价格与 JAN 条码等粘连）——
        # 金额已从求和剔除，这里记录原行用于提示输出质量缺陷
        if _MERGED_TOKEN.search(line):
            p.merged_prices.append(line)
    return p


# ---------------------------------------------------------------------------
# 校验规则
# ---------------------------------------------------------------------------

def _check_invoice(no: str) -> bool:
    """
    適格請求書登録番号校验位（国税庁法人番号算法）：
    13 位去掉末位校验位后，前 7 位乘权重 6,5,4,3,2,10,9 之和为 p，
    第 8~12 位乘权重 8,7,6,5,4 之和为 q，(9p+q) 的个位应等于校验位。
    """
    digits = [int(c) for c in no[1:]]
    body, check = digits[:12], digits[12]
    w1 = (6, 5, 4, 3, 2, 10, 9)
    w2 = (8, 7, 6, 5, 4)
    p = sum(d * w for d, w in zip(body[:7], w1))
    q = sum(d * w for d, w in zip(body[7:], w2))
    return (9 * p + q) % 10 == check


def _fmt(v: int) -> str:
    """内部单位为分，展示时还原：整百显示整数（日元习惯），否则两位小数（人民币习惯）"""
    if v % 100 == 0:
        return f"¥{v // 100:,}"
    return f"¥{v / 100:,.2f}"


def validate_markdown(md: str) -> dict:
    """
    执行全部自检规则，返回:
    { ok: bool, checks_run: int, issues: [{rule, level, message}], parsed: {...} }
    checks_run=0 表示无法解析出任何自检要素（前端据此不展示自检横幅）。
    """
    issues: list[dict] = []

    def add(rule: str, level: str, message: str) -> None:
        issues.append({"rule": rule, "level": level, "message": message})

    try:
        p = _parse(md)
    except Exception:  # noqa: BLE001 —— 解析失败绝不影响主流程
        logger.debug("自检解析异常，跳过本轮校验", exc_info=True)
        return {"ok": True, "checks_run": 0, "issues": [], "parsed": {}}

    checks = 0

    # R1 明细和 = 小計（warning：明细级矛盾多为转写格式问题，见模块头"分级策略"）
    if p.items and p.subtotal is not None:
        checks += 1
        s = sum(v for v, _ in p.items)
        # 三种记账习惯都认（实测各占一类）：
        #   s == 小計                       折扣已在明细价里扣掉（嘉园、711_3）
        #   s == 小計 + 汇总区折扣           折扣印在小計之后（711_3 的変体）
        #   s == 小計 - 明细区折扣           明细是原价、折扣单独列在明细区
        if s not in (p.subtotal, p.subtotal + p.summary_discounts,
                     p.subtotal - p.item_discounts):
            add("items_sum", "warning",
                f"明细 {len(p.items)} 行逐项相加得 {_fmt(s)}，与小計 {_fmt(p.subtotal)} 差 {_fmt(abs(p.subtotal - s))}："
                f"可能某行金额认错或漏行（相加结果是推算值，单据上不会直接印出），请对照原图核对明细区")
        # 额外负数检查：明细里出现负数且不是値引场景 → 可疑
        negs = [(v, l) for v, l in p.items if v < 0]
        for v, l in negs:
            add("negative_item", "warning", f"明细出现负金额（{l}），若原图并非値引请核对")
    elif p.items and p.total is not None and not p.tax_amounts and not _KANA.search(md):
        # 中文小票常只印「应收/总计」而不印小计（永辉实测：24 项明细 → 应收 1865.6），
        # 此时退而比较「明细和 vs 总额」。
        # 两道保险：① 有税行不比（日式合計含税、明细不含税，比了必假警报）；
        # ② 单据里有假名就不比（日式收据的明细常印作「*105」无法提取，比了也是噪声——SAM 实测回归）。
        checks += 1
        s = sum(v for v, _ in p.items)
        if s not in (p.total, p.total - p.summary_discounts, p.total - p.item_discounts):
            add("items_sum", "warning",
                f"明细 {len(p.items)} 行逐项相加得 {_fmt(s)}，与总额 {_fmt(p.total)} 差 {_fmt(abs(p.total - s))}："
                f"可能某行金额认错或漏行（相加结果是推算值，单据上不会直接印出），请对照原图核对明细区")

    # 输出质量提示：价格与条码粘连（¥ 后 8 位以上连续数字）——
    # 金额已从求和剔除（剔除后若与小計吻合即证明只有粘连、金额本身没错），
    # 但输出文本是有缺陷的转写，用户应知情并考虑重新识别/人工修正
    if p.merged_prices:
        add("merged_price", "warning",
            f"检测到 {len(p.merged_prices)} 处疑似价格与条码/编号粘连"
            f"（如「{p.merged_prices[0][:30]}」），已从明细求和中剔除；建议对照原图或重新识别")

    # 税额合计：带税率的各组分税额优先（「税額合計」这类无税率的总税行是它们的合计，
    # 同时计入会重复累加——实测堂吉诃德：8%税額 233 + 税額合計 233 = 466 的假差额）
    per_rate = [v for v, r in p.tax_amounts if r is not None]
    no_rate = [v for v, r in p.tax_amounts if r is None]
    tax_sum = sum(per_rate) if per_rate else sum(no_rate)

    # R2 小計(±汇总区値引)(±税) = 合計。三种成立形态：
    #   内税无折扣：小計 == 合計（711_2）
    #   外税：小計 + 消費税 == 合計（堂吉诃德 2919+233=3152）
    #   小計后有値引：小計 + 値引 == 合計（711_3：3252-3=3249）
    if p.subtotal is not None and p.total is not None:
        checks += 1
        eff_sub = p.subtotal + p.summary_discounts
        if eff_sub != p.total and (not tax_sum or eff_sub + tax_sum != p.total):
            disc_note = f"（已扣汇总区値引 {_fmt(p.summary_discounts)}）" if p.summary_discounts else ""
            add("subtotal_total", "error",
                f"小計 {_fmt(p.subtotal)}{disc_note} 与合計 {_fmt(p.total)} 不符"
                + (f"（含税差额 {_fmt(p.total - eff_sub)}，税行合计 {_fmt(tax_sum)}）" if tax_sum else "（且未解析到税行）"))

    # R3 每个税率组：税额 ≈ floor(基数×rate/100)（外税）或 floor(基数×rate/(100+rate))（内税）。
    # 两种记账方式按哪种打印的都有，命中任意一种即在容差内通过。
    for base, rate in p.tax_bases:
        same_rate = [v for v, r in p.tax_amounts if r == rate]
        if not same_rate:
            continue
        checks += 1
        # 日元税额印到整円：先取整円部分（base//100）再算税，避免分位噪声进入 floor
        expect_out = (base // 100) * rate // 100 * 100          # 外税：税额外加
        expect_in = (base // 100) * rate // (100 + rate) * 100  # 内税：税额含在基数里
        actual = same_rate[0]
        if min(abs(actual - expect_out), abs(actual - expect_in)) > _TAX_TOLERANCE:
            # warning 而非 error：真实小票的税率区也可能印得不自洽
            # （無名軒2 实证：纸面即印 341/338/308/305，与 7,840 无法调和，转写无误）
            add("tax_group", "warning",
                f"{rate}% 税组：対象額 {_fmt(base)} 与税額 {_fmt(actual)} 按外税/内税公式"
                f"均对不上（约 {_fmt(expect_out)} 或 {_fmt(expect_in)}）。"
                f"部分小票实物印刷本身即不自洽，请对照原图确认")

    # R4 お預かり − 合計 = お釣り
    if p.tender is not None and p.total is not None:
        checks += 1
        diff = p.tender - p.total
        # 抹零：中式收银常把零头抹掉，实收略少于应收（广州大排挡实测 1235.4 → 1235，
        # 单据另印「圆整差额 0.4」）。1 元以内的差额不是认错，不报。
        if diff < -100:
            add("tender", "error",
                f"お預り {_fmt(p.tender)} 小于合計 {_fmt(p.total)}，付款额被认错")
        elif diff >= 0 and p.change is not None and diff != p.change:
            add("change", "error",
                f"お預り {_fmt(p.tender)} − 合計 {_fmt(p.total)} = {_fmt(diff)}，"
                f"但お釣り为 {_fmt(p.change)}")

    # R5 インボイス番号校验位（warning 而非 error：
    # 個人事業者の登録番号不适用法人番号校验算法，实测無名軒（真实个人店）即此情形）
    if p.invoice_no:
        checks += 1
        if not _check_invoice(p.invoice_no):
            add("invoice_check", "warning",
                f"登録番号 {p.invoice_no} 未通过法人番号校验位：可能 OCR 认错某位数字；"
                f"但個人事業者的番号不适用此算法，请对照原图确认")

    parsed_summary = {
        "items": len(p.items),
        "subtotal": p.subtotal,
        "total": p.total,
        "tender": p.tender,
        "change": p.change,
        "tax_groups": len(p.tax_bases),
        "invoice_no": p.invoice_no,
    }
    # ok 只表达"数字是否自洽"：warning（如個人事業者番号不适用校验位算法）
    # 不代表识别有错，若也算失败会让整张票被误标为可疑。
    has_error = any(i["level"] == "error" for i in issues)
    return {
        "ok": not has_error,
        "checks_run": checks,
        "issues": issues,
        "parsed": parsed_summary,
    }
