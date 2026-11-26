"""Planner accepts the natural wording coding agents actually send, merges duplicates, rejects conflicts and
never silently drops text it does not understand."""
from __future__ import annotations

import pytest

from core_fakes import demo_snapshot
from flaggate.planner import GRAMMAR_SHORT, PlannerError, plan_from_prompt

# verbatim: Codex's first plan_from_prompt call in EL's rerun (it used to be a 400)
EL_RERUN = ("Roll out the new_checkout feature to 50% of pro users, while keeping total exposure under 45% of all "
            "users. Target only pro users and enforce a 45% maximum exposure cap.")
# verbatim: Codex's first calls in the recorded runs before that
EARLIER = [
    "Roll out new_checkout to 50% of pro users, keeping exposure under 45% of users.",
    "Roll out the new_checkout feature to 50% of pro users, while keeping total exposure under 45% of all users.",
]


def ops(p):
    return [o.op for o in p.ops]


def rollout_rules(p):
    seg = next(o for o in p.ops if o.op == "create_segment")
    return seg.name, [(c.property, c.operator, c.value) for c in seg.rules[0].conditions]


@pytest.mark.parametrize("prompt", [EL_RERUN, *EARLIER])
def test_codex_first_try_sentences(prompt):
    p = plan_from_prompt(prompt, demo_snapshot())
    assert ops(p) == ["create_segment", "set_segment_override"]
    assert rollout_rules(p) == ("new_checkout_rollout_pro", [("plan", "EQUAL", "pro"), ("", "PERCENTAGE_SPLIT", "50")])
    assert p.guardrails.max_exposure_pct == 45


def test_same_plan_as_the_terse_form():
    terse = plan_from_prompt("roll out new_checkout to 50% of pro users with max exposure 45%", demo_snapshot())
    natural = plan_from_prompt(EL_RERUN, demo_snapshot())
    assert [o.model_dump() for o in natural.ops] == [o.model_dump() for o in terse.ops]
    assert natural.guardrails == terse.guardrails


@pytest.mark.parametrize("cap", [
    "max exposure 30%", "with maximum exposure 30%", "maximum exposure cap 30%", "max exposure cap of 30%",
    "enforce a 30% maximum exposure cap", "with a 30% max exposure limit", "exposure cap of 30%",
    "keep exposure below 30%", "while keeping the overall exposure within 30% of all users", "cap exposure at 30%",
])
def test_cap_wordings(cap):
    p = plan_from_prompt(f"roll out new_checkout to 10% of beta users, {cap}", demo_snapshot())
    assert p.guardrails.max_exposure_pct == 30


@pytest.mark.parametrize("prompt", [
    "Roll out the new_checkout feature to 20% of users. Target only pro users.",
    "roll out new_checkout to 20%, only for pro users",
    "roll out new_checkout to 20% and target only the pro users",
])
def test_audience_from_target_modifier(prompt):
    assert rollout_rules(plan_from_prompt(prompt, demo_snapshot()))[0] == "new_checkout_rollout_pro"


def test_enable_for_audience_is_full_rollout():
    p = plan_from_prompt("enable the dark_mode feature for pro users", demo_snapshot())
    assert ops(p) == ["create_feature", "create_segment", "set_segment_override"]
    assert rollout_rules(p)[1] == [("plan", "EQUAL", "pro")]


def test_identical_duplicates_are_merged():
    p = plan_from_prompt("roll out new_checkout to 50% of pro users. Roll out the new_checkout feature to 50% of Pro "
                         "users; target only pro users. Max exposure 45%, enforce a 45% maximum exposure cap",
                         demo_snapshot())
    assert ops(p) == ["create_segment", "set_segment_override"] and p.guardrails.max_exposure_pct == 45
    assert ops(plan_from_prompt("disable new_checkout; disable the new_checkout flag", demo_snapshot())) == ["set_flag"]


@pytest.mark.parametrize("prompt, both", [
    ("roll out new_checkout to 50% of pro users with max exposure 45%, enforce a 30% maximum exposure cap",
     ["45%", "30%"]),
    ("roll out new_checkout to 50% of pro users. Target only free users", ["pro", "free"]),
    ("roll out new_checkout to 20%; target only pro users; target only beta users", ["pro", "beta"]),
    ("roll out new_checkout to 50% of pro users; roll out new_checkout to 10% of pro users", ["50%", "10%"]),
    ("enable new_checkout; disable new_checkout", ["enable new_checkout", "disable new_checkout"]),
])
def test_conflicts_name_both(prompt, both):
    with pytest.raises(PlannerError, match="conflicting") as e:
        plan_from_prompt(prompt, demo_snapshot())
    for part in both:
        assert part in str(e.value)


@pytest.mark.parametrize("prompt, bad", [
    ("roll out new_checkout to 50% of pro users and make the button sparkle", "make the button sparkle"),
    ("Roll out new_checkout to 50% of pro users. Notify the sales team", "Notify the sales team"),
    ("enforce a 45% maximum exposure cap and also send a slack message", "send a slack message"),
])
def test_unknown_text_is_never_dropped(prompt, bad):
    with pytest.raises(PlannerError) as e:
        plan_from_prompt(prompt, demo_snapshot())
    msg = str(e.value)
    assert "could not understand" in msg and bad in msg and "roll out <feature> to <N>%" in msg


def test_target_without_rollout_is_an_error():
    with pytest.raises(PlannerError, match="no rollout"):
        plan_from_prompt("disable new_checkout, target only pro users", demo_snapshot())


def test_values_keep_the_word_feature():
    p = plan_from_prompt("set checkout_banner to 'New feature flag'", demo_snapshot())
    assert p.ops[0].value == "New feature flag"


def test_grammar_short_example_parses():
    example = GRAMMAR_SHORT.split("Example: '", 1)[1].rstrip("'.")
    assert plan_from_prompt(example, demo_snapshot()).guardrails.max_exposure_pct == 45
