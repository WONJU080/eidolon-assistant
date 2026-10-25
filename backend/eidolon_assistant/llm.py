"""The role layer's model calls: intent, command normalisation, persona replies.

Every user-visible reply is bilingual: ``<ja>…</ja><zh>…</zh>``. The Japanese
carries the voice; the Chinese is the subtitle. JSON tasks (intent, normalise)
are parsed defensively: on any doubt the answer is the safe one (chat/ambiguous,
requires_confirmation=true).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

import httpx

from .config import Character, LLMSettings
from .redact import clip, redact

TAG_RE = re.compile(r"<(ja|zh)>(.*?)</\1>", re.S | re.I)


@dataclass
class Bilingual:
    ja: str
    zh: str

    @property
    def display(self) -> str:
        return f"{self.ja}\n{self.zh}".strip()

    @property
    def tts_text(self) -> str:
        return self.ja or self.zh


@dataclass
class Intent:
    intent: str  # chat | task | ambiguous
    reason: str = ""
    task_hint: str = ""


@dataclass
class TaskSpec:
    task: str
    constraints: list[str]
    requires_confirmation: bool
    notes: str = ""
    missing: list[str] = field(default_factory=list)


def parse_bilingual(text: str) -> Bilingual:
    """Read ``<ja>…</ja><zh>…</zh>``; tolerate a model that drops the tags."""
    ja_parts = [m.group(2).strip() for m in TAG_RE.finditer(text) if m.group(1).lower() == "ja"]
    zh_parts = [m.group(2).strip() for m in TAG_RE.finditer(text) if m.group(1).lower() == "zh"]
    ja = "".join(ja_parts).strip()
    zh = "".join(zh_parts).strip()
    if not ja and not zh:
        plain = TAG_RE.sub("", text).strip()
        return Bilingual(ja=plain, zh=plain)
    if not zh:
        zh = ja
    if not ja:
        ja = zh
    return Bilingual(ja=ja, zh=zh)


def _extract_json(text: str) -> dict[str, Any]:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    try:
        value = json.loads(text)
        if isinstance(value, dict):
            return value
    except json.JSONDecodeError:
        pass
    match = re.search(r"\{.*\}", text, re.S)
    if match:
        try:
            value = json.loads(match.group(0))
            if isinstance(value, dict):
                return value
        except json.JSONDecodeError:
            pass
    return {}


class RoleLLM:
    def __init__(self, settings: LLMSettings, character: Character) -> None:
        self.settings = settings
        self.character = character
        self._client = httpx.AsyncClient(
            base_url=settings.base_url.rstrip("/"),
            timeout=httpx.Timeout(settings.timeout_s),
            headers={
                "Authorization": f"Bearer {settings.api_key}",
                "Content-Type": "application/json",
            },
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    # ------------------------------------------------------------------ core
    async def _chat(self, messages: list[dict[str, str]], *, json_mode: bool = False,
                    max_tokens: int | None = None, temperature: float | None = None) -> str:
        if not self.settings.ready:
            raise RuntimeError(f"missing API key: {self.settings.api_key_env}")
        payload: dict[str, Any] = {
            "model": self.settings.model,
            "messages": messages,
            "temperature": self.settings.temperature if temperature is None else temperature,
            "max_tokens": max_tokens or self.settings.max_tokens,
            "stream": False,
        }
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        last_error: Exception | None = None
        for attempt in range(2):
            try:
                resp = await self._client.post("/chat/completions", json=payload)
                if resp.status_code in (400, 422) and json_mode and attempt == 0:
                    payload.pop("response_format", None)
                    continue
                resp.raise_for_status()
                data = resp.json()
                return data["choices"][0]["message"]["content"] or ""
            except (httpx.HTTPError, KeyError, IndexError) as exc:
                last_error = exc
                if attempt == 0:
                    payload.pop("response_format", None) if json_mode else None
                    continue
        raise RuntimeError(f"LLM 调用失败：{type(last_error).__name__}: {last_error}")

    # --------------------------------------------------------------- persona
    def _persona_system(self, extra: str = "") -> str:
        sheet = self.character.sheet_text
        rule = (
            "【这次对话的实际情况】\n"
            "- 你正在协助眼前的人操作这台电脑。系统操作由执行代理 Harness 完成，"
            "你负责判断、转达、确认和总结。\n"
            "- 用户主要用中文说话。你的每条回复都必须严格是这个格式：\n"
            "<ja>自然的日语台词</ja><zh>对应的中文字幕</zh>\n"
            "- 台词要短：一到两句，日语 60 字以内；中文 50 字以内。\n"
            "- 不要 Markdown、不要括号旁白、不要英文解释、不要逐字复述日志。\n"
            "- 只在被叫到时回应；不主动开启话题，不寒暄式追问。\n"
        )
        if extra:
            rule += extra + "\n"
        return f"{sheet}\n\n{rule}"

    async def chat(self, text: str, *, context_note: str = "") -> Bilingual:
        messages = [
            {"role": "system", "content": self._persona_system(context_note)},
            {"role": "user", "content": text},
        ]
        content = await self._chat(messages, max_tokens=400)
        return parse_bilingual(content)

    async def phrase_permission(self, tool: str, command: str, justification: str) -> Bilingual:
        messages = [
            {"role": "system", "content": self._persona_system(
                "【本轮任务】执行代理暂停了一个操作，等你向用户确认。"
                "用一句自然的角色口吻问是否允许，提到关键对象即可，不要念出完整命令、路径或英文选项。"
            )},
            {"role": "user", "content": (
                f"工具：{tool}\n命令：{clip(redact(command), 500)}\n"
                f"理由：{clip(redact(justification), 300)}\n"
                "请只输出一句确认问句，格式 <ja>…</ja><zh>…</zh>。"
            )},
        ]
        content = await self._chat(messages, max_tokens=200)
        return parse_bilingual(content)

    async def ask_missing(self, task: str, missing: list[str]) -> Bilingual:
        items = "、".join(missing) or "必要信息"
        messages = [
            {"role": "system", "content": self._persona_system(
                "用户的任务缺少必要信息。只针对缺少的项问一句，不要执行，不要猜内容。"
            )},
            {"role": "user", "content": f"任务：{task}\n缺少：{items}\n请只输出一句 <ja>…</ja><zh>…</zh>。"},
        ]
        content = await self._chat(messages, max_tokens=200)
        return parse_bilingual(content)

    async def clarify(self, text: str, hint: str) -> Bilingual:
        messages = [
            {"role": "system", "content": self._persona_system(
                "用户刚才的话目标不明确。只问一句澄清，不执行任何操作。"
            )},
            {"role": "user", "content": f"用户说：{text}\n（分类器判断：{hint or '不清楚'}）"},
        ]
        content = await self._chat(messages, max_tokens=200)
        return parse_bilingual(content)

    async def summarize(self, user_text: str, task: str, stop_reason: str,
                        digest: str, failed: bool) -> Bilingual:
        outcome = "失败或被取消" if failed or stop_reason not in ("end_turn", "success") else "成功"
        messages = [
            {"role": "system", "content": self._persona_system(
                "现在要把执行代理的结果口头汇报给用户。"
                "先结论；失败就明确说哪一步失败；最多两句。"
                "不要提工具名、JSON、选项、英文；不要泄露 key 或长路径。"
            )},
            {"role": "user", "content": (
                f"用户原话：{clip(redact(user_text), 500)}\n"
                f"标准化任务：{clip(redact(task), 800)}\n"
                f"结束原因：{stop_reason}\n"
                f"过程摘要：\n{clip(redact(digest), 3000)}\n"
                f"结果判定：{outcome}\n"
                "请用 <ja>…</ja><zh>…</zh> 汇报。"
            )},
        ]
        content = await self._chat(messages, max_tokens=400)
        return parse_bilingual(content)

    # ---------------------------------------------------------------- router
    async def judge_intent(self, text: str) -> Intent:
        messages = [
            {"role": "system", "content": (
                "你是意图分类器。判断用户这句话属于哪一类，只输出 JSON：\n"
                '{"intent":"chat|task|ambiguous","reason":"一句话理由","task_hint":"任务主体，没有则空串"}\n'
                "规则：\n"
                "- task：需要读写文件、运行命令、查网页/天气/新闻、搜索、播放音乐、打开应用、"
                "安装软件、发消息等任何对外部世界产生动作的请求。\n"
                "- chat：感想、寒暄、问时间、问角色本身、评价、开玩笑，不需要操作电脑。\n"
                "- ambiguous：指代不清、缺少对象、可能只是感叹，无法安全地转成任务。\n"
                "宁可选 ambiguous，也不要猜着选 task。只输出 JSON。"
            )},
            {"role": "user", "content": text},
        ]
        try:
            content = await self._chat(messages, json_mode=True, max_tokens=200, temperature=0.1)
            data = _extract_json(content)
            intent = str(data.get("intent", "")).strip().lower()
            if intent not in ("chat", "task", "ambiguous"):
                return Intent("ambiguous", "分类结果不可用", "")
            return Intent(intent, str(data.get("reason", "")), str(data.get("task_hint", "")))
        except Exception as exc:  # noqa: BLE001 - never execute on a broken judge
            return Intent("ambiguous", f"分类失败：{type(exc).__name__}", "")

    async def normalize_task(self, text: str) -> TaskSpec:
        messages = [
            {"role": "system", "content": (
                "你是命令标准化器。把用户对角色说的话改写成给执行代理的、清晰可执行的中文任务描述。\n"
                "只输出 JSON：\n"
                '{"task":"任务描述","constraints":["约束"],"requires_confirmation":true|false,'
                '"missing":["缺少的信息"],"notes":"说明"}\n'
                "规则：\n"
                "- 去掉角色称呼、昵称、口癖、撒娇和无意义寒暄。\n"
                "- 保留目标、对象、数量、时间、地点和约束；不要擅自扩大任务范围。\n"
                "- 如果缺少执行所需的关键信息（例如文件名、要写入的内容、目标位置、数量、"
                "收件人、时间），把这些项逐条放进 missing，并把 requires_confirmation 设为 false；"
                "不要猜测、不要替用户编内容。\n"
                "- 信息完整时 missing=[]。\n"
                "- 只有信息完整、且涉及删除/覆盖/移动文件、上传、安装卸载、支付、发消息、"
                "修改系统设置或其他不可逆动作时，requires_confirmation=true。\n"
                "- 信息完整、无风险时 requires_confirmation=false。\n"
                "- 只输出 JSON。"
            )},
            {"role": "user", "content": text},
        ]
        try:
            content = await self._chat(messages, json_mode=True, max_tokens=400, temperature=0.1)
            data = _extract_json(content)
            task = str(data.get("task", "")).strip()
            if not task:
                return TaskSpec(task=text, constraints=[], requires_confirmation=True,
                                notes="标准化失败，需要确认")
            constraints = data.get("constraints") or []
            if not isinstance(constraints, list):
                constraints = [str(constraints)]
            missing = data.get("missing") or []
            if isinstance(missing, str):
                missing = [missing]
            elif not isinstance(missing, list):
                missing = [str(missing)]
            missing = [str(item).strip() for item in missing if str(item).strip()]
            if not task:
                missing = missing or ["任务目标"]
            return TaskSpec(
                task=task or text,
                constraints=[str(c) for c in constraints],
                requires_confirmation=bool(data.get("requires_confirmation", False)),
                notes=str(data.get("notes", "")),
                missing=missing,
            )
        except Exception as exc:  # noqa: BLE001
            return TaskSpec(task=text, constraints=[], requires_confirmation=False,
                            notes=f"标准化失败：{type(exc).__name__}",
                            missing=["任务的具体要求"])
