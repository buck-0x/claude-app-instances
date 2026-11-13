from claude_instances.scheduler import STATE_INTERVAL_S, LiveScheduler, backoff_after
from claude_instances.usage import LIVE_INTERVAL_S


def make(names):
    t = [1000.0]
    s = LiveScheduler(now=lambda: t[0])
    s.set_candidates(names)
    return t, s


def test_new_candidates_due_immediately_one_at_a_time():
    t, s = make(["A", "B"])
    assert s.next_due() == "A"
    s.start("A")
    assert s.next_due() is None
    s.finish("A", ok=True)
    assert s.next_due() == "B"


def test_success_schedules_five_minutes():
    t, s = make(["A"])
    s.start("A")
    s.finish("A", ok=True)
    t[0] += LIVE_INTERVAL_S - 1
    assert s.next_due() is None
    t[0] += 1
    assert s.next_due() == "A"


def test_failure_backs_off():
    t, s = make(["A"])
    s.start("A")
    s.finish("A", ok=False)
    t[0] += 599
    assert s.next_due() is None
    t[0] += 1
    assert s.next_due() == "A"
    s.start("A")
    s.finish("A", ok=False)
    t[0] += 1199
    assert s.next_due() is None
    t[0] += 1
    assert s.next_due() == "A"
    s.start("A")
    s.finish("A", ok=True)
    t[0] += LIVE_INTERVAL_S
    assert s.next_due() == "A"  # success reset the failure count


def test_backoff():
    assert [backoff_after(n) for n in (0, 1, 2, 3, 4)] == [300, 600, 1200, 2400, 3600]
    assert STATE_INTERVAL_S == 10


def test_request_now_overrides():
    t, s = make(["A", "B"])
    for name in ("A", "B"):
        s.start(name)
        s.finish(name, ok=True)
    assert s.next_due() is None
    s.request_now("B")
    assert s.next_due() == "B"
    s.request_now("unknown")  # not a candidate: ignored
    s.start("B")
    s.finish("B", ok=True)
    assert s.next_due() is None


def test_dropped_candidate_forgotten():
    t, s = make(["A", "B"])
    s.start("A")
    s.finish("A", ok=False)
    s.set_candidates(["B"])
    s.start("B")
    s.finish("B", ok=True)
    assert s.next_due() is None
    s.set_candidates(["A", "B"])
    assert s.next_due() == "A"  # re-added: due immediately, failures forgotten
    s.start("A")
    s.finish("A", ok=False)
    t[0] += 599
    assert s.next_due() == "B"  # B is due; A is not yet
    s.start("B")
    s.finish("B", ok=True)
    assert s.next_due() is None
    t[0] += 1
    assert s.next_due() == "A"  # one failure (+600), not two (+1200)
