"""The operator walkthrough follows the reference cards: fail when a card changes and the plan was not looked at again."""
from tests.operator import cardsteps as C
from tests.operator.plan import PLAN


def test_every_card_item_is_in_the_walkthrough_plan_with_the_same_text():
    seen = {}
    for card in C.CARDS:
        for it in C.items(card):
            seen[it["id"]] = it
    missing = sorted(set(seen) - set(PLAN))
    stale = sorted(i for i, it in seen.items() if i in PLAN and PLAN[i][0] != it["sha"])
    gone = sorted(set(PLAN) - set(seen))
    assert not missing, f"new card items not in tests/operator/plan.py: {missing}"
    assert not stale, f"card text changed (update the walkthrough and the hash in plan.py): {stale}"
    assert not gone, f"plan items no longer on a card: {gone}"


def test_every_plan_item_says_how_it_is_exercised():
    assert {v[1] for v in PLAN.values()} <= {"install", "ui", "ui+sandbox", "unit", "human"}
    assert all(v[2] for v in PLAN.values())


def test_unit_tests_named_in_the_plan_exist():
    import re
    from pathlib import Path
    root = Path(__file__).resolve().parent.parent
    for pid, (_, how, note) in PLAN.items():
        if how == "unit":
            for f in re.findall(r"tests/(test_\w+\.py)", note):
                assert (root / "tests" / f).exists(), f"{pid}: {f} does not exist"
