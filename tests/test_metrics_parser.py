from __future__ import annotations

from datetime import UTC, datetime

import pytest

from jarvis_dashboard.domain import (
    COMPUTED_INPUT,
    GENERATED_OUTPUT,
    MAX_EXACT_UPSTREAM,
    REQUEST_INPUT,
    REQUEST_OUTPUT,
)
from jarvis_dashboard.metrics import CounterValueError, parse_exact_integer_lexeme, parse_scrape

T0 = datetime(2026, 1, 1, 10, 0, 0, tzinfo=UTC)

ENGINE0 = {"model_name": "fixture-model", "engine": "0"}
ENGINE1 = {"model_name": "fixture-model", "engine": "1"}
M0 = {"model_name": "m", "engine": "0"}
M1 = {"model_name": "m", "engine": "1"}


def body(*lines: str) -> str:
    return "\n".join(lines) + "\n"


def scrape_of(text: str, at: datetime = T0):
    return parse_scrape(text, at)


def test_exact_integer_forms_accepted():
    # Scenario C21/C22 lexeme grammar.
    assert parse_exact_integer_lexeme("1000") == 1000
    assert parse_exact_integer_lexeme("1e3") == 1000
    assert parse_exact_integer_lexeme("1.0E3") == 1000
    assert parse_exact_integer_lexeme("0") == 0
    assert parse_exact_integer_lexeme("00010") == 10
    assert parse_exact_integer_lexeme("0.0") == 0
    assert parse_exact_integer_lexeme("1.5e2") == 150


def test_fractional_rejected_not_rounded():
    with pytest.raises(CounterValueError) as e:
        parse_exact_integer_lexeme("1.5")
    assert e.value.code == "fractional_value"
    with pytest.raises(CounterValueError):
        parse_exact_integer_lexeme("1e-1")  # 0.1 is fractional, truncation to 0 is silent corruption


def test_nonfinite_and_unsupported_rejected():
    for bad in ("NaN", "Inf", "-Inf", "+Inf", "0x10", "1_000", "12abc", "", "  ", "+", "-"):
        with pytest.raises(CounterValueError):
            parse_exact_integer_lexeme(bad)


def test_negative_rejected():
    with pytest.raises(CounterValueError) as e:
        parse_exact_integer_lexeme("-5")
    assert e.value.code == "negative_value"


def test_js_safe_integer_boundary():
    assert parse_exact_integer_lexeme(str(MAX_EXACT_UPSTREAM)) == 2**53 - 1
    with pytest.raises(CounterValueError) as e:
        parse_exact_integer_lexeme(str(MAX_EXACT_UPSTREAM + 1))
    assert e.value.code == "above_exact_integer_range"
    # 2^53 exactly is beyond the accepted range
    with pytest.raises(CounterValueError):
        parse_exact_integer_lexeme("9007199254740992")


def test_grammar_length_and_exponent_bounds():
    with pytest.raises(CounterValueError):
        parse_exact_integer_lexeme("1" * 65)
    with pytest.raises(CounterValueError):
        parse_exact_integer_lexeme("1e99999999999999999999")


def test_selected_counter_values_parse():
    text = body(
        "# TYPE radiance:prefill_tokens_total counter",
        'radiance:prefill_tokens_total{model_name="fixture-model",engine="0"} 1000',
        'radiance:decode_tokens_total{model_name="fixture-model",engine="0"} 100',
        'vllm:prompt_tokens_total{model_name="fixture-model",engine="0"} 7',
        'vllm:generation_tokens_total{model_name="fixture-model",engine="0"} 9',
    )
    res = scrape_of(text)
    assert res.scrape_error is None
    c = res.for_labels(ENGINE0)
    assert c is not None
    assert c.values == {COMPUTED_INPUT: 1000, GENERATED_OUTPUT: 100, REQUEST_INPUT: 7, REQUEST_OUTPUT: 9}
    assert c.primary_usable()


def test_label_order_does_not_change_identity():
    a = scrape_of(body('radiance:prefill_tokens_total{model_name="m",engine="0"} 1', 'radiance:decode_tokens_total{engine="0",model_name="m"} 1'))
    b = scrape_of(body('radiance:prefill_tokens_total{engine="0",model_name="m"} 1', 'radiance:decode_tokens_total{model_name="m",engine="0"} 1'))
    assert set(a.identities) == set(b.identities)
    assert list(a.identities)[0] == list(b.identities)[0]


def test_engine1_is_separate_identity():
    text = body(
        'radiance:prefill_tokens_total{model_name="m",engine="0"} 1000',
        'radiance:decode_tokens_total{model_name="m",engine="0"} 100',
        'radiance:prefill_tokens_total{model_name="m",engine="1"} 5',
        'radiance:decode_tokens_total{model_name="m",engine="1"} 6',
    )
    res = scrape_of(text)
    assert len(res.identities) == 2
    assert res.for_labels(M0).values[COMPUTED_INPUT] == 1000
    assert res.for_labels(M1).values[COMPUTED_INPUT] == 5


def test_escaped_labels_parsed_not_comma_split():
    text = body(r'radiance:prefill_tokens_total{model_name="a\"b",engine="0"} 1', 'radiance:decode_tokens_total{model_name="a\\"b",engine="0"} 1')
    res = scrape_of(text)
    c = res.identities.get('{"engine":"0","model_name":"a\\"b"}')
    assert c is not None, res.identities.keys()
    assert c.values[COMPUTED_INPUT] == 1


def test_missing_primary_member_rejects_whole_pair():
    # Scenario B15: generated_output invalid -> computed_input also unusable.
    text = body(
        'radiance:prefill_tokens_total{model_name="m",engine="0"} 1000',
        'radiance:decode_tokens_total{model_name="m",engine="0"} 1.5',
        'vllm:prompt_tokens_total{model_name="m",engine="0"} 7',
    )
    c = scrape_of(text).for_labels(M0)
    assert not c.primary_usable()
    assert c.values[COMPUTED_INPUT] is None
    assert c.values[GENERATED_OUTPUT] is None
    # optionals stay independent
    assert c.values[REQUEST_INPUT] == 7


def test_duplicate_selected_series_invalid():
    text = body(
        'radiance:prefill_tokens_total{model_name="m",engine="0"} 1',
        'radiance:prefill_tokens_total{model_name="m",engine="0"} 2',
        'radiance:decode_tokens_total{model_name="m",engine="0"} 1',
    )
    c = scrape_of(text).for_labels(M0)
    assert not c.primary_usable()
    assert c.problems.get(COMPUTED_INPUT) == "duplicate_sample"


def test_unrelated_metrics_and_helpers_ignored():
    text = body(
        "# HELP http_requests_total x",
        "# TYPE http_requests_total counter",
        'http_requests_total{model_name="m",engine="0"} 999',
        "vllm:num_requests_running 3",
        'radiance:prefill_tokens_created{model_name="m",engine="0"} 1.7e9',
        'radiance:prefill_tokens_total{model_name="m",engine="0"} 1',
        'radiance:decode_tokens_total{model_name="m",engine="0"} 1',
    )
    res = scrape_of(text)
    c = res.for_labels(M0)
    assert c.values[COMPUTED_INPUT] == 1
    assert len(res.identities) == 1


def test_above_exact_range_value_poisons_only_its_counter():
    text = body(
        f'radiance:prefill_tokens_total{{model_name="m",engine="0"}} {MAX_EXACT_UPSTREAM + 2}',
        'radiance:decode_tokens_total{model_name="m",engine="0"} 5',
        'vllm:prompt_tokens_total{model_name="m",engine="0"} 5',
    )
    c = scrape_of(text).for_labels(M0)
    assert c.values[COMPUTED_INPUT] is None
    assert c.problems[COMPUTED_INPUT] == "above_exact_integer_range"
    assert c.values[REQUEST_INPUT] == 5  # untouched other counter


def test_untrustworthy_exposition_rejects_whole_scrape():
    res = scrape_of(body('radiance:prefill_tokens_total{model_name="m" 1'))  # broken syntax
    assert res.scrape_error == "exposition_invalid"
    assert res.identities == {}


def test_missing_metric_family_reported():
    res = scrape_of(body('radiance:prefill_tokens_total{model_name="m",engine="0"} 1'))
    assert "radiance:decode_tokens_total" in res.missing_metrics


def test_too_many_identities_rejects_scrape():
    lines = []
    for i in range(300):
        lines.append(f'radiance:prefill_tokens_total{{model_name="m",engine="0",shard="{i}"}} 1')
        lines.append(f'radiance:decode_tokens_total{{model_name="m",engine="0",shard="{i}"}} 1')
    res = scrape_of(body(*lines))
    assert res.scrape_error == "too_many_selected_identities"
