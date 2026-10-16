"""Replay evaluation of retrieval-trigger policies (issue #160).

Given a trace of a session's tool calls, score candidate trigger policies by how
well their decisions match where a retrieval SHOULD have happened, weighting
false negatives much heavier than false positives.

HONEST LIMITATION (stated in the issue): replay evaluates the *trigger decision*
against a proxy label built from observable signals in the trace. It cannot
predict how the model would behave *after* receiving different memory — that
needs live paired task evaluations. See `LIVE_PAIRED_EVAL_PLAN`. The replay
numbers rank policies by trigger quality, nothing more.

Proxy label (`should_retrieve`):
  - A consequential op (file mutation / destructive command) that was NOT
    preceded by a look-first on its topic and that the KB could have informed
    (`kb_has_hit`) SHOULD have triggered a retrieval.
  - A tool failure is the strongest regret signal: a lookup should have happened
    before the op that failed, regardless of the KB oracle, and its miss is
    weighted extra (a prior observed failure is exactly the memory #137 most
    wants surfaced).
  - When the KB oracle is unknown (None) and there was no failure, the event is
    UNLABELED and excluded from the denominator — better honest than guessed.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Optional, Protocol

# Asymmetric weights. A false negative (missed a needed lookup) costs far more
# than a false positive (an unneeded lookup), and a miss that preceded a failure
# costs more still. Overridable per call.
DEFAULT_FN_WEIGHT = 5.0
DEFAULT_FP_WEIGHT = 1.0
DEFAULT_FAILURE_MULTIPLIER = 2.0


@dataclass
class TraceEvent:
    """One observable step in a session's tool-call history."""
    seq: int
    tool: str
    consequential: bool = False
    topic: str = ""
    looked_up: bool = False       # this event IS a look-first (search/report_error)
    kb_has_hit: Optional[bool] = None  # did the KB hold something for `topic`?
    led_to_failure: bool = False  # the op failed / a fixation followed


def should_retrieve(ev: TraceEvent) -> Optional[bool]:
    """Proxy ground truth. True = a retrieval should have preceded this event;
    False = it should not have; None = unlabeled (exclude from scoring)."""
    if ev.looked_up:
        return False  # the lookup itself is never a "should have looked up" site
    if not ev.consequential:
        return False  # routine ops do not demand a look-first
    if ev.led_to_failure:
        return True   # regret signal: a lookup should have preceded a failing op
    if ev.kb_has_hit is True:
        return True
    if ev.kb_has_hit is False:
        return False  # consequential but nothing to find: a lookup would be noise
    return None       # unknown oracle, no failure: cannot label


class Policy(Protocol):
    """A retrieval-trigger policy. `step` returns whether the policy would fire a
    retrieval BEFORE `ev`, then updates internal state with `ev`."""
    name: str

    def reset(self) -> None: ...
    def step(self, ev: TraceEvent) -> bool: ...


@dataclass
class FixedInterval:
    """The legacy trigger: fire every N built-in calls since the last look-first."""
    n: int = 8
    name: str = "fixed-interval"
    _count: int = 0

    def reset(self) -> None:
        self._count = 0

    def step(self, ev: TraceEvent) -> bool:
        if ev.looked_up:
            self._count = 0
            return False
        fire = self._count >= self.n
        if fire:
            self._count = 0
        self._count += 1
        return fire


@dataclass
class RiskBased:
    """The #159 trigger: fire before a consequential op whose topic is uncovered."""
    name: str = "risk-based"
    _covered: set = field(default_factory=set)

    def reset(self) -> None:
        self._covered = set()

    def step(self, ev: TraceEvent) -> bool:
        if ev.looked_up:
            if ev.topic:
                self._covered.add(ev.topic)
            return False
        if ev.consequential and ev.topic and ev.topic not in self._covered:
            self._covered.add(ev.topic)  # the trigger performs the lookup
            return True
        return False


@dataclass
class Combined:
    """Risk-based with the fixed interval retained as a net (the #159 posture)."""
    n: int = 8
    name: str = "combined"
    _risk: RiskBased = field(default_factory=RiskBased)
    _interval: FixedInterval = field(default_factory=lambda: FixedInterval())

    def __post_init__(self) -> None:
        self._interval = FixedInterval(self.n)

    def reset(self) -> None:
        self._risk.reset()
        self._interval.reset()

    def step(self, ev: TraceEvent) -> bool:
        a = self._risk.step(ev)
        b = self._interval.step(ev)
        return a or b


@dataclass
class PolicyScore:
    name: str
    tp: int = 0
    fn: int = 0
    fp: int = 0
    fn_cost: float = 0.0
    fp_cost: float = 0.0
    labeled_positive: int = 0

    @property
    def weighted_cost(self) -> float:
        return self.fn_cost + self.fp_cost

    @property
    def recall(self) -> Optional[float]:
        d = self.tp + self.fn
        return self.tp / d if d else None

    @property
    def precision(self) -> Optional[float]:
        d = self.tp + self.fp
        return self.tp / d if d else None

    def as_dict(self) -> dict:
        return {
            "policy": self.name,
            "tp": self.tp, "fn": self.fn, "fp": self.fp,
            "fn_cost": round(self.fn_cost, 3),
            "fp_cost": round(self.fp_cost, 3),
            "weighted_cost": round(self.weighted_cost, 3),
            "recall": None if self.recall is None else round(self.recall, 3),
            "precision": None if self.precision is None else round(self.precision, 3),
        }


def score_policy(
    trace: list[TraceEvent],
    policy: Policy,
    *,
    fn_weight: float = DEFAULT_FN_WEIGHT,
    fp_weight: float = DEFAULT_FP_WEIGHT,
    failure_multiplier: float = DEFAULT_FAILURE_MULTIPLIER,
) -> PolicyScore:
    """Replay `trace` through `policy` and tally weighted FN/FP against the proxy
    label. Unlabeled events still advance policy state but are not scored."""
    policy.reset()
    sc = PolicyScore(name=policy.name)
    for ev in trace:
        fired = policy.step(ev)
        label = should_retrieve(ev)
        if label is None:
            continue
        if label:
            sc.labeled_positive += 1
            if fired:
                sc.tp += 1
            else:
                sc.fn += 1
                w = fn_weight * (failure_multiplier if ev.led_to_failure else 1.0)
                sc.fn_cost += w
        else:
            if fired:
                sc.fp += 1
                sc.fp_cost += fp_weight
    return sc


def evaluate(
    trace: list[TraceEvent],
    policies: list[Policy],
    *,
    fn_weight: float = DEFAULT_FN_WEIGHT,
    fp_weight: float = DEFAULT_FP_WEIGHT,
    failure_multiplier: float = DEFAULT_FAILURE_MULTIPLIER,
) -> list[PolicyScore]:
    """Score every policy; lower weighted_cost is better."""
    return [
        score_policy(
            trace, p, fn_weight=fn_weight, fp_weight=fp_weight,
            failure_multiplier=failure_multiplier,
        )
        for p in policies
    ]


def default_policies() -> list[Policy]:
    return [FixedInterval(8), RiskBased(), Combined(8)]


# The replay harness cannot measure post-retrieval behavior; that requires a
# controlled live comparison. This plan is also stored in the KB (issue #160).
LIVE_PAIRED_EVAL_PLAN = """\
Live paired evaluation of retrieval policies (issue #160)

Why: replay scores only the TRIGGER decision against a proxy label. It cannot
observe how the model acts after receiving (or not receiving) a memory. Only a
live comparison on real tasks can.

Design:
1. Task set: a fixed battery of real, history-dependent tasks drawn from closed
   issues whose fix depended on a prior decision/rule/failure already in the KB
   (so the "right" memory demonstrably exists).
2. Arms, same task, same model, same seed where available:
   A. no retrieval hooks (control),
   B. fixed-interval only,
   C. risk-based gate + task-aware inject (#157/#159),
   D. C plus failure recall (#158).
3. Blind grading: a rubric per task (did it use the prior decision? repeat a
   known failure? contradict an established preference?) graded without knowing
   the arm. Record tokens spent as a cost axis (the founding currency).
4. Readout: per arm, task-success rate and the two regret rates — repeated a
   known failure, violated a known constraint — which are what FN weight stands
   in for in replay. Pair by task so each task is its own control.
5. N and power: enough tasks that a one-arm difference clears the by-task
   variance (cf. the stratified-vs-proportional sampling lesson, RULE #307);
   report the spread, not just the mean.
Limits: grading is the expensive step and the real bottleneck; start with a
small battery graded carefully over a large one graded loosely.
"""
