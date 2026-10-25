import asyncio
import dataclasses

from eidolon_assistant.config import Character, Settings
from eidolon_assistant.orchestrator import Orchestrator, PendingQuestion
from eidolon_assistant.store import Store


def build(tmp_path, monkeypatch):
    monkeypatch.setenv("EIDOLON_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("EIDOLON_PERMISSION_MODE", "read-only")
    settings = Settings.load()
    settings = dataclasses.replace(settings, wake=dataclasses.replace(settings.wake, enabled=False))
    character = Character.load(settings.character_id)
    store = Store(tmp_path / "t.sqlite3")
    events = []

    async def broadcast(event):
        events.append(event)

    return Orchestrator(settings, character, store, broadcast), store, events


def test_confirm_label_allows_pending_permission(tmp_path, monkeypatch) -> None:
    orch, _, _ = build(tmp_path, monkeypatch)

    async def scenario():
        orch._loop = asyncio.get_running_loop()
        future = asyncio.get_running_loop().create_future()
        orch._pending = PendingQuestion(id="perm_1", kind="permission", future=future,
                                        allow_words={"可以"}, deny_words={"不要"})
        orch._on_kws_label("confirm|可以")
        return await asyncio.wait_for(future, timeout=1)

    assert asyncio.run(scenario()) is True
    asyncio.run(orch.llm.aclose())


def test_confirm_label_denies_pending_permission(tmp_path, monkeypatch) -> None:
    orch, _, _ = build(tmp_path, monkeypatch)

    async def scenario():
        orch._loop = asyncio.get_running_loop()
        future = asyncio.get_running_loop().create_future()
        orch._pending = PendingQuestion(id="perm_2", kind="task", future=future,
                                        allow_words={"可以"}, deny_words={"不要"})
        orch._on_kws_label("confirm|不要")
        return await asyncio.wait_for(future, timeout=1)

    assert asyncio.run(scenario()) is False
    asyncio.run(orch.llm.aclose())


def test_state_dedupes(tmp_path, monkeypatch) -> None:
    orch, _, events = build(tmp_path, monkeypatch)

    async def scenario():
        await orch._set_state("idle")
        await orch._set_state("idle")
        await orch._set_state("thinking")
        await orch._set_state("thinking", "x")

    # initial state is idle, so the first call is a duplicate
    asyncio.run(scenario())
    states = [(event["state"], event.get("detail", "")) for event in events if event["type"] == "state"]
    assert states == [("thinking", ""), ("thinking", "x")]
    asyncio.run(orch.llm.aclose())


def test_new_voice_attempt_clears_stale_cancel(tmp_path, monkeypatch) -> None:
    """An 中断 from an earlier turn must not swallow the next utterance."""
    orch, _, events = build(tmp_path, monkeypatch)

    async def returns_text(pcm, sample_rate):
        return "帮我查询后天的沈阳天气"

    async def scenario():
        orch.stt.transcribe_pcm = returns_text
        called = {}

        async def fake_turn(text):
            called["text"] = text

        orch._turn_after_transcription = fake_turn
        orch._cancel_requested = True  # leftover from an earlier 中断
        await orch._transcribe_and_turn(b"\x00" * 3200)
        return called

    called = asyncio.run(scenario())
    assert called["text"] == "帮我查询后天的沈阳天气"
    assert any(event.get("type") == "message" and event.get("role") == "user" for event in events)
    asyncio.run(orch.llm.aclose())


def test_cancel_during_stt_aborts_with_a_message(tmp_path, monkeypatch) -> None:
    orch, _, events = build(tmp_path, monkeypatch)

    async def cancel_then_return(pcm, sample_rate):
        orch._cancel_requested = True
        return "这句话不应该被执行"

    async def scenario():
        orch.stt.transcribe_pcm = cancel_then_return
        called = {}

        async def fake_turn(text):
            called["text"] = text

        orch._turn_after_transcription = fake_turn
        await orch._transcribe_and_turn(b"\x00" * 3200)
        return called

    called = asyncio.run(scenario())
    assert called == {}
    messages = [event.get("text", "") for event in events if event.get("type") == "message"]
    assert any("被中断" in text for text in messages)
    asyncio.run(orch.llm.aclose())


class FakeAudio:
    """Enough of AudioEngine for the wake-capture decision."""

    def __init__(self, level: float, pcm: bytes = b"\x01" * 3200) -> None:
        self.level = level
        self.pcm_value = pcm
        self.spoken: list[bytes] = []
        self.recording_calls = 0
        self.suspended = False
        self.player = __import__("types").SimpleNamespace(playing=False)

    async def record_utterance(self, *args, **kwargs):
        self.recording_calls += 1
        await asyncio.sleep(0.6)
        return self.pcm_value

    def cancel_recording(self) -> None:
        pass

    def suspend_wake(self) -> None:
        self.suspended = True

    def resume_wake(self) -> None:
        self.suspended = False

    async def speak_audio(self, audio, resume=True, cooldown_ms=None):
        self.spoken.append(audio)

    async def stop_playback(self) -> None:
        pass


def test_wake_plays_reply_then_records(tmp_path, monkeypatch) -> None:
    orch, _, _ = build(tmp_path, monkeypatch)
    fake = FakeAudio(level=0.0)
    orch.audio = fake
    orch._wake_audio = b"wake-reply"

    async def scenario():
        captured = {}

        async def fake_transcribe(pcm, source="voice"):
            captured["pcm"] = pcm

        orch._transcribe_and_turn = fake_transcribe
        await orch._capture_and_turn_inner(play_wake=True)
        return captured

    captured = asyncio.run(scenario())
    assert captured["pcm"] == fake.pcm_value
    assert fake.spoken == [b"wake-reply"], "the fixed reply plays immediately"
    assert fake.recording_calls == 1
    asyncio.run(orch.llm.aclose())


def test_missing_info_asks_then_runs_with_the_answer(tmp_path, monkeypatch) -> None:
    from eidolon_assistant.llm import Bilingual, TaskSpec

    orch, _, _ = build(tmp_path, monkeypatch)
    first = TaskSpec(task="在桌面创建文本文件", constraints=[], requires_confirmation=False,
                     missing=["文件名", "要写入的内容"])
    second = TaskSpec(task="在桌面创建 demo.txt 并写入 hello world", constraints=[],
                      requires_confirmation=False)

    async def fake_normalize(text):
        return second

    async def fake_question(spec):
        return Bilingual(ja="……ファイル名と中身は？", zh="……文件名和内容是什么？")

    async def fake_say(*args, **kwargs):
        return None

    async def fake_collect(future, timeout_s=12.0):
        orch._clarification = None
        return "文件名 demo.txt，内容 hello world"

    async def fake_run(raw, *, source, spec=None):
        ran["raw"] = raw
        ran["spec"] = spec

    ran = {}
    orch.llm.normalize_task = fake_normalize
    orch._clarification_question = fake_question
    orch._say = fake_say
    orch._collect_answer = fake_collect
    orch._run_task = fake_run

    asyncio.run(orch._dispatch_task_spec("在桌面创建一个文本文件，在那里写", first, "voice"))
    assert ran["spec"] is second
    assert "补充信息" in ran["raw"]
    assert ran["spec"].missing == []
    asyncio.run(orch.llm.aclose())


def test_typed_answer_resolves_a_pending_clarification(tmp_path, monkeypatch) -> None:
    orch, _, _ = build(tmp_path, monkeypatch)

    async def scenario():
        future = asyncio.get_running_loop().create_future()
        orch._clarification = {"future": future}
        result = await orch.handle_text("文件名 demo.txt，内容 hello world")
        return result, await future

    result, answer = asyncio.run(scenario())
    assert result == {"ok": True, "answered": "clarification"}
    assert answer == "文件名 demo.txt，内容 hello world"
    asyncio.run(orch.llm.aclose())


def test_collect_answer_returns_an_already_typed_answer(tmp_path, monkeypatch) -> None:
    orch, _, events = build(tmp_path, monkeypatch)

    async def scenario():
        future = asyncio.get_running_loop().create_future()
        future.set_result("hello world")
        orch._clarification = {"future": future}
        return await orch._collect_answer(future)

    assert asyncio.run(scenario()) == "hello world"
    assert any(event.get("type") == "message" and event.get("source") == "text"
               for event in events)
    asyncio.run(orch.llm.aclose())
