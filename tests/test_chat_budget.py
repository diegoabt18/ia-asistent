from app.services.assistant_service import THINK_MIN_MAX_TOKENS, effective_max_tokens


def test_without_thinking_the_requested_budget_is_kept() -> None:
    assert effective_max_tokens(512, think=False) == 512


def test_thinking_raises_a_short_budget_to_the_minimum() -> None:
    assert effective_max_tokens(512, think=True) == THINK_MIN_MAX_TOKENS


def test_thinking_keeps_a_budget_already_above_the_minimum() -> None:
    assert effective_max_tokens(THINK_MIN_MAX_TOKENS, think=True) == THINK_MIN_MAX_TOKENS
