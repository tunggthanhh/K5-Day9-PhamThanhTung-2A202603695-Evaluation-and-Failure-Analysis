"""Statistics students implement (M2 judge calibration, M4 paired bootstrap).

Rules: a judge is a classifier -- measure it like one. A higher mean is not
"better" until the paired CI of the difference excludes 0.
Reference values to self-check are in tests/test_lab9_stats.py.
"""

from __future__ import annotations

from collections.abc import Sequence
from math import comb, isfinite

POS = "FAIL"   # the judge's job is to FIND failures, so FAIL is the positive class


def confusion(human: Sequence[str], judge: Sequence[str]) -> dict[str, int]:
    """tp/fp/fn/tn with FAIL as positive."""
    if len(human) != len(judge):
        raise ValueError("human and judge labels must have the same length")
    out = {"tp": 0, "fp": 0, "fn": 0, "tn": 0}
    for h, j in zip(human, judge):
        h_pos, j_pos = h.upper() == POS, j.upper() == POS
        out["tp" if h_pos and j_pos else "fp" if j_pos else "fn" if h_pos else "tn"] += 1
    return out


def precision_recall(human: Sequence[str], judge: Sequence[str]) -> tuple[float, float]:
    cm = confusion(human, judge)
    precision_denominator = cm["tp"] + cm["fp"]
    recall_denominator = cm["tp"] + cm["fn"]
    return (
        cm["tp"] / precision_denominator if precision_denominator else 0.0,
        cm["tp"] / recall_denominator if recall_denominator else 0.0,
    )


def cohen_kappa(human: Sequence[str], judge: Sequence[str]) -> float:
    """kappa = (p_o - p_e) / (1 - p_e). Raise ValueError when undefined (p_e == 1):
    a constant judge has no signal and must FAIL calibration, not pass on a technicality."""
    if not human:
        raise ValueError("kappa is undefined for an empty sample")
    cm = confusion(human, judge)
    total = len(human)
    observed = (cm["tp"] + cm["tn"]) / total
    human_fail = (cm["tp"] + cm["fn"]) / total
    judge_fail = (cm["tp"] + cm["fp"]) / total
    expected = human_fail * judge_fail + (1 - human_fail) * (1 - judge_fail)
    if expected == 1:
        raise ValueError("kappa is undefined when expected agreement is 1")
    return (observed - expected) / (1 - expected)


def rogan_gladen(observed_rate: float, sensitivity: float, specificity: float) -> float:
    """Correct an observed failure rate for judge error; clip to [0, 1]."""
    values = (observed_rate, sensitivity, specificity)
    if not all(isfinite(x) and 0 <= x <= 1 for x in values):
        raise ValueError("rates must be finite values in [0, 1]")
    denominator = sensitivity + specificity - 1
    if denominator == 0:
        raise ValueError("Rogan-Gladen is undefined when sensitivity + specificity equals 1")
    return min(1.0, max(0.0, (observed_rate + specificity - 1) / denominator))


def paired_bootstrap(a: Sequence[float], b: Sequence[float], *, n_resamples: int = 1000,
                     rng: int = 2027, confidence: float = 0.95) -> tuple[float, float, float]:
    """a[i], b[i] = per-item mean over seeds, SAME items, SAME order.
    Return (mean(a)-mean(b), lo, hi) with scipy.stats.bootstrap(paired=True, method="percentile",
    rng=numpy.random.default_rng(rng)). If a == b everywhere return (0.0, 0.0, 0.0)."""
    if len(a) != len(b) or not a:
        raise ValueError("paired bootstrap requires non-empty equal-length samples")
    if n_resamples < 1 or not 0 < confidence < 1:
        raise ValueError("invalid bootstrap parameters")
    import numpy as np
    from scipy.stats import bootstrap

    aa, bb = np.asarray(a, dtype=float), np.asarray(b, dtype=float)
    if not np.isfinite(aa).all() or not np.isfinite(bb).all():
        raise ValueError("samples must be finite")
    delta = float(np.mean(aa) - np.mean(bb))
    if np.array_equal(aa, bb):
        return (0.0, 0.0, 0.0)
    result = bootstrap(
        (aa, bb), lambda x, y: np.mean(x) - np.mean(y), paired=True,
        vectorized=False, n_resamples=n_resamples, confidence_level=confidence,
        method="percentile", rng=np.random.default_rng(rng),
    )
    return delta, float(result.confidence_interval.low), float(result.confidence_interval.high)


def pass_at_k(n: int, c: int, k: int) -> float:
    """Probability that AT LEAST ONE of k trials drawn from n (c of them passing) passes.
    Unbiased estimator (Chen et al. 2021): 1 - C(n-c, k) / C(n, k). Raise ValueError if k > n."""
    _validate_pass_args(n, c, k)
    return 1.0 - comb(n - c, k) / comb(n, k)


def pass_hat_k(n: int, c: int, k: int) -> float:
    """pass^k (tau-bench, Yao et al. 2024): probability that ALL k trials drawn from n pass.
    Unbiased estimator: C(c, k) / C(n, k). Raise ValueError if k > n.
    pass^k <= pass@1 <= pass@k; a reliable agent keeps pass^k close to pass@1."""
    _validate_pass_args(n, c, k)
    return comb(c, k) / comb(n, k) if c >= k else 0.0


def _validate_pass_args(n: int, c: int, k: int) -> None:
    if not all(isinstance(x, int) and not isinstance(x, bool) for x in (n, c, k)):
        raise ValueError("n, c and k must be integers")
    if n < 1 or c < 0 or c > n or k < 1 or k > n:
        raise ValueError("require n >= 1, 0 <= c <= n and 1 <= k <= n")
