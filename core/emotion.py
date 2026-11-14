import re


DEFAULT_STATE = "idle"

PRESET_EMOTIONS = {
    "pleased": {"label": "愉悦", "keywords": ("愉悦", "惬意", "心情不错")},
    "happy": {"label": "高兴", "keywords": ("高兴", "开心", "快乐", "太好了", "好耶")},
    "excited": {"label": "兴奋", "keywords": ("兴奋", "激动", "太激动了")},
    "affectionate": {"label": "喜欢", "keywords": ("喜欢你", "好喜欢", "亲近", "爱你")},
    "sparkling_eyes": {"label": "眼睛发光", "keywords": ("眼睛发光", "眼睛一亮", "两眼放光", "星星眼")},
    "expectant": {"label": "期待", "keywords": ("期待", "盼望", "迫不及待")},
    "teasing": {"label": "捉弄", "keywords": ("捉弄", "逗你", "骗你的", "恶作剧")},
    "smirking": {"label": "坏笑", "keywords": ("坏笑", "不怀好意地笑", "嘿嘿")},
    "confident": {"label": "自信", "keywords": ("自信", "有把握", "包在我身上", "相信自己")},
    "smug": {"label": "得瑟", "keywords": ("得瑟", "得意", "厉害吧", "佩服我吧")},
    "shy": {"label": "害羞", "keywords": ("害羞", "脸红", "害臊")},
    "embarrassed": {"label": "难为情", "keywords": ("难为情", "不好意思", "羞于", "脸热")},
    "fake_crying": {"label": "假哭", "keywords": ("假哭", "装哭", "呜呜", "嘤嘤")},
    "speechless": {"label": "无语", "keywords": ("无语", "无言以对", "无话可说")},
    "awkward": {"label": "尴尬", "keywords": ("尴尬", "窘迫", "冷场")},
    "unexpected": {"label": "意外", "keywords": ("意外", "没想到")},
    "surprised": {"label": "惊讶", "keywords": ("惊讶", "吃惊", "真的假的")},
    "shocked": {"label": "震惊", "keywords": ("震惊", "惊呆", "难以置信")},
    "thinking": {"label": "认真", "keywords": ("认真", "仔细", "专注", "分析", "研究", "推理")},
    "serious": {"label": "严肃", "keywords": ("严肃", "郑重", "严正")},
    "confused": {"label": "疑惑", "keywords": ("疑惑", "困惑", "不懂", "不明白", "怎么回事")},
    "sighing": {"label": "叹气", "keywords": ("叹气", "唉", "叹了口气")},
    "compromising": {"label": "妥协", "keywords": ("妥协", "听你的吧", "依你了")},
    "displeased": {"label": "不满", "keywords": ("不满", "不乐意", "不满意")},
    "angry": {"label": "生气", "keywords": ("生气", "可恶", "气人")},
    "furious": {"label": "愤怒", "keywords": ("愤怒", "怒不可遏", "气死我了", "火冒三丈")},
    "disappointed": {"label": "失落", "keywords": ("失落", "失望", "空落落")},
    "dejected": {"label": "沮丧", "keywords": ("沮丧", "垂头丧气", "灰心")},
    "sad": {"label": "伤心", "keywords": ("伤心", "难过", "悲伤")},
    "crying": {"label": "哭泣", "keywords": ("哭泣", "流泪", "泪流满面", "忍不住哭", "哭了")},
    "disdainful": {"label": "嫌弃", "keywords": ("嫌弃", "嫌脏", "嫌烦")},
    "disgusted": {"label": "厌恶", "keywords": ("厌恶", "恶心", "反感", "真讨厌")},
    "provoking": {"label": "挑衅", "keywords": ("挑衅", "不服来战", "你敢吗")},
    "uneasy": {"label": "不安", "keywords": ("不安", "忐忑", "心神不宁")},
    "worried": {"label": "担忧", "keywords": ("担忧", "担心", "忧虑", "放心不下")},
    "flustered": {"label": "慌张", "keywords": ("慌张", "慌了", "手忙脚乱", "来不及", "糟了")},
    "afraid": {"label": "害怕", "keywords": ("害怕", "恐惧", "好可怕", "救命")},
    "despairing": {"label": "绝望", "keywords": ("绝望", "万念俱灰", "毫无希望")},
    "helpless": {"label": "无奈", "keywords": ("无奈", "没办法", "拿你没办法", "无可奈何")},
}

# Keep established keys stable; retired presets remain readable as custom emotions.
EMOTION_GROUPS = {
    "正向": tuple(list(PRESET_EMOTIONS)[:10]),
    "中性": tuple(list(PRESET_EMOTIONS)[10:23]),
    "负向": tuple(list(PRESET_EMOTIONS)[23:]),
}

LEGACY_EMOTIONS = {
    "playful": {"label": "调皮", "keywords": ("调皮",)},
    "passionate": {"label": "情动", "keywords": ("情动", "动情", "怦然")},
    "low_battery": {"label": "低电量", "keywords": ("低电量", "没电", "电量低", "电池")},
    "found_good_thing": {"label": "发现好东西", "keywords": ("发现好东西", "找到好东西")},
}
EMOTION_LABEL_ALIASES = {"担心": "worried", "发光": "sparkling_eyes"}
STATE_KEYWORDS = {
    key: definition["keywords"]
    for key, definition in {**PRESET_EMOTIONS, **LEGACY_EMOTIONS}.items()
}

# Explicit substitutions, never transitive links or arbitrary same-group matches.
EMOTION_FALLBACKS = {
    "pleased": ("happy",),
    "happy": ("pleased",),
    "excited": ("happy", "pleased"),
    "affectionate": ("pleased",),
    "sparkling_eyes": ("excited", "happy"),
    "expectant": ("pleased",),
    "teasing": ("smirking",),
    "smirking": ("teasing",),
    "confident": (),
    "smug": ("confident",),
    "shy": ("embarrassed",),
    "embarrassed": ("shy", "awkward"),
    "fake_crying": (),
    "speechless": ("helpless",),
    "awkward": ("embarrassed",),
    "unexpected": ("surprised",),
    "surprised": ("unexpected",),
    "shocked": ("surprised", "unexpected"),
    "thinking": (),
    "serious": (),
    "confused": (),
    "sighing": ("helpless",),
    "compromising": ("helpless",),
    "displeased": ("angry",),
    "angry": ("displeased",),
    "furious": ("angry", "displeased"),
    "disappointed": ("dejected", "sad"),
    "dejected": ("disappointed", "sad"),
    "sad": ("disappointed",),
    "crying": ("sad", "dejected"),
    "disdainful": ("disgusted",),
    "disgusted": ("disdainful",),
    "provoking": (),
    "uneasy": ("worried",),
    "worried": ("uneasy",),
    "flustered": ("uneasy", "worried"),
    "afraid": ("uneasy",),
    "despairing": ("sad",),
    "helpless": ("sighing",),
}


def emotion_fallback_chain(state):
    return tuple(dict.fromkeys((state, *EMOTION_FALLBACKS.get(state, ()), DEFAULT_STATE)))


def _expressed_text(text):
    text = re.sub(r"\[(?:emotion|intent):[^\]]*\]", "", text or "")
    text = re.sub(r"“[^”]*”|「[^」]*」|『[^』]*』|\"[^\"]*\"", "", text)
    clauses = re.split(r"[，。！？；\n!?;]", text)
    return "，".join(
        clause for clause in clauses
        if not re.match(r"\s*(?:如果|假如|比如|例如|你(?:们)?|他(?:们)?|她(?:们)?|用户|角色)", clause)
        or re.search(r"不理我|理理我|陪我|欺负我", clause)
    )


def select_state(text, available_states=None, state_keywords=None):
    """Conservative keyword fallback; a valid model marker takes priority upstream."""
    text = _expressed_text(text)
    available = set(STATE_KEYWORDS if available_states is None else available_states)
    keywords_by_state = dict(STATE_KEYWORDS)
    if isinstance(state_keywords, dict):
        for state, keywords in state_keywords.items():
            if isinstance(keywords, str):
                keywords = (keywords,)
            elif not isinstance(keywords, (list, tuple, set)):
                continue
            keywords_by_state[state] = tuple(
                str(keyword).strip() for keyword in keywords if str(keyword).strip()
            )
    scores = {}
    for state, keywords in keywords_by_state.items():
        if state not in available:
            continue
        hits = []
        for keyword in keywords:
            for match in re.finditer(re.escape(keyword), text):
                prefix = text[max(0, match.start() - 5):match.start()]
                if keyword == "得意" and prefix.endswith("觉"):
                    continue
                if not re.search(r"(?:不(?:是|会|再|要|用|必|觉得|感到)?|没有(?:觉得|感到)?|并非|别|无需|不用感到)$", prefix):
                    hits.append(len(keyword))
        scores[state] = max(hits, default=0)
    # An unmarked sob alone does not distinguish pretend crying from real sadness.
    if "fake_crying" in scores and not re.search(
        r"假哭|装哭|(?:呜呜|嘤嘤).*(?:不理我|理理我|陪我|欺负我)|撒娇|逗你", text
    ):
        scores["fake_crying"] = 0
    intensity = {"crying": 3, "furious": 3, "shocked": 3, "excited": 3,
                 "fake_crying": 2, "dejected": 2}
    best = max(scores, key=lambda key: (scores[key], intensity.get(key, 0)),
               default=DEFAULT_STATE)
    return best if scores.get(best, 0) else DEFAULT_STATE


def state_from_model_result(result, available_states=None):
    """Parse the reply's emotion marker, including an explicit neutral idle."""
    available = set(STATE_KEYWORDS if available_states is None else available_states)
    available.add(DEFAULT_STATE)
    match = re.search(r"\[emotion:([a-zA-Z0-9_]+)\]", result or "")
    if match and match.group(1) in available:
        return match.group(1)
    return None
