from app.prompts.system_prompts import SYSTEM_PROMPT
from app.services.assistant_service import build_messages, title_from


def test_prompt_starts_with_system_and_ends_with_user_message() -> None:
    messages = build_messages([("user", "hola"), ("assistant", "¡hola!")], "¿qué tal?", 1000)
    assert messages[0] == {"role": "system", "content": SYSTEM_PROMPT}
    assert messages[-1] == {"role": "user", "content": "¿qué tal?"}
    assert [m["content"] for m in messages[1:-1]] == ["hola", "¡hola!"]


def test_history_is_trimmed_from_the_oldest_keeping_order() -> None:
    history = [("user", "a" * 100), ("assistant", "b" * 100), ("user", "c" * 100)]
    messages = build_messages(history, "pregunta", max_chars=250)
    kept = [m["content"] for m in messages[1:-1]]
    # Caben 100 + 100 caracteres del historial más reciente, más "pregunta" (8) = 208 <= 250.
    assert kept == ["b" * 100, "c" * 100]


def test_history_is_dropped_completely_when_user_message_fills_budget() -> None:
    messages = build_messages([("user", "x" * 50)], "y" * 200, max_chars=200)
    assert len(messages) == 2  # solo sistema y usuario


def test_title_is_truncated_and_normalized() -> None:
    assert title_from("  hola   mundo  ") == "hola mundo"
    assert len(title_from("a" * 200)) == 60
    assert title_from("   ") == "Nueva conversación"
