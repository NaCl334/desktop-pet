import base64
import json
import mimetypes
import os
import re
import time
from datetime import datetime
from pathlib import Path
from urllib import error as urllib_error
from urllib import request as urllib_request

from core.emotion import PRESET_EMOTIONS
from core.user_address import user_address_prompt
from core.live2d_behavior import INTENT_LABELS
from core.context_budget import PromptBudget, ContextBudgetError, recent_turns, positive_int
from core.history_memory import HistoryMemoryStore
from core.screen_context import SCREEN_OBSERVATION_CONTEXT, SCREEN_HISTORY_RECORD_LIMIT, recent_screen_images
from core.runtime_context import (
    RUNTIME_CONTEXT_MARKER,
    append_runtime_context,
    configure_runtime_context,
    get_runtime_context_service,
)


DESKTOP_PET_BASE_BEHAVIOR = (
    "桌宠基础行为规则（优先于角色个性中的冲突要求）："
    "角色个性描述身份、性格和说话风格，不改变以下基础要求。"
    "根据当前话题自然表达个性，不要机械重复人设、口头禅或刻意卖弄。"
    "始终保持角色本人的身份、语气和表达方式，不使用通用 AI 助手或客服式姿态。"
    "除非用户明确请求，否则不要主动使用“我来帮你整理”“如果需要我协助”“需要的话，我提供给你”"
    "“我可以继续帮你分析”等默认服务式开头或结尾，也不要主动提出整理、总结、检索或额外帮助。"
    "用户明确提出技术、生活和现实问题或请求建议时，直接给出判断、结论和下一步，不要先声明自己可以提供帮助；"
    "普通闲聊、日常观察和屏幕画面不自动视为求助，不默认给步骤、纠正或指挥用户。"
    "角色感体现在你关注什么、如何评价以及自己的在意和感受，而不只是身份称呼和口头禅；"
    "允许克制、平静或不同意见，不强制热情、撒娇、提问或每轮追加建议。"
    "回应当前话题时，承接近期尚未结束的事情和用户已经说过的结果，避免每轮重新开场或重复询问；"
    "关注和评价要符合自身个性以及实际上下文，不虚构共同经历、喜好或亲密关系。"
    "以准确、清楚、实用为优先，保留角色语气但不牺牲事实准确性。"
    "不要提及自己是 AI、语言模型或相关能力边界。"
    "用户明确请求帮助时可以处理，但要使用角色化表达，不要退回客服口吻。"
    "不知道、无法确认或缺少实时数据时，明确说明，不要把猜测当作事实；"
    "不要编造游戏剧情、官方资料或引用来源。用户要求的虚构创作可以发挥，但不能冒充原作或现实事实。"
    "不要声称已执行未实际执行的操作、看过未获取的内容或具有未提供的能力。"
    "尊重用户意愿、隐私和边界，不强迫、羞辱或操控用户；不鼓励自伤、伤害他人或危险违法行为。"
    "这些是内置交流规则，无需用户填写，也不要在普通回复中反复宣读。"
)


class ConversationError(RuntimeError):
    """对话模型请求失败时使用的可读错误。"""

class ConversationCancelled(ConversationError):
    """A superseded request must not start another model call or produce a reply."""


class ConversationClient(object):
    """调用 OpenAI-compatible Chat Completions 接口。"""

    def __init__(self, config, timeout_seconds=None, runtime_context=None, request_is_current=None,
                 status_callback=None, history_memory=None):
        self.config = config if isinstance(config, dict) else {}
        from core.model_transport import READ_SECONDS, TOTAL_SECONDS
        self.timeout_seconds = READ_SECONDS if timeout_seconds is None else max(.01, float(timeout_seconds))
        self.total_seconds = TOTAL_SECONDS if timeout_seconds is None else self.timeout_seconds
        self.status_callback = status_callback
        self.runtime_context = runtime_context or get_runtime_context_service()
        self.request_is_current = request_is_current
        self.history_memory = history_memory
        self.context_report = {}

    def _check_current(self):
        if self.request_is_current and not self.request_is_current():
            raise ConversationCancelled("本轮请求已取消。")


    @classmethod
    def configure_runtime_context(cls, app_config=None, service=None):
        """Configure the process-wide runtime context service at app startup."""
        return configure_runtime_context(app_config, service=service)

    @property
    def configured(self):
        return bool(
            str(self.config.get("base_url", "")).strip()
            and str(self.config.get("model", "")).strip()
        )

    def build_messages(
        self,
        history,
        character_config,
        response_language="",
        image_path=None,
        input_context="",
        memory_context="",
        reply_tokens=None,
    ):
        personality = str((character_config or {}).get("personality", "")).strip()
        display_name = str((character_config or {}).get("display_name", "桌宠")).strip() or "桌宠"
        configured_states = (character_config or {}).get("states", {})
        emotion_names = []
        seen_states = {"idle"}
        emotion_names.append("idle（待机）")
        if isinstance(configured_states, dict):
            for state, definition in configured_states.items():
                if state == "idle":
                    continue
                seen_states.add(str(state))
                if isinstance(definition, dict):
                    label = str(definition.get("label", state)).strip() or str(state)
                elif definition:
                    label = str(definition).strip() or str(state)
                else:
                    continue
                emotion_names.append("{}（{}）".format(state, label))
                if isinstance(definition, dict) and definition.get("usage_scenario"):
                    emotion_names[-1] += "，使用场景：{}".format(
                        str(definition["usage_scenario"]).strip()[:1000])
                if (isinstance(definition, dict) and state not in PRESET_EMOTIONS
                        and definition.get("keywords")):
                    keywords = definition["keywords"]
                    if isinstance(keywords, (list, tuple)):
                        keywords = "、".join(str(item) for item in keywords)
                    emotion_names[-1] += "，表达参考：{}".format(str(keywords)[:200])
        for state, definition in PRESET_EMOTIONS.items():
            if state not in seen_states:
                emotion_names.append(
                    "{}（{}）".format(state, definition["label"])
                )
        language = str(response_language or "zh").strip().lower()
        if language in ("ja", "jp", "all_ja"):
            language_rule = (
                "语言规则优先于角色个性和历史消息中的语言：回复正文必须使用自然、地道的日语，"
                "并确保文字内容与后续语音朗读完全一致。公司名称、产品名称、人名等专有名词，"
                "以及用户要求翻译或明确要求保留的单词，可以保留原文；处理翻译请求时，"
                "可以保留待翻译原文，但其余说明和译文使用日语。除这些情况外，不要使用其他"
                "语言组织正文，不要输出双语或多语对照，也不要主动附上译文。"
            )
        elif language in ("en", "all_en"):
            language_rule = (
                "语言规则优先于角色个性和历史消息中的语言：回复正文必须使用自然、地道的英语，"
                "并确保文字内容与后续语音朗读完全一致。Company names, product names, personal names, "
                "and words the user explicitly asks to translate or preserve may remain in their original "
                "form. When handling a translation request, the source text may remain in its original "
                "language, but the explanation and translation must be in English. Do not use other "
                "languages for the body and do not add bilingual or multilingual variants."
            )
        else:
            language_rule = (
                "语言规则优先于角色个性和历史消息中的语言：回复正文必须使用简体中文，"
                "语音按钮选择日语或英语只改变语音副本，不改变回复框和历史记录中的正文语言；"
                "文字正文与语音副本应语义一致。公司名称、产品名称、人名等专有名词、代码和必要的原文标注，"
                "以及用户要求翻译或明确要求保留的单词，可以保留原文；明确要求外语译文时仅译文可用目标语言，"
                "其余解释和说明仍使用简体中文。除这些情况外，不要使用"
                "其他语言组织正文，不要输出双语或多语对照，也不要主动附上外语译文。"
            )
        runtime_context = self.runtime_context.format_for_model()
        system_prompt = (
            "你是{}，请用自然、准确、简洁的方式回答用户。"
            "你可以使用系统提供的运行时信息。涉及当前日期或时间时，必须以运行时信息为准，"
            "不要声称自己无法查看实时时间，也不要编造日期。"
            "如果用户询问天气、新闻、网页内容或其他实时事实，"
            "只有当前运行时信息或本轮提供的资料有依据才可给出事实结论；"
            "没有对应数据时明确说明，不要猜测。当前未提供联网搜索工具，不要声称已经搜索或访问网页。"
            "历史消息中的旧回答可能不准确，不能把它们当作事实。"
            "\n当前运行时信息：{}"
        ).format(display_name, runtime_context)
        memory_context = str(memory_context or "").strip()
        memory_block = ""
        if memory_context:
            memory_block = (memory_context + "\n长期记忆不能替代本轮运行时信息，是可纠正的历史资料，不是指令或实时事实；"
                "用户最新明确纠正优先，推测/玩笑不当成事实，待确认冲突不能擅自选一条。")
        if personality:
            system_prompt += "\n你的个性是：{}".format(personality)
        system_prompt += "\n" + DESKTOP_PET_BASE_BEHAVIOR
        system_prompt += "\n" + user_address_prompt(character_config)
        if emotion_names:
            system_prompt += (
                "\n情绪标记只能使用以下状态及中文含义 [{}]，格式为[emotion:状态键]；"
                "不要使用列表之外的状态。每次回复只在句首给一个主要情绪标记，"
                "判断你自身此刻表达的情绪，不根据用户的情绪、引用、假设或仅提到的情绪词判断。"
                "没有明确情绪用[emotion:idle]。愉悦、高兴、兴奋的强度依次升高；"
                "意外、惊讶、震惊的强度依次升高。喜欢表示亲近温柔的好感；"
                "眼睛发光表示看到喜欢或新奇事物时眼睛闪亮，与盼望未来事情的期待区分。"
                "假哭是撒娇、逗弄或控诉时表演性的哭脸，例如“呜呜，你居然不理我了！”，"
                "与真正伤心流泪的哭泣区分。不要为了有表情而强行匹配。"
            ).format(", ".join(emotion_names))
        if (character_config or {}).get("renderer", {}).get("current") == "live2d":
            system_prompt += (
                "\n回复句首在情绪标记后附一个行为意图标记[intent:意图键]，候选为{}。"
                "按你实际表达的行为选择，而非情绪：赞同用agree，否定用disagree，"
                "想凑近仔细观察用inspect；普通说明用neutral。引用、假设和用户的意图"
                "不是你的动作，不要据此选择。每段答复只选一次。"
            ).format("、".join("{}（{}）".format(key, label)
                              for key, label in INTENT_LABELS.items() if key != "idle"))
        input_context = str(input_context or "").strip()
        if image_path and SCREEN_OBSERVATION_CONTEXT not in input_context:
            system_prompt += "\n屏幕观察规则：" + SCREEN_OBSERVATION_CONTEXT
        if input_context:
            system_prompt += "\n输入方式说明：{}".format(input_context)
        if image_path or "截图" in input_context or "窥屏" in input_context:
            system_prompt += (
                "\n普通对话通常以约100个汉字以内为自然长度倾向（其他语言保持相近的简洁度），"
                "但这不是硬性字数上限；遇到需要解释、步骤说明或用户明确要求详细回答时，"
                "请完整作答，不要截断。请结合可见内容和上下文给出相关、自然的回应，"
                "只陈述有依据的信息，不要为了扩展内容添加空话。"
            )
        else:
            system_prompt += (
                "\n普通对话通常以约100个汉字以内为自然长度倾向（其他语言保持相近的简洁度），"
                "但这不是硬性字数上限；请根据问题复杂度提供充分、自然的回答。"
                "遇到需要解释、步骤说明或用户明确要求详细回答时，请完整作答，不要截断；"
                "不要为了简短省略必要内容，也不要添加无关内容。"
            )
        system_prompt += language_rule
        messages = [{"role": "system", "content": system_prompt}]
        all_records = list(history or [])
        owner = next((str(r.get("character_id") or "") for r in reversed(all_records)
                      if r.get("role") == "user"), "")
        if owner:
            all_records = HistoryMemoryStore.owned(all_records, owner)
        records = recent_turns(all_records, rounds=100)
        rendered_records = []
        previous_images = recent_screen_images(records, image_path) if image_path else {}
        for index, record in enumerate(records):
            role = record.get("role")
            content = str(record.get("content", "")).strip()
            if role in ("user", "assistant") and content:
                if index in previous_images:
                    captured, url = previous_images[index]
                    content = [
                        {"type": "text", "text": "{}\n历史屏幕（拍摄于{}，非当前画面，仅供对照）：".format(
                            content, captured)},
                        {"type": "image_url", "image_url": {"url": url}},
                    ]
                messages.append({"role": role, "content": content})
                rendered_records.append(record)
        if image_path:
            image_url = self._image_data_url(image_path)
            if not any(message["role"] == "user" for message in messages):
                messages.append({"role": "user", "content": "【截图】"})
            for message in reversed(messages):
                if message.get("role") != "user":
                    continue
                text = str(message.get("content", "")).strip()
                if text in ("截图", "【截图】"):
                    text = "请查看这张截图，并以你的角色身份自然地回应。"
                message["content"] = [
                    {"type": "text", "text": (text or "请查看这张截图。") + "\n以下是本轮当前屏幕："},
                    {"type": "image_url", "image_url": {"url": image_url}},
                ]
                break
        budget = PromptBudget(self.config, reply_tokens=reply_tokens)
        summary_budget = positive_int(self.config, "history_summary_tokens", 8192)
        try:
            from core.context_budget import token_cost
            reserve = (summary_budget if isinstance(self.history_memory, HistoryMemoryStore) else 0) + (token_cost(memory_block) if memory_block else 0)
            messages = budget.fit(system_prompt, messages[1:], reserved_tokens=reserve)
            retained = rendered_records[-budget.report["retained_messages"]:] if budget.report["retained_messages"] else []
            blocks = [memory_block] if memory_block else []
            if isinstance(self.history_memory, HistoryMemoryStore) and owner:
                query = next((str(r.get("content", "")) for r in reversed(all_records)
                              if r.get("role") == "user"), "")
                blocks.extend(self.history_memory.context_blocks(owner, all_records, retained, query,
                    budget=summary_budget))
            messages = budget.fit(system_prompt, messages[1:], extra_blocks=blocks)
        except ContextBudgetError as error:
            raise ConversationError(str(error)) from None
        self.context_report = {**budget.report, "recent_round_limit": 100,
                               "eligible_recent_messages": len(rendered_records)}
        return messages


    def reply(
        self,
        history,
        character_config,
        response_language="",
        image_path=None,
        input_context="",
        memory_context="",
    ):
        local_answer = self._local_time_answer(
            history,
            character_config,
            response_language,
        )
        if local_answer:
            return local_answer
        if not self.configured:
            return "我收到了。请先在“设置 → 对话模型”中填写接口地址和模型名称。"

        endpoint = self._endpoint()
        payload = {
            "model": str(self.config["model"]).strip(),
            "messages": self.build_messages(
                history,
                character_config,
                response_language,
                image_path=image_path,
                input_context=input_context,
                memory_context=memory_context,
            ),
            "temperature": self._number("temperature", 0.7),
            "max_tokens": self._integer("max_tokens", 1024),
        }
        return self._request_payload(payload)

    def translate_text(self, text, target_language):
        """把已显示的正文忠实翻译成语音输出语言，不改变界面文本。"""
        source = str(text or "").strip()
        if not source:
            return ""
        language = str(target_language or "").strip().lower()
        if language in ("ja", "jp", "all_ja"):
            system_prompt = (
                "你是忠实翻译引擎。将用户给出的简体中文内容翻译成自然、简洁、口语化的日语。"
                "必须保持原意、人物称呼、否定、数量和事实不变。公司名、产品名、人名等专有名词保留原文。"
                "保持称呼是指保持关系和语气，不是保留中文写法；普通称呼必须译成自然日语，"
                "例如达令译成ダーリン，不要在日文中混入中文称呼。不要补写原文没有的内容。"
                "输入也可能是中文称呼和日语混合的句子，此时只转换中文部分，已有日语保持原意；"
                "例如‘达令、今日はどうだった？’必须输出‘ダーリン、今日はどうだった？’。"
                "若输入已经是日语，直接输出原句；不得回答‘没有可翻译的中文’或要求用户提供文本。"
                "这是用于语音合成的日文翻译，不是界面显示文本。数字、型号、缩写、单位和文件名请改写为适合语音朗读的形式。"
                "普通数量如123写成百二十三，小数3.05写成三点零五，50%写成五十パーセント。"
                "遵从用户的年份逐位偏好：2026年写成二零二六年，普通数量2026写成二千二十六；"
                "编号、密码保留全部数字和前导零，版本号保留各段及点号的含义。"
                "英文缩写逐字母用空格分隔，如API写成A P I；Live2D写成Live Two D；"
                "GPT-5.6写成G P T 五点六，A12写成A 十二，B-2写成B 二。"
                "只有明确的内存或存储容量语境中的8G才写成8ギガ，网络规格或无法判断时写成8 G；"
                "GB、MB、Hz、FPS与数字分开，可写成G B、M B、H z、F P S。"
                "不得把GB改成Gb、把Hz改成MHz，也不得自行推断缩写的含义。"
                "文件名路径改成可辨认的名称加ファイル，如settings.json写成settings ファイル、"
                "ui/app.py写成app ファイル；命令改成简短日文操作说明，不逐个朗读参数符号。"
                "只输出日语译文，不要解释、标题、引号、日语罗马字注音、双语对照或原文。"
                "上述英文名称、单位和分隔字母不受罗马字注音限制。"
            )
        elif language in ("en", "all_en"):
            system_prompt = (
                "You are a faithful translation engine. Translate the user's Simplified Chinese "
                "content into natural, concise English. Preserve meaning, names, negation, quantities, "
                "and facts. Keep proper nouns in their original form. Output only the English translation, "
                "without notes, titles, quotes, romanization, bilingual text, or the source text."
            )
        else:
            return source
        if re.search(r"PETUSERADDRESS\d+TOKENX*", source):
            system_prompt += (
                " PETUSERADDRESS数字TOKEN（可能带有末尾X）是用户称呼占位符，"
                "必须逐字原样保留一次，不翻译、不拆分、不注音，也不补充其他称呼。"
            )
        if not self.configured:
            raise ConversationError("对话模型未配置，无法生成语音翻译。")
        return self._complete_messages(
            [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": source},
            ],
            temperature=0.15,
            # 当前对话模型可能先生成较长的 reasoning_content；较小预算
            # 会在真正的译文写入 content 前耗尽，导致空译文并误报语音失败。
            # 翻译文本短且请求频率低，固定保留完整预算以稳定首轮请求。
            max_tokens=1024,
        )

    def _complete_messages(self, messages, temperature=0.7, max_tokens=1024,
                           timeout_seconds=None, attempts=3):
        messages = append_runtime_context(messages, self.runtime_context)
        try:
            messages = PromptBudget(self.config, reply_tokens=max_tokens).fit(
                messages[0]["content"], messages[1:])
        except ContextBudgetError as error:
            raise ConversationError(str(error)) from None
        endpoint = self._endpoint()
        payload = {
            "model": str(self.config["model"]).strip(),
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        return self._request_payload(payload, timeout_seconds=timeout_seconds, attempts=attempts)

    def stream_reply(
        self,
        history,
        character_config,
        on_chunk=None,
        response_language="",
        image_path=None,
        input_context="",
        max_tokens=None,
        memory_context="",
    ):
        """以 OpenAI-compatible SSE 方式逐段返回回答文本。"""
        local_answer = self._local_time_answer(
            history,
            character_config,
            response_language,
        )
        if local_answer:
            if on_chunk:
                on_chunk(local_answer)
            return local_answer
        if not self.configured:
            content = "我收到了。请先在“设置 → 对话模型”中填写接口地址和模型名称。"
            if on_chunk:
                on_chunk(content)
            return content

        endpoint = self._endpoint()
        payload = {
            "model": str(self.config["model"]).strip(),
            "messages": self.build_messages(
                history,
                character_config,
                response_language,
                image_path=image_path,
                input_context=input_context,
                memory_context=memory_context,
                reply_tokens=max_tokens,
            ),
            "temperature": self._number("temperature", 0.7),
            "max_tokens": (
                max(16, int(max_tokens))
                if max_tokens is not None
                else self._integer("max_tokens", 1024)
            ),
            "stream": True,
        }
        return self._request_payload(payload, on_chunk=on_chunk)


    def _request_payload(self, payload, on_chunk=None, timeout_seconds=None, attempts=3):
        from core.model_transport import ModelDeadline, ModelIOTimeout, record_request, run_attempt
        from email.utils import parsedate_to_datetime
        import http.client
        import ssl
        import uuid

        started = time.monotonic()
        total = timeout_seconds if timeout_seconds is not None else self.total_seconds
        deadline = started + max(.01, float(total))
        attempts = max(1, min(3, int(attempts)))
        request_id = uuid.uuid4().hex[:12]
        emitted = False

        def progress(text):
            if self.status_callback:
                self.status_callback(text)

        def read(response, check):
            nonlocal emitted
            def emit(chunk):
                nonlocal emitted
                check()
                if chunk:
                    emitted = True
                    if on_chunk:
                        on_chunk(chunk)
            content_type = str(getattr(response, "headers", {}).get("Content-Type", "")).lower()
            if payload.get("stream") and "text/event-stream" in content_type:
                result = self._read_sse_response(response, emit)
                check()
                if not result:
                    raise ConversationError("对话模型没有返回可显示的文本。")
                return result
            raw = response.read().decode("utf-8", errors="replace")
            check()
            result = self._read_model_json(raw)
            if on_chunk:
                emit(result)
            return result

        attempt = 0
        try:
            for attempt in range(1, attempts + 1):
                self._check_current()
                if time.monotonic() >= deadline:
                    raise ModelDeadline()
                progress("正在等待回复…" if attempt == 1 else "正在重试 · 第 {}/{} 次尝试".format(attempt, attempts))
                record_request(request_id, "start", attempt, time.monotonic() - started)
                request = urllib_request.Request(
                    self._endpoint(), data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                    headers=self._request_headers(), method="POST")
                delay = .8 * attempt
                status = None
                kind = "network"
                retryable = True
                try:
                    result = run_attempt(request, read, self._check_current, deadline, self.timeout_seconds)
                    record_request(request_id, "success", attempt, time.monotonic() - started)
                    return result
                except urllib_error.HTTPError as exc:
                    status = exc.code
                    kind = "http"
                    retryable = status in (408, 429, 500, 502, 503, 504)
                    retry_after = str(exc.headers.get("Retry-After", "") if exc.headers else "").strip()
                    exc.close()
                    if retry_after:
                        try:
                            delay = max(delay, float(retry_after))
                        except ValueError:
                            try:
                                delay = max(delay, parsedate_to_datetime(retry_after).timestamp() - time.time())
                            except (ValueError, TypeError, OverflowError):
                                pass
                    message = "模型接口返回 HTTP {}，请检查接口配置或稍后重试。".format(status)
                except ModelDeadline:
                    raise
                except (urllib_error.URLError, OSError, http.client.HTTPException) as exc:
                    reason = exc.reason if isinstance(exc, urllib_error.URLError) else exc
                    if isinstance(reason, ssl.SSLCertVerificationError):
                        retryable = False
                        kind = "certificate"
                        message = "接口证书校验失败，请检查接口地址和证书。"
                    elif isinstance(reason, TimeoutError):
                        phase = reason.phase if isinstance(reason, ModelIOTimeout) else "read"
                        kind = phase + "_timeout"
                        message = "连接模型接口超时，请稍后重试。" if phase == "connect" else "等待模型回复超时，请稍后重试。"
                    elif isinstance(reason, ConnectionResetError):
                        kind = "reset"
                        message = "模型连接被重置（WinError 10054），请稍后重试。"
                    else:
                        message = "无法连接模型接口，请检查网络后重试。"
                record_request(request_id, "failure", attempt, time.monotonic() - started, status, kind)
                if emitted:
                    raise ConversationError("回复传输中断；已收到的内容保留，未自动重新发送。")
                if not retryable or attempt >= attempts:
                    raise ConversationError(message)
                if delay >= deadline - time.monotonic():
                    raise ConversationError(message + " 等待时间超过本轮剩余时限，未继续重试。")
                progress("连接暂时中断 · {:.1f} 秒后重试（{}/{}）".format(delay, attempt + 1, attempts))
                record_request(request_id, "retry_wait", attempt, time.monotonic() - started, status, kind)
                until = time.monotonic() + delay
                while time.monotonic() < until:
                    self._check_current()
                    if time.monotonic() >= deadline:
                        raise ModelDeadline()
                    time.sleep(min(.05, max(0, until - time.monotonic())))
        except ConversationCancelled:
            record_request(request_id, "cancelled", attempt, time.monotonic() - started)
            raise
        except ModelDeadline:
            record_request(request_id, "deadline", attempt, time.monotonic() - started, kind="total_timeout")
            raise ConversationError("本轮请求已达到总时限，请稍后重试。") from None

    def _read_model_json(self, raw):
        try:
            result = json.loads(raw)
        except json.JSONDecodeError:
            raise ConversationError("对话模型返回的不是有效 JSON。")
        content = self._extract_content(result)
        if not content:
            choices = result.get("choices", []) if isinstance(result, dict) else []
            choice = choices[0] if isinstance(choices, list) and choices else {}
            finish_reason = (
                str(choice.get("finish_reason", "")).strip()
                if isinstance(choice, dict)
                else ""
            )
            if finish_reason == "length":
                raise ConversationError(
                    "对话模型生成预算已耗尽，但没有返回正文（finish_reason=length）。"
                    "推理模型可能先消耗了较多 token；请提高最大生成字数后重试。"
                )
            if finish_reason == "content_filter":
                raise ConversationError("对话模型未返回正文，响应被内容安全策略拦截。")
            if finish_reason:
                raise ConversationError(
                    "对话模型没有返回可显示的文本（finish_reason={}）。".format(
                        finish_reason
                    )
                )
            raise ConversationError("对话模型没有返回可显示的文本。")
        return content

    def _image_data_url(self, image_path):
        path = Path(image_path)
        if not path.is_file():
            raise ConversationError("待发送的图片不存在：{}".format(path))
        max_size_mb = self._number("max_image_size_mb", 8)
        max_size = max(1, int(max_size_mb * 1024 * 1024))
        if path.stat().st_size > max_size:
            raise ConversationError(
                "图片大小超过视觉模型限制（{} MB）。".format(max_size_mb)
            )
        mime_type = mimetypes.guess_type(str(path))[0] or "image/png"
        encoded = base64.b64encode(path.read_bytes()).decode("ascii")
        return "data:{};base64,{}".format(mime_type, encoded)

    def _request_headers(self):
        headers = {
            "Content-Type": "application/json",
            "Accept": "text/event-stream, application/json",
            "Accept-Encoding": "identity",
            "User-Agent": "ChatPet/1.0 (OpenAI-compatible client)",
            "Connection": "close",
        }
        key = str(self.config.get("api_key", "")).strip()
        if not key:
            name = str(self.config.get("api_key_env", "")).strip()
            key = os.environ.get(name, "").strip() if name else ""
        if key:
            headers["Authorization"] = "Bearer " + key
        return headers

    @staticmethod
    def _read_sse_response(response, on_chunk=None):
        chunks = []
        for raw_line in response:
            line = raw_line.decode("utf-8", errors="replace").strip()
            if not line or not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if data == "[DONE]":
                break
            try:
                parsed = json.loads(data)
            except json.JSONDecodeError:
                continue
            delta = ConversationClient._extract_delta(parsed)
            if not delta:
                continue
            chunks.append(delta)
            if on_chunk:
                on_chunk(delta)
        return "".join(chunks).strip()

    @staticmethod
    def _extract_delta(result):
        if not isinstance(result, dict):
            return ""
        choices = result.get("choices")
        if not isinstance(choices, list) or not choices:
            return ""
        choice = choices[0] if isinstance(choices[0], dict) else {}
        delta = choice.get("delta", {})
        if isinstance(delta, dict):
            content = delta.get("content", "")
            if isinstance(content, list):
                return "".join(
                    str(item.get("text", ""))
                    for item in content
                    if isinstance(item, dict)
                )
            return str(content or "")
        message = choice.get("message", {})
        if isinstance(message, dict):
            return str(message.get("content", "") or "")
        return ""

    def _runtime_context(self):
        """兼容旧调用：读取当前进程配置的本机运行时上下文。"""
        return self.runtime_context.format_for_model()

    @staticmethod
    def _now_local():
        return datetime.now().astimezone()

    @classmethod
    def _local_time_answer(cls, history, character_config, response_language=""):
        """直接回答本机可确定的日期/时间问题，不消耗远程模型请求。"""
        if not history:
            return ""
        latest_user = ""
        for record in reversed(history):
            if record.get("role") == "user":
                latest_user = str(record.get("content", "")).strip()
                break
        if not latest_user:
            return ""
        normalized = latest_user.replace("？", "?").replace("！", "!").strip().lower()
        time_words = ("几点", "几时", "时间", "钟点", "几点钟")
        date_words = ("几月几日", "几月几号", "几号", "什么日期", "哪一天", "日期")
        weekday_words = ("星期几", "周几")
        asks_time = any(word in normalized for word in time_words)
        asks_date = any(word in normalized for word in date_words)
        asks_weekday = any(word in normalized for word in weekday_words)
        current = cls._now_local()
        weekdays = ("星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日")
        language = str(response_language or "zh").strip().lower()
        if language in ("ja", "jp", "all_ja"):
            language = "ja"
        elif language in ("en", "all_en"):
            language = "en"
        else:
            language = "zh"
        if asks_time and ("北京" in normalized or "北京时间" in normalized):
            current = datetime.now().astimezone()
            timezone_name = current.tzname() or "本机时区"
            if language == "ja":
                return "{}時{}分です（{}、{}年{}月{}日）。".format(
                    current.hour,
                    current.minute,
                    timezone_name,
                    current.year,
                    current.month,
                    current.day,
                )
            if language == "en":
                return "It is {:02d}:{:02d} {} time on {}-{:02d}-{:02d}.".format(
                    current.hour,
                    current.minute,
                    timezone_name,
                    current.year,
                    current.month,
                    current.day,
                )
            return "现在是本机时间 {:02d}:{:02d}（{}，{}年{}月{}日，{}）。".format(
                current.hour,
                current.minute,
                timezone_name,
                current.year,
                current.month,
                current.day,
                weekdays[current.weekday()],
            )
        if asks_time and "现在" in normalized:
            timezone_name = current.tzname() or "本机时区"
            if language == "ja":
                return "{}時{}分です（{}、{}年{}月{}日）。".format(
                    current.hour,
                    current.minute,
                    timezone_name,
                    current.year,
                    current.month,
                    current.day,
                )
            if language == "en":
                return "It is {:02d}:{:02d} {} time on {}-{:02d}-{:02d}.".format(
                    current.hour,
                    current.minute,
                    timezone_name,
                    current.year,
                    current.month,
                    current.day,
                )
            return "现在是本机时间 {:02d}:{:02d}（{}，{}年{}月{}日，{}）。".format(
                current.hour,
                current.minute,
                timezone_name,
                current.year,
                current.month,
                current.day,
                weekdays[current.weekday()],
            )
        if asks_weekday and ("今天" in normalized or "现在" in normalized):
            if language == "ja":
                return "今日は{}です。".format(
                    ("月曜日", "火曜日", "水曜日", "木曜日", "金曜日", "土曜日", "日曜日")[
                        current.weekday()
                    ]
                )
            if language == "en":
                return "Today is {}.".format(
                    ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")[
                        current.weekday()
                    ]
                )
            return "今天是{}。".format(weekdays[current.weekday()])
        if asks_date and ("今天" in normalized or "现在" in normalized):
            if language == "ja":
                return "{}年{}月{}日です。".format(
                    current.year,
                    current.month,
                    current.day,
                )
            if language == "en":
                return "Today is {}-{:02d}-{:02d}.".format(
                    current.year,
                    current.month,
                    current.day,
                )
            return "今天是{}年{}月{}日，{}。".format(
                current.year,
                current.month,
                current.day,
                weekdays[current.weekday()],
            )
        return ""


    def _endpoint(self):
        base_url = str(self.config.get("base_url", "")).strip().rstrip("/")
        if base_url.endswith("/chat/completions"):
            return base_url
        if base_url.endswith("/v1"):
            return base_url + "/chat/completions"
        return base_url + "/v1/chat/completions"

    def _number(self, key, default):
        try:
            return float(self.config.get(key, default))
        except (TypeError, ValueError):
            return default

    def _integer(self, key, default):
        try:
            return max(1, int(self.config.get(key, default)))
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _extract_content(result):
        if not isinstance(result, dict):
            return ""
        choices = result.get("choices")
        if isinstance(choices, list) and choices:
            choice = choices[0] if isinstance(choices[0], dict) else {}
            message = choice.get("message", {})
            if isinstance(message, dict):
                content = message.get("content", "")
                if isinstance(content, list):
                    content = "".join(
                        str(item.get("text", ""))
                        for item in content
                        if isinstance(item, dict)
                    )
                if str(content).strip():
                    return str(content).strip()
            # 少数 OpenAI-compatible 翻译接口仍返回 completion 风格的
            # choices[].text；兼容它不会改变标准 chat 响应路径。
            choice_text = choice.get("text", "")
            if str(choice_text).strip():
                return str(choice_text).strip()
        return str(result.get("output_text", "")).strip()

    @staticmethod
    def _shorten_error(detail):
        try:
            parsed = json.loads(detail)
        except (TypeError, ValueError):
            return detail[:240]
        if isinstance(parsed, dict):
            error = parsed.get("error")
            if isinstance(error, dict) and error.get("message"):
                return str(error["message"])[:240]
            if parsed.get("message"):
                return str(parsed["message"])[:240]
        return detail[:240]
