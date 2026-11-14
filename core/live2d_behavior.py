"""Event-based choices, independent of the Live2D rendering frame rate."""

import math
import random
import re
from core.emotion import PRESET_EMOTIONS, emotion_fallback_chain


INTENT_LABELS = {
    "idle": "待机",
    "agree": "赞同",
    "disagree": "否定",
    "inspect": "仔细观察",
    "thinking": "思考",
    "greet": "打招呼",
    "comfort": "安慰",
    "neutral": "普通答复",
}

INTENT_MOTION_GROUPS = {
    "idle": ("blink", "wink", "look", "glance", "眨眼", "观察", "左右看"),
    "agree": ("nod", "agree", "点头", "赞同"),
    "disagree": ("shake", "disagree", "摇头", "否定"),
    "inspect": ("lean", "leanforward", "forwardlean", "inspect", "前倾", "仔细观察"),
    "thinking": ("think", "thinking", "glanceup", "tilt", "思考"),
    "greet": ("wave", "greet", "打招呼", "挥手"),
    "comfort": ("comfort", "安慰"),
    "neutral": (),
}

EMOTION_MOTION_GROUPS = {
    "idle": ("look", "glance", "tilt", "wink", "观察", "侧倾"),
    "happy": ("happy", "joy", "swayforward", "wink", "开心", "高兴"),
    "excited": ("excited", "celebrate", "jump", "hop", "swayforward", "兴奋"),
    "found_good_thing": ("peekforward", "leanforward", "发现好东西"),
    "playful": ("playful", "jump", "hop", "tilt", "wink", "swayforward", "调皮"),
    "shy": ("shy", "bashful", "wink", "害羞"),
    "embarrassed": ("embarrassed", "bashful", "难为情"),
    "passionate": ("bashful", "peekforward", "情动"),
    "confused": ("confused", "tilt", "疑惑"),
    "thinking": ("think", "thinking", "glanceup", "tilt", "思考"),
    "speechless": ("glancedown", "leanback", "无语"),
    "awkward": ("bashful", "glancedown", "尴尬"),
    "worried": ("worried", "tilt", "glancedown", "担心"),
    "afraid": ("afraid", "leanback", "害怕"),
    "flustered": ("bashful", "leanback", "慌张"),
    "surprised": ("surprised", "leanback", "惊讶"),
    "sad": ("sad", "glancedown", "伤心"),
    "low_battery": ("glancedown", "低电量"),
    "helpless": ("helpless", "glancedown", "无奈"),
    "angry": ("angry", "frustrated", "生气"),
    "disgusted": ("disgusted", "leanback", "厌恶"),
    "confident": ("confident", "leanforward", "自信"),
}

_MOTION_NAME_ALIASES = {
    "bashful": "扭捏", "swayforward": "前倾左右摇摆身体",
    "peekforward": "轻探身", "leanforward": "前倾",
    "leanforwarddeep": "大幅前倾", "leanback": "后仰",
    "glanceup": "向上看一眼", "glancedown": "向下看一眼",
}


def motion_matches(group, filename, meanings):
    group = re.sub(r"[\s_-]", "", str(group).casefold())
    name = str(filename).replace("\\", "/").rsplit("/", 1)[-1].casefold()
    name = re.sub(r"[\s_-]", "", name.removesuffix(".motion3.json"))
    aliases = (*meanings, *(_MOTION_NAME_ALIASES[meaning] for meaning in meanings
                           if meaning in _MOTION_NAME_ALIASES))
    return any(group == meaning or name.endswith("." + meaning) or name == meaning
               for meaning in aliases)


def intent_from_reply(reply):
    text = str(reply or "")
    explicit = re.search(r"\[intent:([a-z_]+)\]", text, re.IGNORECASE)
    if explicit and explicit.group(1).lower() in INTENT_LABELS:
        return explicit.group(1).lower()
    # Quoted examples and questions are not assertions made by the character.
    text = re.sub(r"```.*?```|[“「『\"].*?[”」』\"]", "", text, flags=re.DOTALL)
    text = re.sub(r"\[(?:emotion|intent):[^\]]+\]", "", text, flags=re.IGNORECASE)
    for sentence in re.findall(r"[^。！？!?;\n]+[。！？!?;\n]?", text):
        sentence = sentence.strip()
        if not sentence or sentence.endswith(("?", "？")) or re.search(
                r"(?:吗|么|是否|是不是|要不要|如果|假如|例如|比如)", sentence):
            continue
        rules = (
            ("disagree", r"不同意|不赞同|不认同|不能同意|不支持|我反对|我不认可|i disagree|i don't agree|i do not agree"),
            ("inspect", r"(?<!不)(?:仔细看看|仔细看一下|凑近看看|靠近看看|仔细观察)|let me (?:take a closer look|look closely)"),
            ("agree", r"(?<!不)(?:我同意|我赞同|我认同|说得对|你说的对|确实如此)|\bi agree\b|you're right"),
            ("thinking", r"让我想想|我想一想|让我思考|let me think"),
            ("comfort", r"别担心|不要担心|我陪着你|我在这里陪你|don't worry"),
            ("greet", r"你好|早上好|晚上好|欢迎回来|达令.*回来了|\bhello\b|welcome back"),
        )
        for intent, pattern in rules:
            if intent == "inspect" and re.search(r"(?:不想|不用|不需要|不要|不能).{0,5}(?:仔细|凑近|靠近)", sentence):
                continue
            if re.search(pattern, sentence, re.IGNORECASE):
                return intent
    return "neutral"


for _emotion, _definition in PRESET_EMOTIONS.items():
    _groups = EMOTION_MOTION_GROUPS.get(_emotion, ())
    if not _groups:
        _groups = tuple(
            group
            for fallback in emotion_fallback_chain(_emotion)[1:-1]
            for group in EMOTION_MOTION_GROUPS.get(fallback, ())
        )
    EMOTION_MOTION_GROUPS[_emotion] = tuple(dict.fromkeys(
        (_emotion, _definition["label"], *_groups)
    ))


def finite_number(value, default, minimum, maximum):
    try:
        value = float(value)
        return max(minimum, min(maximum, value)) if math.isfinite(value) else default
    except (TypeError, ValueError):
        return default


class BehaviorSelector:
    def __init__(self, rng=None):
        self.rng = rng or random.Random()
        self.last_choices = {}
        self.last_event = None
        self.last_pose_at = float("-inf")
        self.last_motion_at = float("-inf")

    def choose(self, kind, candidates):
        candidates = list(dict.fromkeys(candidates))
        if not candidates:
            return None
        previous = self.last_choices.get(kind)
        choices = [item for item in candidates if item != previous] or candidates
        selected = self.rng.choice(choices)
        self.last_choices[kind] = selected
        return selected

    def select(self, event, state, now, expressions, poses=(), motions=(), cooldown=4.0):
        if event == self.last_event:
            return None
        self.last_event = event
        result = {"expression": self.choose("expression:" + state, expressions),
                  "pose": None, "motion": None}
        if now - self.last_pose_at >= cooldown:
            result["pose"] = self.choose("pose", poses)
            if result["pose"] is not None:
                self.last_pose_at = now
        if now - self.last_motion_at >= cooldown:
            result["motion"] = self.choose("motion", motions)
            if result["motion"] is not None:
                self.last_motion_at = now
        return result
