import ast
import tempfile
from pathlib import Path
from unittest.mock import patch

from streamlit.testing.v1 import AppTest

from app.config import config
from app.models import const
from app.services import llm, topic_backlog


ROOT_DIR = Path(__file__).parent.parent.parent
WEBUI_MAIN = ROOT_DIR / "webui" / "Main.py"


def _function(tree, name):
    return next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == name
    )


def _load_function(name, namespace):
    tree = ast.parse(WEBUI_MAIN.read_text(encoding="utf-8"))
    module = ast.fix_missing_locations(
        ast.Module(body=[_function(tree, name)], type_ignores=[])
    )
    exec(compile(module, str(WEBUI_MAIN), "exec"), namespace)
    return namespace[name]


def _widget_by_key(elements, key):
    return next(item for item in elements if str(getattr(item, "key", "")) == key)


def _temp_backlog():
    temp_dir = tempfile.TemporaryDirectory()
    patcher = patch.object(
        topic_backlog.utils, "storage_dir", return_value=temp_dir.name
    )
    patcher.start()
    return temp_dir, patcher


def _run_app(**session_state):
    with patch.object(config, "save_config"):
        app = AppTest.from_file(str(WEBUI_MAIN), default_timeout=30)
        app.session_state["ui_language"] = "en"
        for key, value in session_state.items():
            app.session_state[key] = value
        app.run()
    return app


# --- auto-marking -----------------------------------------------------------


def _settle_namespace(marked, active_tasks):
    class _FakeBacklog:
        STATUS_PENDING = topic_backlog.STATUS_PENDING

        @staticmethod
        def mark_made(subject, task_id=None):
            marked.append((subject, task_id))
            return True

    removed = []
    return {
        "const": const,
        "topic_backlog": _FakeBacklog,
        "logger": type("L", (), {"warning": staticmethod(lambda *a, **k: None)})(),
        "_active_generation_tasks": lambda: active_tasks,
        "_remove_active_generation_task": removed.append,
    }, removed


def test_successful_generation_marks_the_subject_made():
    marked = []
    namespace, removed = _settle_namespace(
        marked, {"task-1": {"subject": "Why volcanoes erupt"}}
    )
    settle = _load_function("_settle_generation_task", namespace)

    settle("task-1", const.TASK_STATE_COMPLETE)

    assert marked == [("Why volcanoes erupt", "task-1")]
    assert removed == ["task-1"]


def test_failed_generation_does_not_mark_the_subject_made():
    """
    失败的任务没有视频产出。一并标记会让用户误以为该选题已经拍过，
    于是永远不会重试这条选题。
    """
    marked = []
    namespace, removed = _settle_namespace(
        marked, {"task-1": {"subject": "Why volcanoes erupt"}}
    )
    settle = _load_function("_settle_generation_task", namespace)

    settle("task-1", const.TASK_STATE_FAILED)

    assert marked == []
    assert removed == ["task-1"]


def test_subject_falls_back_to_the_stored_task_params():
    """刷新页面后会话记录会丢失，此时要从任务自身保存的参数里取选题。"""
    marked = []
    namespace, _ = _settle_namespace(marked, {})
    settle = _load_function("_settle_generation_task", namespace)

    settle(
        "task-1",
        const.TASK_STATE_COMPLETE,
        {"params": {"video_subject": "How yeast works"}},
    )

    assert marked == [("How yeast works", "task-1")]


def test_placeholder_subject_equal_to_task_id_is_not_marked():
    """_add_active_generation_task 在没有主题时回退成 task_id，不能当选题归档。"""
    marked = []
    namespace, _ = _settle_namespace(marked, {"task-1": {"subject": "task-1"}})
    settle = _load_function("_settle_generation_task", namespace)

    settle("task-1", const.TASK_STATE_COMPLETE)

    assert marked == []


# --- panel behaviour --------------------------------------------------------


def test_backlog_panel_is_rendered_in_script_settings():
    tree = ast.parse(WEBUI_MAIN.read_text(encoding="utf-8"))
    script_settings = _function(tree, "_render_script_settings")

    assert any(
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "_render_subject_backlog"
        for node in ast.walk(script_settings)
    )


def test_generated_subjects_are_written_to_the_backlog():
    temp_dir, patcher = _temp_backlog()
    try:
        with patch.object(
            llm,
            "generate_topics_from_trends",
            return_value=["Why volcanoes erupt", "How yeast works"],
        ):
            app = _run_app(trending_hashtags="#volcano #baking")
            assert not app.exception
            _widget_by_key(app.button, "generate_trending_topics").click().run()

        assert not app.exception
        assert topic_backlog.pending_subjects() == [
            "Why volcanoes erupt",
            "How yeast works",
        ]
    finally:
        patcher.stop()
        temp_dir.cleanup()


def test_pending_subject_can_be_used_and_marked_made():
    temp_dir, patcher = _temp_backlog()
    try:
        topic_backlog.add_subjects(["Why volcanoes erupt"])

        app = _run_app()
        assert not app.exception

        _widget_by_key(app.button, "backlog_use_0").click().run()
        assert app.session_state["video_subject"] == "Why volcanoes erupt"

        _widget_by_key(app.button, "backlog_made_0").click().run()
        assert not app.exception
        assert topic_backlog.pending_subjects() == []
        assert (
            topic_backlog.load_backlog()[0]["status"] == topic_backlog.STATUS_MADE
        )
    finally:
        patcher.stop()
        temp_dir.cleanup()


def test_made_subject_can_be_restored():
    temp_dir, patcher = _temp_backlog()
    try:
        topic_backlog.add_subjects(["Why volcanoes erupt"])
        topic_backlog.mark_made("Why volcanoes erupt")

        app = _run_app()
        assert not app.exception

        _widget_by_key(app.button, "backlog_restore_0").click().run()
        assert not app.exception
        assert topic_backlog.pending_subjects() == ["Why volcanoes erupt"]
    finally:
        patcher.stop()
        temp_dir.cleanup()


def test_empty_backlog_renders_without_error():
    temp_dir, patcher = _temp_backlog()
    try:
        app = _run_app()
        assert not app.exception
        assert any("No subjects yet" in item.value for item in app.caption)
    finally:
        patcher.stop()
        temp_dir.cleanup()
