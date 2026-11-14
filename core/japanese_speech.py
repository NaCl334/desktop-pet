"""Speech-only Japanese spelling; never rewrite displayed replies."""
import re
import unicodedata
from pathlib import PureWindowsPath


_DIGITS = "零一二三四五六七八九"
_JAPANESE_DIGITS = ("ゼロ", "イチ", "ニ", "サン", "ヨン", "ゴ", "ロク", "ナナ", "ハチ", "キュウ")
_KANJI = "〇零一二三四五六七八九十百千万億兆"
_UNITS = ("GB", "MB", "TB", "KB", "GHz", "MHz", "kHz", "Hz", "FPS")
_FREQUENCY_READINGS = {"GHz": "ギガヘルツ", "MHz": "メガヘルツ", "kHz": "キロヘルツ", "Hz": "ヘルツ"}
_MODEL_LETTER_READINGS = {
    "A": "エー",
    "B": "ビー",
}
_MODEL_NUMBER_READINGS = (
    "ゼロ", "ワン", "ツー", "スリー", "フォー",
    "ファイブ", "シックス", "セブン", "エイト", "ナイン",
)
_ORDINAL_READINGS = (
    "第一", "第二", "第三", "第四", "第五",
    "第六", "第七", "第八", "第九", "第十",
)
_LIST_ORDINAL = re.compile(
    r"(?m)(?P<prefix>^|[。！？!?、,;；]\s*|\n\s*)"
    r"(?P<number>[0-9]+|[〇零一二三四五六七八九十]+)"
    r"(?P<separator>\s*[.．](?=\s)|、|，|,|;|；|。(?!。))\s*"
)
_TOKEN = re.compile(
    r"(?<![A-Za-z0-9_])(?:[A-Za-z][A-Za-z0-9]*(?:[-.]\d+)*"
    r"|\d+(?:,\d{3})*(?:\.\d+)*(?:[ \t]*(?:%|[A-Za-z]+))?)(?![A-Za-z0-9_])"
)
_FILE = re.compile(
    r"(?<![A-Za-z0-9_.-])(?:[A-Za-z]:[\\/])?"
    r"(?:[A-Za-z0-9_.-]+[\\/])*[A-Za-z0-9_-]+"
    r"\.(?:json|py|yaml|yml|txt|exe|vbs|md|toml|ps1|bat|sh|csv|log)(?![A-Za-z0-9_.-])",
    re.I,
)
_COMMAND = re.compile(
    r"^(?:python(?:3|\.exe)?|pip|git|npm|pnpm|node|powershell|pwsh|cmd|curl)\s+", re.I
)

# GPT-SoVITS 会把「ウィンドウ」的起始音节合成为近似「民」的声音，
# 导致日文 ASR 稳定回听成「民道」。真实 A/B 中同音写法「ウインドウ」
# 可以稳定还原为「ウィンドウ」。这里只规范送入合成器的语音副本。
_TTS_PRONUNCIATION_REPLACEMENTS = (
    ("ウィンドウ", "ウインドウ"),
)

def readable_references(text):
    def inline(match):
        content = match.group(1).strip()
        if _COMMAND.match(content):
            return content.split()[0] + " コマンド"
        if _FILE.fullmatch(content) or (
            PureWindowsPath(content).suffix.lower() in (".json", ".py", ".txt", ".yaml")
        ):
            return PureWindowsPath(content).stem + " ファイル"
        return content

    text = re.sub(r"```.*?```", " コード ", str(text or ""), flags=re.S)
    text = re.sub(r"`([^`\n]+)`", inline, text)
    return _FILE.sub(lambda m: PureWindowsPath(m.group()).stem + " ファイル", text)


def number_reading(value, digit_by_digit=False):
    value = str(value).replace(",", "")
    if not re.fullmatch(r"[0-9]+", value):
        return value
    if digit_by_digit or (len(value) > 1 and value.startswith("0")) or len(value) > 16:
        return "".join(_DIGITS[int(char)] for char in value)
    number = int(value)
    if number == 0:
        return _DIGITS[0]
    result = []
    for unit, label in ((10**12, "兆"), (10**8, "億"), (10000, "万"),
                        (1000, "千"), (100, "百"), (10, "十")):
        count, number = divmod(number, unit)
        if count:
            if count > 1 or unit >= 10000:
                result.append(number_reading(count))
            result.append(label)
    if number:
        result.append(_DIGITS[number])
    return "".join(result)


def quantity_digits(token):
    if not any(char in "十百千万億兆" for char in token):
        return "".join(str(int(unicodedata.numeric(char))) for char in token)
    total, section, number = 0, 0, 0
    for char in token:
        value = int(unicodedata.numeric(char))
        if value < 10:
            number = number * 10 + value
        elif value < 10000:
            section += (number or 1) * value
            number = 0
        else:
            total += (section + number or 1) * value
            section, number = 0, 0
    return str(total + section + number)


def _numeric_reading(value, digits=False):
    parts = value.split(".")
    return "点".join(number_reading(part, digits or index > 0)
                    for index, part in enumerate(parts))


def _model_number_reading(value):
    """Read an identifier digit-by-digit, including kanji-number spellings."""
    parts = []
    for part in str(value).split("."):
        digits = quantity_digits(part)
        if not digits.isdigit():
            return None
        # Real GPT-SoVITS/ASR probes show that Japanese commas are often
        # ignored between a Latin letter and the following digits. Explicit
        # spaces create a stable pause while preserving every digit.
        parts.append(" ".join(_MODEL_NUMBER_READINGS[int(char)] for char in digits))
    return " ポイント ".join(parts)


def _nearby_prefix(text, start):
    return re.split(r"[。！？!?\n、,;；]", text[max(0, start - 32):start])[-1]


def _list_ordinal_reading(match):
    if match.group("separator") in (",", "，") and re.match(
        r"\d", match.string[match.end():]
    ):
        return match.group()
    value = quantity_digits(match.group("number"))
    if not value.isdigit() or not 1 <= int(value) <= 10:
        return match.group()
    return match.group("prefix") + _ORDINAL_READINGS[int(value) - 1] + "に、"


def normalize_speech(text):
    text = unicodedata.normalize("NFKC", readable_references(text))
    text = _LIST_ORDINAL.sub(_list_ordinal_reading, text)
    # 「3000/3000」中的斜杠不是可省略的装饰。保留斜杠语义，但在两侧
    # 放稳定停顿，避免 TTS 把两组相同数字连成一个数；符号本身留给
    # 校验文本，朗读副本中换成明确的“対”。
    text = re.sub(
        r"(?<=\d)\s*/\s*(?=\d)",
        "、対、",
        text,
    )

    def replace(match):
        token = match.group()
        before = _nearby_prefix(text, match.start())
        after = text[match.end():match.end() + 16]
        if token.lower() == "live2d":
            return "Live Two D"
        if token in _FREQUENCY_READINGS:
            return _FREQUENCY_READINGS[token]
        if token in _UNITS or re.fullmatch(r"[A-Z]{2,8}", token):
            return " ".join(token)
        model = re.fullmatch(r"([A-Z]{1,8})-?(\d+(?:\.\d+)*)", token)
        if model:
            return " ".join(model[1]) + " " + _numeric_reading(model[2])
        number = re.fullmatch(r"(\d+(?:,\d{3})*(?:\.\d+)*)(?:[ \t]*(%|[A-Za-z]+))?", token)
        if not number:
            return token
        value, suffix = number[1], number[2] or ""
        if not suffix and re.match(r"(?:ギガ|パーセント)", after):
            return token
        # G is ambiguous: only an explicit local capacity context gives it a unit.
        if suffix == "G":
            capacity = re.search(r"メモリ(?:ー)?|ストレージ|容量|RAM|内存|存储|記憶", before, re.I)
            network = re.search(r"通信|回線|ネットワーク|規格|网络|世代", before)
            # 容量语境既可能写成「8Gの容量」，也可能写成
            # 「8Gという容量」；两者都必须读作「8ギガ」。
            if not capacity and re.match(
                r"(?:(?:という|の)?(?:メモリ|ストレージ|容量|内存|存储))",
                after,
            ):
                capacity = True
            return value + ("ギガ" if capacity and not network else " G")
        if suffix and suffix not in _UNITS and suffix != "%":
            return token
        is_id = bool(re.search(r"(?:番号|暗証番号|パスワード|ID|コード|编号|密码)[はがの:： ]*$", before))
        # Follow the user's digit-wise year preference; ordinary counts stay cardinal.
        is_year = len(value) == 4 and bool(re.match(r"年(?!間|分|生)", after))
        reading = _numeric_reading(value, digits=is_id or is_year)
        # 编号中的前导零需要逐位朗读，并在数字之间留出短停顿；否则
        # 日文 ASR 很容易把「ゼロゼロ一二三」压成「003」。
        if is_id and len(value) > 1 and value.startswith("0"):
            # 编号必须逐位朗读，并用空格形成稳定短停顿；逗号在部分
            # TTS 声学模型中会被当成列表标点，可能造成末位数字丢失。
            reading = " ".join(_JAPANESE_DIGITS[int(char)] for char in value)
        if suffix == "%":
            return reading + "パーセント"
        if suffix in _FREQUENCY_READINGS:
            return reading + _FREQUENCY_READINGS[suffix]
        if suffix:
            return reading + " " + " ".join(suffix)
        return reading

    normalized = re.sub(r"\s{2,}", " ", _TOKEN.sub(replace, text)).strip()
    identifier_digits = {
        "〇": "ゼロ", "零": "ゼロ", "一": "イチ", "二": "ニ",
        "三": "サン", "四": "ヨン", "五": "ゴ", "六": "ロク",
        "七": "ナナ", "八": "ハチ", "九": "キュウ",
    }

    def expand_translated_identifier(match):
        return match.group(1) + " ".join(identifier_digits[digit] for digit in match.group(2))

    normalized = re.sub(
        r"((?:暗証番号|パスワード|番号|ID|コード|编号|密码)[がの:： ]*)"
        r"([〇零一二三四五六七八九]{3,})",
        expand_translated_identifier,
        normalized,
    )
    for source, spoken in _TTS_PRONUNCIATION_REPLACEMENTS:
        normalized = normalized.replace(source, spoken)
    return normalized


def explicit_model_pronunciation(text):
    """Use unambiguous letter and English-digit readings for model IDs."""
    # Only short Latin model identifiers get the explicit letter reading.
    # This keeps ordinary English words and unit abbreviations unchanged.
    def model_reading(match):
        letter, number = match.group(1), match.group(2)
        number_reading = _model_number_reading(number)
        if number_reading is None:
            return match.group()
        return (
            _MODEL_LETTER_READINGS.get(letter.upper(), letter)
            + " " + number_reading
        )

    return re.sub(
        r"(?<![A-Za-z0-9])([A-Z])\s*[-.]?\s*"
        r"([0-9" + _KANJI + r"]+(?:[.][0-9" + _KANJI + r"]+)*)"
        r"(?![A-Za-z0-9])",
        model_reading,
        str(text or ""),
    )


def retry_pronunciation(text, model_identifiers=False):
    """Disambiguate synthesis readings without changing the verification target."""
    from core.japanese_reading import japanese_reading

    # Repeated totals such as「十二のうち十二」are easily decoded as
    #「12のうち10」when spoken as one uninterrupted phrase. Add short
    # punctuation pauses around the comparison while preserving both values.
    repeated_quantity = re.compile(
        r"(?P<number>[0-9〇零一二三四五六七八九十百千万億兆]+)"
        r"\s*のうち\s*(?P=number)"
    )
    normalized = repeated_quantity.sub(
        lambda match: "{number}、のうち、{number}".format(
            number=match.group("number")
        ),
        str(text or ""),
    )

    def counter_reading(match):
        source = match.group()
        reading = japanese_reading(source)
        if reading and re.fullmatch(r"[ァ-ヶー]+", reading):
            return reading
        return source

    normalized = re.sub(
        r"(?<![A-Za-z0-9" + _KANJI + r"])[0-9" + _KANJI + r"]+"
        r"(?:件|個|枚|本|回|台)(?![ァ-ヶ一-龥])",
        counter_reading,
        normalized,
    )
    if not model_identifiers:
        return normalized
    # Keep the retry reading identical to the first attempt. The explicit
    # model reading already separates the letter and every digit with plain
    # spaces; replacing that space with an ideographic comma was measured to
    # make ASR merge "エー、ワン ツー" into wrong identifiers such as
    # "B-1-2", so retries must not reintroduce the separator.
    return explicit_model_pronunciation(normalized)
