import ast
from pathlib import Path
from unittest.mock import patch

from streamlit.testing.v1 import AppTest

from app.config import config
from app.services import llm, trend_sources


ROOT_DIR = Path(__file__).parent.parent.parent
WEBUI_MAIN = ROOT_DIR / "webui" / "Main.py"


def _function(tree, name):
    return next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == name
    )


def _widget_by_key(elements, key):
    return next(item for item in elements if str(getattr(item, "key", "")) == key)


def _run_app(**session_state):
    with (
        patch.object(config, "save_config"),
    ):
        app = AppTest.from_file(str(WEBUI_MAIN), default_timeout=30)
        app.session_state["ui_language"] = "en"
        for key, value in session_state.items():
            app.session_state[key] = value
        app.run()
    return app


def test_trending_panel_is_rendered_inside_script_settings():
    """入口必须挂在文案设置面板里，否则用户找不到这个功能。"""
    tree = ast.parse(WEBUI_MAIN.read_text(encoding="utf-8"))
    script_settings = _function(tree, "_render_script_settings")

    assert any(
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "_render_trending_topics"
        for node in ast.walk(script_settings)
    )


def test_trending_generation_goes_through_the_shared_llm_config_snapshot():
    """
    只读模型调用必须走 _run_llm_read_operation，才能在后台视频任务运行时
    复用同一份配置快照，而不是直接读全局配置。
    """
    tree = ast.parse(WEBUI_MAIN.read_text(encoding="utf-8"))
    function = _function(tree, "_render_trending_topics")

    called = {
        node.func.id
        for node in ast.walk(function)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    assert "_run_llm_read_operation" in called

    llm_calls = {
        node.func.attr
        for node in ast.walk(function)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "llm"
    }
    assert "generate_topics_from_trends" in llm_calls


def test_generated_subjects_are_listed_with_use_buttons():
    app = _run_app(
        trending_topic_results=[
            "Why Iceland beaches are black",
            "How sourdough starters work",
        ]
    )

    assert not app.exception
    assert any(
        "Why Iceland beaches are black" in item.value for item in app.markdown
    )
    assert _widget_by_key(app.button, "use_trending_topic_0") is not None
    assert _widget_by_key(app.button, "use_trending_topic_1") is not None


def test_use_button_fills_the_video_subject_field():
    """
    点击“使用”后主题必须真正落进输入框。Streamlit 不允许在控件实例化之后
    修改它的 session_state，因此这条路径只能走 on_click 回调。
    """
    app = _run_app(trending_topic_results=["Why Iceland beaches are black"])
    assert not app.exception

    _widget_by_key(app.button, "use_trending_topic_0").click().run()

    assert not app.exception
    assert app.session_state["video_subject"] == "Why Iceland beaches are black"


def test_mechanical_only_input_warns_without_calling_the_model():
    """全是 #fyp 这类标签时应直接提示，不发起模型请求。"""
    with patch.object(llm, "generate_topics_from_trends") as generate:
        app = _run_app(trending_hashtags="#fyp\n#viral\n#capcut")
        assert not app.exception
        _widget_by_key(app.button, "generate_trending_topics").click().run()

    assert not app.exception
    generate.assert_not_called()
    assert any("No usable hashtags" in item.value for item in app.warning)


def test_generated_subjects_are_stored_from_a_successful_run():
    with patch.object(
        llm,
        "generate_topics_from_trends",
        return_value=["Why Iceland beaches are black"],
    ) as generate:
        app = _run_app(trending_hashtags="#icelandtravel\n#sourdoughstarter")
        assert not app.exception
        _widget_by_key(app.button, "generate_trending_topics").click().run()

    assert not app.exception
    generate.assert_called_once()
    assert app.session_state["trending_topic_results"] == [
        "Why Iceland beaches are black"
    ]


def test_failed_generation_surfaces_an_error():
    with patch.object(llm, "generate_topics_from_trends", return_value=[]):
        app = _run_app(trending_hashtags="#icelandtravel")
        assert not app.exception
        _widget_by_key(app.button, "generate_trending_topics").click().run()

    assert not app.exception
    assert any("Could not generate subjects" in item.value for item in app.error)


def test_fetch_button_fills_the_textarea_from_the_selected_source():
    with patch.object(
        trend_sources,
        "fetch_trending_tags",
        return_value=["Deep sea creature", "Sourdough"],
    ) as fetch:
        app = _run_app()
        assert not app.exception
        _widget_by_key(app.button, "fetch_trending_tags").click().run()

    assert not app.exception
    fetch.assert_called_once()
    assert app.session_state["trending_hashtags"] == "Deep sea creature\nSourdough"
    assert app.session_state["trend_fetch_failed"] is False


def test_failed_fetch_warns_and_keeps_manual_input_intact():
    """拉取失败不能清空用户已经手动粘贴的内容。"""
    with patch.object(trend_sources, "fetch_trending_tags", return_value=[]):
        app = _run_app(trending_hashtags="#icelandtravel")
        assert not app.exception
        _widget_by_key(app.button, "fetch_trending_tags").click().run()

    assert not app.exception
    assert app.session_state["trending_hashtags"] == "#icelandtravel"
    assert any("Could not fetch trending topics" in i.value for i in app.warning)


def test_fetch_callback_survives_an_unexpected_service_error():
    with patch.object(
        trend_sources, "fetch_trending_tags", side_effect=RuntimeError("boom")
    ):
        app = _run_app()
        assert not app.exception
        _widget_by_key(app.button, "fetch_trending_tags").click().run()

    assert not app.exception
    assert app.session_state["trend_fetch_failed"] is True
