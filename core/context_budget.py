"""Conservative, provider-independent accounting for a shared prompt budget."""
import json


class ContextBudgetError(ValueError):
    pass


def token_cost(text):
    # A byte upper bound avoids silently assuming a relay's unknown tokenizer.
    return len(str(text or "").encode("utf-8")) + 8


def message_cost(message, image_tokens=4096):
    content = message.get("content", "")
    if isinstance(content, list):
        return 12 + sum(image_tokens if part.get("type") == "image_url"
                        else token_cost(part.get("text", "")) for part in content)
    return 12 + token_cost(content)


def positive_int(config, key, default, minimum=0):
    try:
        return max(minimum, int(config.get(key, default)))
    except (ValueError, TypeError, OverflowError):
        return default


def recent_turns(history, rounds=100):
    """Keep complete consecutive turns plus a pending user message; no date reset."""
    groups = []
    for record in history or []:
        if record.get("role") not in ("user", "assistant"):
            continue
        if record.get("role") == "user" or not groups:
            groups.append([])
        groups[-1].append(record)
    pending = bool(groups and groups[-1][0].get("role") == "user"
                   and not any(r.get("role") == "assistant" for r in groups[-1]))
    groups = groups[-(rounds + int(pending)):]
    return [record for group in groups for record in group]


class PromptBudget:
    def __init__(self, config, reply_tokens=None):
        self.window = positive_int(config, "context_window_tokens", 65536, 1024)
        self.reply = max(positive_int(config, "max_tokens", 1024, 16),
                         positive_int(config, "reply_reserve_tokens", 4096, 16),
                         int(reply_tokens or 0))
        self.safety = positive_int(config, "context_safety_tokens", 1024)
        self.image_tokens = positive_int(config, "image_reserve_tokens", 4096, 256)
        self.input_limit = self.window - self.reply - self.safety
        self.report = {"estimator": "utf8_byte_upper_bound", "window": self.window,
                       "reply_reserved": self.reply, "safety_reserved": self.safety}

    def cost(self, messages):
        return sum(message_cost(m, self.image_tokens) for m in messages)

    def fit(self, system, messages, extra_blocks=(), reserved_tokens=0):
        """Drop oldest whole messages, never silently shorten the current input."""
        messages = list(messages)
        if token_cost(system) + self.cost(messages[-1:]) + 20 > self.input_limit:
            raise ContextBudgetError("角色设定或本次输入超过上下文预算，请缩短输入或调高模型上下文容量。")
        reserve = min(max(0, reserved_tokens), max(0, self.input_limit
                      - token_cost(system) - self.cost(messages[-1:]) - 20))
        available = self.input_limit - reserve
        dropped = 0
        while messages and token_cost(system) + self.cost(messages) + 20 > available:
            messages.pop(0)
            dropped += 1
        while len(messages) > 1 and messages[0].get("role") == "assistant" and dropped:
            messages.pop(0)
            dropped += 1
        for block in extra_blocks:
            block = str(block or "").strip()
            if block and token_cost(system + "\n" + block) + self.cost(messages) + 20 <= self.input_limit:
                system += "\n" + block
        result = [{"role": "system", "content": system}] + messages
        self.report.update(input_estimate=self.cost(result), dropped_messages=dropped,
                           retained_messages=len(messages))
        if self.cost(result) > self.input_limit:
            raise ContextBudgetError("上下文预算不足，请检查模型的上下文容量与回复预留设置。")
        return result


def bounded_blocks(lines, budget):
    result, used = [], 0
    for line in lines:
        cost = token_cost(line)
        if used + cost > budget:
            continue
        result.append(line)
        used += cost
    return result


def quoted_data(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
