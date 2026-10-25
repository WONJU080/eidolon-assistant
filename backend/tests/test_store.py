import time

from eidolon_assistant.store import Store


def make_store(tmp_path):
    return Store(tmp_path / "test.sqlite3")


def test_task_lifecycle(tmp_path) -> None:
    store = make_store(tmp_path)
    task_id = store.create_task(
        source="voice",
        raw_input="帮我看看明天北京天气",
        normalized="查询明天北京天气",
        constraints=["用中文"],
        requires_confirmation=False,
    )
    task = store.get_task(task_id)
    assert task is not None
    assert task["normalized"] == "查询明天北京天气"
    assert task["constraints"] == ["用中文"]
    assert task["status"] == "pending"

    store.update_task(task_id, status="done", stop_reason="end_turn",
                      summary_ja="……わかった。", summary_zh="……知道了。", duration_ms=1234)
    task = store.get_task(task_id)
    assert task["status"] == "done"
    assert task["duration_ms"] == 1234
    assert task["summary_zh"] == "……知道了。"


def test_events_are_ordered_and_redacted(tmp_path) -> None:
    store = make_store(tmp_path)
    task_id = store.create_task(source="direct", raw_input="列出文件")
    store.add_event(task_id, "tool", {"command": "ls", "api_key": "abcdef123456"})
    store.add_event(task_id, "result", {"stop_reason": "end_turn"})
    events = store.get_events(task_id)
    assert [event["seq"] for event in events] == [0, 1]
    assert events[0]["payload"]["api_key"] == "***"
    assert events[0]["payload"]["command"] == "ls"


def test_redacts_secrets_in_task_fields(tmp_path) -> None:
    store = make_store(tmp_path)
    task_id = store.create_task(
        source="text",
        raw_input="token=sk-abcdefghijklmnop and /Users/ramlethal/secret",
    )
    task = store.get_task(task_id)
    assert "sk-abcdefghijklmnop" not in task["raw_input"]
    assert "/Users/ramlethal" not in task["raw_input"]


def test_list_tasks_newest_first(tmp_path) -> None:
    store = make_store(tmp_path)
    first = store.create_task(source="text", raw_input="a")
    time.sleep(0.01)
    second = store.create_task(source="text", raw_input="b")
    tasks = store.list_tasks(limit=10)
    assert [task["id"] for task in tasks] == [second, first]


def test_stale_running_rows_are_marked_interrupted(tmp_path) -> None:
    from eidolon_assistant.store import Store

    path = tmp_path / "s.sqlite3"
    store = Store(path)
    task_id = store.create_task(source="voice", raw_input="x")
    store.update_task(task_id, status="running")
    store.close()

    reopened = Store(path)
    task = reopened.get_task(task_id)
    assert task["status"] == "interrupted"
    assert task["stop_reason"] == "interrupted"
    reopened.close()
