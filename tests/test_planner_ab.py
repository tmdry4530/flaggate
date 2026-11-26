"""A/B split wordings, rollout + A/B in one sentence, and the verbatim prompts of the final demo video."""
from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

from core_fakes import demo_snapshot
from flaggate.planner import GRAMMAR_SHORT, PlannerError, plan_from_prompt

REPO = Path(__file__).resolve().parents[1]

# verbatim: dev3's demo request (demo-video/final/scenes.py TASK, typed in the final video and given to Codex)
DEMO_TASK = ("Product request: roll out the new checkout (feature new_checkout) to 25% of pro users and A/B test the "
             "button colour (feature checkout_button_color): green 30%, orange 30%, blue 40%. Keep exposure under 20% "
             "of users.")
# verbatim: the keyless-planner fallback of the same scene (demo-video/final/scenes.py)
DEMO_FALLBACK = ("roll out new_checkout to 25% of pro users; a/b test checkout_button_color green vs orange 30/30 "
                 "keeping exposure under 20%")
# verbatim: Codex CLI's first and second plan_from_prompt calls for the demo prompt on main 61361f3 (first one failed)
CODEX_FIRST = ("roll out new_checkout to 25% of pro users with max exposure 20%; a/b test checkout_button_color green "
               "vs orange vs blue [30/30/40] with max exposure 20%")
CODEX_SECOND = ("roll out new_checkout to 25% of pro users; a/b test checkout_button_color green vs orange vs blue "
                "30/30/40; keeping total exposure under 20% of all users")
# verbatim: Codex CLI's first plan_from_prompt calls for the same demo prompt after this change (run1, run2)
CODEX_AFTER = [
    "roll out new_checkout to 25% of pro users and a/b test checkout_button_color green 30%, orange 30%, blue 40%, "
    "keeping total exposure under 20% of users",
    "roll out new_checkout to 25% of pro users; a/b test checkout_button_color green 30%, orange 30%, blue 40%; "
    "keeping total exposure under 20% of all users",
]
# verbatim: EL's examples
EL_AB = "A/B test checkout_button_color green 30%, orange 30%, blue 40%"
EL_COMBINED = ("roll out new_checkout to 25% of pro users and A/B test the button color green/orange 50/50, keeping "
               "exposure under 20%")

W_30_30_40 = {"blue": 40.0, "green": 30.0, "orange": 30.0}
W_50_50 = {"blue": 0.0, "green": 50.0, "orange": 50.0}


def summary(prompt, snap=None):
    p = plan_from_prompt(prompt, snap or demo_snapshot())
    weights = [o.weights for o in p.ops if o.op == "set_mv_weights"]
    seg = [(c.property, c.operator, c.value) for o in p.ops if o.op == "create_segment" for c in o.rules[0].conditions]
    return [o.op for o in p.ops], (weights[0] if weights else None), seg, p.guardrails.max_exposure_pct


def test_demo_task_is_verbatim_from_scenes_py():
    tree = ast.parse((REPO / "demo-video/final/scenes.py").read_text())
    lines = next(ast.literal_eval(n.value) for n in tree.body
                 if isinstance(n, ast.Assign) and getattr(n.targets[0], "id", "") == "TASK_LINES")
    src = (REPO / "demo-video/final/scenes.py").read_text()
    assert " ".join(lines) == DEMO_TASK
    assert "fg plan \"" + DEMO_FALLBACK[:60] in src.replace("'\n               '", "")


@pytest.mark.parametrize("prompt, weights", [
    (DEMO_TASK, W_30_30_40), (DEMO_FALLBACK, W_30_30_40), (CODEX_FIRST, W_30_30_40), (CODEX_SECOND, W_30_30_40),
    *[(c, W_30_30_40) for c in CODEX_AFTER],
    (EL_COMBINED, W_50_50),
])
def test_rollout_plus_ab_sentences(prompt, weights):
    ops, w, seg, cap = summary(prompt)
    assert ops == ["create_segment", "set_segment_override", "set_mv_weights"]
    assert seg == [("plan", "EQUAL", "pro"), ("", "PERCENTAGE_SPLIT", "25")]
    assert w == weights and cap == 20


@pytest.mark.parametrize("prompt, weights", [
    (EL_AB, W_30_30_40),
    ("a/b test checkout_button_color green 30%, orange 30% and blue 40%", W_30_30_40),
    ("a/b test checkout_button_color with green at 30%, orange at 30%, blue at 40%", W_30_30_40),
    ("a/b test checkout_button_color green/orange 50/50", W_50_50),
    ("a/b test checkout_button_color green/orange/blue 30%/30%/40%", W_30_30_40),
    ("split checkout_button_color 50/50 between green and orange", W_50_50),
    ("split checkout_button_color 30/30/40 between green, orange and blue", W_30_30_40),
    ("a/b test checkout_button_color between green and orange 50/50", W_50_50),
    ("a/b test the checkout_button_color feature green vs orange 50/50", W_50_50),
    ("a/b test the checkout_button_color flag green and orange 50/50", W_50_50),
    ("A/B test the button colors green 50% and orange 50%", W_50_50),
    ("a/b test the button colour green vs orange (50/50)", W_50_50),
    ("a/b test checkout_button_color green vs orange vs blue [30/30/40]", W_30_30_40),
    ("a/b test checkout_button_color green vs orange 50/50", W_50_50),          # the old form is unchanged
])
def test_ab_split_wordings(prompt, weights):
    ops, w, _, _ = summary(prompt)
    assert ops == ["set_mv_weights"] and w == weights


@pytest.mark.parametrize("prompt", [
    "a/b test checkout_button_color green 30%, orange 30%",
    "a/b test checkout_button_color green vs orange 30/30",
    "split checkout_button_color 30/30 between green and orange",
])
def test_unlisted_control_of_existing_feature_gets_the_rest(prompt):
    assert summary(prompt)[1] == W_30_30_40


@pytest.mark.parametrize("prompt, msg", [
    ("a/b test checkout_button_color green 30%, orange 30%, blue 30%", "sums to 90%, not 100%"),  # control listed
    ("a/b test checkout_button_color green 60%, orange 50%", "sums to 110%, not 100%"),
    ("a/b test hero_copy short 30%, long 30%", "sums to 60%, not 100%"),                        # new feature
    ("a/b test checkout_button_color green vs orange vs green 50/30/20", "listed twice"),
    ("a/b test checkout_button_color green/orange 50/30/20", "3 parts for 2 variants"),
])
def test_bad_splits_are_clear_errors(prompt, msg):
    with pytest.raises(PlannerError, match=re.escape(msg)):
        plan_from_prompt(prompt, demo_snapshot())


def test_new_mv_feature_from_percent_list():
    p = plan_from_prompt("a/b test hero_copy short 50%, long 50%", demo_snapshot())
    assert [o.op for o in p.ops] == ["create_mv_feature"] and p.ops[0].weights == {"short": 50.0, "long": 50.0}


@pytest.mark.parametrize("prompt, bad", [
    ("a/b test checkout_button_color green/orange 50/50 and make it sparkle", "make it sparkle"),
    ("A/B test checkout_button_color green 30%, orange 30%, blue 40%, notify the sales team", "notify the sales team"),
    ("a/b test the checkout thing green/orange 50/50", "checkout thing"),
])
def test_ab_text_is_never_dropped(prompt, bad):
    with pytest.raises(PlannerError, match="could not understand") as e:
        plan_from_prompt(prompt, demo_snapshot())
    assert bad in str(e.value)


def test_description_must_match_exactly_one_feature():
    snap = demo_snapshot()
    snap.features["checkout_button_size"] = snap.features["checkout_button_color"]
    with pytest.raises(PlannerError, match="matches several features: checkout_button_color, checkout_button_size"):
        plan_from_prompt("a/b test the checkout button green/orange 50/50", snap)


def test_parenthesised_name_must_fit_its_description():
    with pytest.raises(PlannerError, match="does not describe feature 'new_checkout'"):
        plan_from_prompt("roll out the payment page (feature new_checkout) to 25% of pro users", demo_snapshot())


def test_grammar_short_ab_examples_parse():
    assert "green 30%, orange 30%, blue 40%" in GRAMMAR_SHORT and "between green and orange" in GRAMMAR_SHORT
    examples = re.findall(r"'([^']*(?:a/b test|split )[^']*)'", GRAMMAR_SHORT)
    examples = [e for e in examples if "<" not in e]
    assert len(examples) >= 4
    for e in examples:
        assert "set_mv_weights" in summary(e)[0], e
