"""Self-check for the 'end at first kill' report trimming. No network."""
from src.calendar_fetcher import reports_up_to_first_kill

BOSS = 3131
OTHER_BOSS = 3134

REPORTS = [
    {"code": "c", "startTime": 300},
    {"code": "a", "startTime": 100},
    {"code": "d", "startTime": 400},
    {"code": "b", "startTime": 200},
]


def fights(kills_by_code):
    """Fake get_boss_fights_for_report: kills only for the requested boss."""
    def _fn(code, boss_id, difficulty=5):
        killed_boss = kills_by_code.get(code)
        return [
            {"id": 1, "kill": False},
            {"id": 2, "kill": killed_boss == boss_id},
        ]
    return _fn


def codes(reports):
    return [r["code"] for r in reports]


def demo():
    # Trims to the kill log, in chronological order regardless of input order.
    got = reports_up_to_first_kill(REPORTS, BOSS, 5, fights({"b": BOSS}))
    assert codes(got) == ["a", "b"], codes(got)

    # A kill on a *different* boss in the same log is not a cutoff.
    got = reports_up_to_first_kill(
        REPORTS, BOSS, 5, fights({"b": OTHER_BOSS, "c": BOSS})
    )
    assert codes(got) == ["a", "b", "c"], codes(got)

    # Never killed: every log is progression.
    got = reports_up_to_first_kill(REPORTS, BOSS, 5, fights({}))
    assert codes(got) == ["a", "b", "c", "d"], codes(got)

    # Kill in the very first log still keeps that log (its wipes are progress).
    got = reports_up_to_first_kill(REPORTS, BOSS, 5, fights({"a": BOSS}))
    assert codes(got) == ["a"], codes(got)

    print("first-kill trimming OK")


if __name__ == "__main__":
    demo()
