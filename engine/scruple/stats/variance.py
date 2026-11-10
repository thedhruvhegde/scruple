"""Variance reduction for paired loss differences that keeps anytime validity.

Write-up with proofs: docs/math/07_variance_reduction.md. Everything below is about
``X_i = (C_i - B_i) / c in [-1, 1]`` (candidate minus baseline loss over a declared cap,
positive = worse), as in ``sequential.py``. Four tools:

1. Common random numbers (CRN). The baseline and the candidate are replayed on the
   same case with the same seed and the same issuer draws, so a case whose decisions
   do not change has a difference of exactly zero. ``crn_report`` measures what that
   buys (paired against unpaired variance); ``crn_violations`` checks the invariant
   (an unchanged trajectory with a non-zero difference means the pairing is broken).

2. Stratified betting, ``StratifiedBettingEProcess``. One wealth process for the
   intersection null ``H0: E[X_i | F_{i-1}, k_i] <= m_{k_i}`` for every case ("no case
   type regressed beyond its tolerance"), with one adaptive bet per case type. The
   type ``k_i`` is a pre-treatment covariate revealed before ``X_i``, so a bet that
   depends on it is predictable and every factor has conditional mean at most one.
   Against per-type e-processes combined by Bonferroni it pays no ``K``-fold
   threshold; against one pooled process it neither lets an improving type cancel a
   regressing one nor charges the between-type variance.

3. Control variates (CUPED-style), ``ControlVariateEProcess``. For the *marginal*
   null ``E[X_i | F_{i-1}] <= m`` the adjusted observation
   ``R_i = X_i - theta_i . (Z_i - nu)`` with a predictable coefficient ``theta_i``
   (ridge least squares on past data only) has the same conditional mean as ``X_i``
   when ``nu = E[Z_i | F_{i-1}]`` is known (design-known shares, or a long pre-period),
   so betting on ``R_i`` is an exact e-process. With ``nu`` only known to within a
   radius (from independent history) or learned from the stream itself, a drift
   penalty ``sum_j |theta_ij| r_ij`` keeps the test valid at level ``alpha`` plus the
   covariate interval's level (Proposition 7.4). ``PostStratifiedEProcess`` is the
   special case of stratum indicators with known shares.

4. Planning, ``stratified_cases_needed`` and ``cv_cases_needed``: the Bernstein plan of
   docs/math/03 with the level and the variance the reduced test actually has.

5. Gate composition, ``evaluate_pairs_vr``: the block family of ``evaluate_pairs``
   at ``(1 - w) alpha`` plus the stratified e-process at ``w alpha`` (so the false-block
   rate stays at most ``alpha``), and a post-stratified certificate when the type
   shares are known by design. Bets only
change power; every validity claim rests on boundedness and predictability. Money is
USD minor units; every simulation result is synthetic.
"""

from __future__ import annotations

import math
import random
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .gate_adapter import LossPair, StatsGateConfig, StatsVerdict, evaluate_pairs
from .planning import Method, StratumSpec, stratum_cases_needed
from .sequential import Alternative, BettingEProcess, _check_alpha, _check_x, safe_exp
from .simulate import Sampler

Centre = Literal["known", "running"]

# --- 1. common random numbers ----------------------------------------------------------


class CRNReport(BaseModel):
    """What pairing on the same case, seed and issuer draws buys (one sample)."""

    model_config = ConfigDict(frozen=True)

    n: int
    mean_diff: float
    var_paired: float  # Var(C - B): what the paired test pays
    var_unpaired: float  # Var(C) + Var(B): what two independent samples would pay
    correlation: float  # corr(B, C)
    share_identical: float  # share of cases with C == B exactly
    variance_ratio: float  # var_paired / var_unpaired (1 = pairing buys nothing)

    @property
    def sample_size_ratio(self) -> float:
        """Paired over unpaired cases needed at a fixed power (variance-dominated)."""
        return self.variance_ratio


def _var(xs: Sequence[float]) -> float:
    m = math.fsum(xs) / len(xs)
    return math.fsum((x - m) ** 2 for x in xs) / len(xs)


def crn_report(baseline: Sequence[float], candidate: Sequence[float]) -> CRNReport:
    """Paired against unpaired variance of the loss difference, on the same cases."""
    if len(baseline) != len(candidate) or len(baseline) < 2:
        raise ValueError("need two equally long sequences of at least two losses")
    d = [c - b for b, c in zip(baseline, candidate, strict=True)]
    vb, vc, vd = _var(baseline), _var(candidate), _var(d)
    unpaired = vb + vc
    corr = (vb + vc - vd) / (2.0 * math.sqrt(vb * vc)) if vb > 0 and vc > 0 else 0.0
    return CRNReport(
        n=len(d),
        mean_diff=math.fsum(d) / len(d),
        var_paired=vd,
        var_unpaired=unpaired,
        correlation=corr,
        share_identical=sum(1 for x in d if x == 0) / len(d),
        variance_ratio=vd / unpaired if unpaired > 0 else 1.0,
    )


def crn_violations(diffs: Sequence[float], changed: Sequence[bool]) -> list[int]:
    """Indices where the trajectory did not change but the loss did (pairing broken)."""
    if len(diffs) != len(changed):
        raise ValueError("diffs and changed must have the same length")
    return [i for i, (d, c) in enumerate(zip(diffs, changed, strict=True)) if not c and d != 0]


# --- shared bet state --------------------------------------------------------------------


@dataclass
class _Agrapa:
    """Running moments of ``Y`` for the aGRAPA bet (one pseudo-observation at 0)."""

    prior_second_moment: float = 0.25
    n: int = 0
    sum_y: float = 0.0
    sum_y2: float = 0.0

    def bet(self, lam_max: float, activation: float = 0.0) -> float:
        """``clip(ybar / s2, 0, lam_max)``; 0 unless ``ybar >= activation sqrt(s2 / k)``."""
        k = self.n + 1
        s2 = (self.prior_second_moment + self.sum_y2) / k
        ybar = self.sum_y / k
        if activation > 0 and ybar < activation * math.sqrt(s2 / k):
            return 0.0
        return min(max(ybar / s2, 0.0), lam_max)

    def add(self, y: float) -> None:
        self.n += 1
        self.sum_y += y
        self.sum_y2 += y * y


def _sign(alternative: Alternative) -> int:
    if alternative not in ("greater", "less"):
        raise ValueError("alternative must be 'greater' or 'less'")
    return 1 if alternative == "greater" else -1


# --- 2. stratified betting ---------------------------------------------------------------


@dataclass
class StratifiedBettingEProcess:
    """One e-process for "every case type's conditional mean is at most its null mean".

    ``null_means[k]`` is ``m_k`` (tolerance over cap, normalised); types not listed use
    ``default_null_mean``. Observations are normalised by the type's own cap before
    they arrive here. Each type keeps its own aGRAPA bet, capped so every factor is at
    least ``1 - max_bet``. ``contributions`` splits the log-wealth by type (they sum to
    ``log_wealth``), which shows which type carries the evidence.

    ``activation`` (in standard errors): a type is bet on only once its running mean
    of ``Y`` is at least ``activation`` standard errors above zero. Types at or below
    their null then cost almost nothing (a plug-in bet at the null loses about
    ``log(n_k) / 2`` over ``n_k`` cases, which adds up over many types), at the price of
    a later start on a type that regresses. The default 1.0 is fixed from synthetic
    Monte Carlo before E3 (docs/math/07); 0 is plain per-type aGRAPA. Validity does
    not depend on it (the bet stays predictable).
    """

    alternative: Alternative = "greater"
    null_means: Mapping[str, float] = field(default_factory=dict)
    default_null_mean: float = 0.0
    max_bet: float = 0.5
    prior_second_moment: float = 0.25
    activation: float = 1.0

    n: int = field(default=0, init=False)
    log_wealth: float = field(default=0.0, init=False)
    max_log_wealth: float = field(default=0.0, init=False)
    contributions: dict[str, float] = field(default_factory=dict, init=False)
    _bets: dict[str, _Agrapa] = field(default_factory=dict, init=False, repr=False)

    def __post_init__(self) -> None:
        _sign(self.alternative)
        if not 0.0 < self.max_bet < 1.0:
            raise ValueError("max_bet must lie in (0, 1)")
        for m in (self.default_null_mean, *self.null_means.values()):
            if not -1.0 < m < 1.0:
                raise ValueError("null means must lie strictly inside (-1, 1)")
        if self.prior_second_moment <= 0:
            raise ValueError("prior_second_moment must be positive")
        if self.activation < 0:
            raise ValueError("activation must be non-negative")

    @property
    def sign(self) -> int:
        return _sign(self.alternative)

    def null_mean(self, stratum: str) -> float:
        return self.null_means.get(stratum, self.default_null_mean)

    def lambda_max(self, stratum: str) -> float:
        return self.max_bet / (1.0 + self.sign * self.null_mean(stratum))

    def next_bet(self, stratum: str) -> float:
        """The bet for the next case of ``stratum`` (past data and its type only)."""
        st = self._bets.get(stratum)
        if st is None:
            return 0.0
        return st.bet(self.lambda_max(stratum), self.activation)

    def update(self, stratum: str, x: float) -> float:
        x = _check_x(x)
        st = self._bets.setdefault(stratum, _Agrapa(self.prior_second_moment))
        lam = st.bet(self.lambda_max(stratum), self.activation)
        y = self.sign * (x - self.null_mean(stratum))
        step = math.log1p(lam * y)
        self.log_wealth += step
        self.max_log_wealth = max(self.max_log_wealth, self.log_wealth)
        self.contributions[stratum] = self.contributions.get(stratum, 0.0) + step
        st.add(y)
        self.n += 1
        return self.e_value

    def counts(self) -> dict[str, int]:
        return {k: v.n for k, v in sorted(self._bets.items())}

    @property
    def e_value(self) -> float:
        return safe_exp(self.log_wealth)

    @property
    def max_e_value(self) -> float:
        return safe_exp(self.max_log_wealth)

    def rejected(self, alpha: float) -> bool:
        """True once the wealth has ever reached ``1/alpha`` (Ville, time-uniform)."""
        _check_alpha(alpha)
        return self.max_log_wealth >= math.log(1.0 / alpha) - 1e-12


# --- 3. control variates -------------------------------------------------------------------


def _solve(a: list[list[float]], b: list[float]) -> list[float]:
    """Gaussian elimination with partial pivoting (small, positive definite systems)."""
    n = len(b)
    m = [[*row, b[i]] for i, row in enumerate(a)]
    for col in range(n):
        piv = max(range(col, n), key=lambda r: abs(m[r][col]))
        m[col], m[piv] = m[piv], m[col]
        if abs(m[col][col]) < 1e-300:
            return [0.0] * n
        for r in range(col + 1, n):
            f = m[r][col] / m[col][col]
            for c in range(col, n + 1):
                m[r][c] -= f * m[col][c]
    out = [0.0] * n
    for r in range(n - 1, -1, -1):
        s = m[r][n] - sum(m[r][c] * out[c] for c in range(r + 1, n))
        out[r] = s / m[r][r]
    return out


def mixture_radius(t: int, alpha: float, rho: float = 100.0) -> float:
    """Time-uniform radius for the running mean of ``t`` values in [-1, 1].

    With ``S_t = sum (z_i - nu)`` and conditionally mean-zero increments in a range of
    width 2 (1-sub-Gaussian by Hoeffding's lemma), the normal mixture
    ``sqrt(rho/(t+rho)) exp(S_t^2 / (2 (t+rho)))`` is a non-negative supermartingale
    (Robbins 1970; Howard et al. 2021), so by Ville, with probability at least
    ``1 - alpha``, for all ``t``:
    ``|S_t| / t < sqrt(2 (t+rho) log(sqrt((t+rho)/rho) / alpha)) / t``.
    """
    _check_alpha(alpha)
    if t <= 0:
        return math.inf
    return math.sqrt(2.0 * (t + rho) * math.log(math.sqrt((t + rho) / rho) / alpha)) / t


@dataclass
class ControlVariateEProcess:
    """Betting on ``R_i = X_i - theta_i . (Z_i - c_i)`` for the marginal mean of ``X``.

    ``Z_i`` is a vector of ``dim`` pre-treatment covariates, each scaled into [-1, 1].
    ``theta_i`` is the ridge least-squares slope of ``X`` on ``Z`` from cases
    ``1..i-1`` only, scaled so ``||theta||_1 <= theta_max``.

    Centre ``c_i`` and validity:

    * ``centre="known"``: ``c_i = covariate_mean``. If ``covariate_radius`` is 0 this is
      ``E[Z_i | F_{i-1}]`` exactly and the wealth is an e-process (Proposition 7.3).
      A positive radius ``r_j`` (from an independent pre-period at level ``a``) adds
      the drift penalty ``sum_j |theta_j| r_j``; the test is then valid at
      ``alpha + a`` (Proposition 7.4), and the wealth is an e-process only on the
      event that the true mean is within the radius.
    * ``centre="running"``: ``c_i`` is the mean of ``Z_1..Z_{i-1}`` and the radius is
      the time-uniform ``mixture_radius`` at ``alpha_covariate / dim`` per coordinate.
      Valid at ``alpha + alpha_covariate``. ``rejected(alpha)`` spends ``alpha`` on the
      wealth; the caller accounts for ``alpha_covariate``.

    Every factor is at least ``1 - max_bet``: ``|R_i - m| <= 1 + |m| + 2 theta_max`` and
    the penalty is at most ``2 theta_max``, so the bet is capped accordingly.
    """

    null_mean: float
    alternative: Alternative
    dim: int
    covariate_mean: Sequence[float] | None = None
    covariate_radius: Sequence[float] | None = None
    centre: Centre = "known"
    alpha_covariate: float = 0.01
    theta_max: float = 1.0
    ridge: float = 1.0
    max_bet: float = 0.5
    prior_second_moment: float = 0.25
    mixture_rho: float = 100.0

    n: int = field(default=0, init=False)
    log_wealth: float = field(default=0.0, init=False)
    max_log_wealth: float = field(default=0.0, init=False)
    sum_penalty: float = field(default=0.0, init=False)
    _bet: _Agrapa = field(default_factory=_Agrapa, init=False, repr=False)
    _sx: float = field(default=0.0, init=False, repr=False)
    _sz: list[float] = field(default_factory=list, init=False, repr=False)
    _szz: list[list[float]] = field(default_factory=list, init=False, repr=False)
    _szx: list[float] = field(default_factory=list, init=False, repr=False)

    def __post_init__(self) -> None:
        _sign(self.alternative)
        if not -1.0 < self.null_mean < 1.0:
            raise ValueError("null_mean must lie strictly inside (-1, 1)")
        if self.dim < 1:
            raise ValueError("dim must be at least 1")
        if not 0.0 < self.max_bet < 1.0:
            raise ValueError("max_bet must lie in (0, 1)")
        if self.theta_max < 0 or self.ridge <= 0:
            raise ValueError("theta_max must be non-negative and ridge positive")
        if self.centre == "known":
            if self.covariate_mean is None or len(self.covariate_mean) != self.dim:
                raise ValueError("centre='known' needs covariate_mean of length dim")
            if any(not -1.0 <= v <= 1.0 for v in self.covariate_mean):
                raise ValueError("covariate means must lie in [-1, 1]")
            if self.covariate_radius is not None and (
                len(self.covariate_radius) != self.dim or min(self.covariate_radius) < 0
            ):
                raise ValueError("covariate_radius must be dim non-negative values")
        elif self.centre == "running":
            _check_alpha(self.alpha_covariate)
        else:
            raise ValueError("centre must be 'known' or 'running'")
        p = self.dim
        self._sz = [0.0] * p
        self._szz = [[0.0] * p for _ in range(p)]
        self._szx = [0.0] * p
        self._bet = _Agrapa(self.prior_second_moment)

    @property
    def sign(self) -> int:
        return _sign(self.alternative)

    @property
    def lambda_max(self) -> float:
        extra = 2.0 * self.theta_max * (1.0 if self.exact else 2.0)
        return self.max_bet / (1.0 + self.sign * self.null_mean + extra)

    @property
    def exact(self) -> bool:
        """True when the wealth is an e-process with no extra error budget."""
        return self.centre == "known" and (
            self.covariate_radius is None or max(self.covariate_radius) == 0.0
        )

    def centre_now(self) -> list[float]:
        if self.centre == "known":
            assert self.covariate_mean is not None
            return [float(v) for v in self.covariate_mean]
        if self.n == 0:
            return [0.0] * self.dim
        return [s / self.n for s in self._sz]

    def radius_now(self) -> list[float]:
        if self.centre == "known":
            if self.covariate_radius is None:
                return [0.0] * self.dim
            return [min(float(r), 2.0) for r in self.covariate_radius]
        r = mixture_radius(self.n, self.alpha_covariate / self.dim, self.mixture_rho)
        return [min(r, 2.0)] * self.dim

    def coefficient(self) -> list[float]:
        """Ridge slope of X on Z from past cases, scaled so ``||theta||_1 <= theta_max``.

        The slope is shift invariant, so it is fitted on the raw covariates."""
        p, n = self.dim, self.n
        if n < 2 or self.theta_max == 0:
            return [0.0] * p
        mz = [s / n for s in self._sz]
        mx = self._sx / n
        mat = [
            [
                self._szz[a][b] / n - mz[a] * mz[b] + (self.ridge / n if a == b else 0.0)
                for b in range(p)
            ]
            for a in range(p)
        ]
        cxy = [self._szx[a] / n - mz[a] * mx for a in range(p)]
        theta = _solve(mat, cxy)
        norm = math.fsum(abs(t) for t in theta)
        if norm > self.theta_max:
            theta = [t * self.theta_max / norm for t in theta]
        return theta

    def update(self, x: float, z: Sequence[float]) -> float:
        x = _check_x(x)
        if len(z) != self.dim:
            raise ValueError(f"expected {self.dim} covariates")
        zz = [_check_x(v) for v in z]
        theta = self.coefficient()
        c = self.centre_now()
        r = self.radius_now()
        w = [zz[j] - c[j] for j in range(self.dim)]
        adj = math.fsum(theta[j] * w[j] for j in range(self.dim))
        pen = math.fsum(abs(theta[j]) * r[j] for j in range(self.dim))
        y = self.sign * (x - adj - self.null_mean) - pen
        lam = self._bet.bet(self.lambda_max)
        self.log_wealth += math.log1p(lam * y)
        self.max_log_wealth = max(self.max_log_wealth, self.log_wealth)
        self.sum_penalty += pen
        self._bet.add(y)
        self.n += 1
        self._sx += x
        for a in range(self.dim):
            self._sz[a] += zz[a]
            self._szx[a] += zz[a] * x
            for b in range(self.dim):
                self._szz[a][b] += zz[a] * zz[b]
        return self.e_value

    @property
    def e_value(self) -> float:
        return safe_exp(self.log_wealth)

    @property
    def max_e_value(self) -> float:
        return safe_exp(self.max_log_wealth)

    def rejected(self, alpha: float) -> bool:
        """Wealth ever reached ``1/alpha``. Total level: ``alpha`` if ``exact``, else
        ``alpha`` plus the covariate interval's level (Proposition 7.4)."""
        _check_alpha(alpha)
        return self.max_log_wealth >= math.log(1.0 / alpha) - 1e-12


@dataclass
class PostStratifiedEProcess:
    """Marginal-mean e-process with stratum indicators as control variates.

    ``shares[k] = P(type = k)`` must be known (by design: stratified sampling, or a
    generator with a published mix), and types must arrive independently of the past
    with these probabilities. With ``theta_k`` the predictable running mean of type
    ``k`` (clipped to ``[-theta_max, theta_max]``),

        R_i = X_i - theta_{k_i} + sum_j shares_j theta_j

    has ``E[R_i | F_{i-1}] = E[X_i | F_{i-1}]`` (the indicators have known mean), so the
    between-type variance drops out of the bet while the null stays the marginal one.
    """

    shares: Mapping[str, float]
    null_mean: float
    alternative: Alternative
    theta_max: float = 0.5
    max_bet: float = 0.5
    prior_second_moment: float = 0.25

    n: int = field(default=0, init=False)
    log_wealth: float = field(default=0.0, init=False)
    max_log_wealth: float = field(default=0.0, init=False)
    _bet: _Agrapa = field(default_factory=_Agrapa, init=False, repr=False)
    _sum: dict[str, float] = field(default_factory=dict, init=False, repr=False)
    _cnt: dict[str, int] = field(default_factory=dict, init=False, repr=False)

    def __post_init__(self) -> None:
        _sign(self.alternative)
        if not self.shares or any(not 0.0 < v <= 1.0 for v in self.shares.values()):
            raise ValueError("shares must be a non-empty mapping of values in (0, 1]")
        if abs(math.fsum(self.shares.values()) - 1.0) > 1e-9:
            raise ValueError("shares must sum to 1")
        if not -1.0 < self.null_mean < 1.0:
            raise ValueError("null_mean must lie strictly inside (-1, 1)")
        if not 0.0 <= self.theta_max <= 1.0:
            raise ValueError("theta_max must lie in [0, 1]")
        if not 0.0 < self.max_bet < 1.0:
            raise ValueError("max_bet must lie in (0, 1)")
        self._bet = _Agrapa(self.prior_second_moment)

    @property
    def sign(self) -> int:
        return _sign(self.alternative)

    @property
    def lambda_max(self) -> float:
        return self.max_bet / (1.0 + self.sign * self.null_mean + 2.0 * self.theta_max)

    def theta(self, stratum: str) -> float:
        c = self._cnt.get(stratum, 0)
        if c == 0:
            return 0.0
        return max(-self.theta_max, min(self.theta_max, self._sum[stratum] / c))

    def offset(self) -> float:
        return math.fsum(s * self.theta(k) for k, s in self.shares.items())

    def update(self, stratum: str, x: float) -> float:
        if stratum not in self.shares:
            raise ValueError(f"unknown stratum {stratum!r}")
        x = _check_x(x)
        r = x - self.theta(stratum) + self.offset()
        y = self.sign * (r - self.null_mean)
        lam = self._bet.bet(self.lambda_max)
        self.log_wealth += math.log1p(lam * y)
        self.max_log_wealth = max(self.max_log_wealth, self.log_wealth)
        self._bet.add(y)
        self.n += 1
        self._sum[stratum] = self._sum.get(stratum, 0.0) + x
        self._cnt[stratum] = self._cnt.get(stratum, 0) + 1
        return self.e_value

    @property
    def e_value(self) -> float:
        return safe_exp(self.log_wealth)

    @property
    def max_e_value(self) -> float:
        return safe_exp(self.max_log_wealth)

    def rejected(self, alpha: float) -> bool:
        _check_alpha(alpha)
        return self.max_log_wealth >= math.log(1.0 / alpha) - 1e-12


# --- 4. planning ---------------------------------------------------------------------------


def stratified_cases_needed(
    spec: StratumSpec,
    alpha: float,
    beta: float,
    *,
    share_of_alpha: float = 1.0,
    method: Method = "bernstein",
) -> tuple[int, float]:
    """Type-``k`` cases for the stratified e-process to flag a regression in type ``k``.

    The stratified process rejects the intersection null at ``share_of_alpha * alpha``
    with no division by the number of types. The plan is the fixed-bet guarantee of
    docs/math/03 for the regressing type, with the other types' bets set to zero
    (Proposition 7.6): the adaptive bets in the other types pay a lower-order cost
    that the Monte Carlo in tests/test_stats_variance_mc.py measures.
    """
    if not 0.0 < share_of_alpha <= 1.0:
        raise ValueError("share_of_alpha must lie in (0, 1]")
    return stratum_cases_needed(spec, share_of_alpha * alpha, beta, method)


def cv_cases_needed(
    spec: StratumSpec,
    alpha: float,
    beta: float,
    *,
    r2: float,
    theta_max: float = 1.0,
    method: Method = "bernstein",
) -> tuple[int, float]:
    """Cases for the exact control-variate e-process (known covariate mean).

    ``r2`` is the share of the per-case variance the covariates explain, an
    ASSUMPTION fixed before outcomes (estimated from history, never from the run).
    The residual sd is ``sd sqrt(1 - r2)`` (the oracle linear coefficient; the
    predictable estimate converges to it). The adjusted observation lies in
    ``[-(1 + 2 theta_max), 1 + 2 theta_max]`` (Proposition 7.3), so the plan is the
    docs/math/03 plan on the cap ``c (1 + 2 theta_max)`` with the gap, tolerance and
    residual sd unchanged in money.
    """
    if not 0.0 <= r2 < 1.0:
        raise ValueError("r2 must lie in [0, 1)")
    if spec.sd is None:
        raise ValueError("a control-variate plan needs a per-case sd")
    widen = 1.0 + 2.0 * theta_max  # R / widen lies in [-1, 1]
    reduced = StratumSpec(
        name=spec.name,
        frequency=spec.frequency,
        cost_cap=spec.cost_cap * widen,
        min_detectable_regression=spec.min_detectable_regression,
        tolerance=spec.tolerance,
        sd=spec.sd * math.sqrt(1.0 - r2),
    )
    return stratum_cases_needed(reduced, alpha, beta, method)


# --- 5. gate composition ---------------------------------------------------------------------


class VRGateConfig(BaseModel):
    """``evaluate_pairs`` settings plus the variance-reduced additions."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    base: StatsGateConfig
    stratified_share: float = Field(default=0.8, gt=0.0, lt=1.0)  # w: share of alpha_block
    shares: dict[str, float] | None = None  # known type shares (post-stratified certificate)
    # Types that must each be certified non-inferior (H0_k: mean_k >= t_k rejected at
    # alpha_certify) before promoting; an intersection-union test, so no split of alpha.
    certify_types: tuple[str, ...] = ()
    max_bet: float = Field(default=0.5, gt=0.0, lt=1.0)  # stratified process's bet cap

    @model_validator(mode="after")
    def _shares(self) -> VRGateConfig:
        if self.shares is not None:
            if any(not 0.0 < v <= 1.0 for v in self.shares.values()):
                raise ValueError("shares must lie in (0, 1]")
            if abs(math.fsum(self.shares.values()) - 1.0) > 1e-9:
                raise ValueError("shares must sum to 1")
        return self


class VRVerdict(BaseModel):
    model_config = ConfigDict(frozen=True)

    outcome: Literal["promote", "hold", "block"]
    reasons: tuple[str, ...]
    base: StatsVerdict  # evaluate_pairs at (1 - w) alpha_block
    stratified_e_value: float  # running maximum, intersection null
    stratified_threshold: float  # 1 / (w alpha_block)
    stratified_rejected: bool
    contributions: dict[str, float]  # log-wealth by case type (sums to the final log-wealth)
    post_stratified_e_value: float | None  # running maximum, H0: mean >= tolerance
    type_certificates: dict[str, float]  # running maxima for certify_types
    certified: bool

    @property
    def blocked_by(self) -> tuple[str, ...]:
        out = []
        if self.base.regressed_fwer:
            out.append("family")
        if self.stratified_rejected:
            out.append("stratified")
        return tuple(out)


def evaluate_pairs_vr(pairs: Sequence[LossPair], cfg: VRGateConfig) -> VRVerdict:
    """``evaluate_pairs`` with the stratified block test and post-stratified certificate.

    P(block) <= ``alpha_block`` when no type regressed: the base family runs at
    ``(1 - w) alpha_block`` and the stratified e-process at ``w alpha_block``. The
    certificate keeps ``P(promote | mean >= tolerance) <= alpha_certify``: it is the
    base certificate, or the post-stratified one when ``shares`` are given.
    """
    b = cfg.base
    w = cfg.stratified_share
    base = evaluate_pairs(pairs, b.model_copy(update={"alpha_block": (1.0 - w) * b.alpha_block}))
    order = list(pairs)
    if b.shuffle_seed is not None:
        random.Random(b.shuffle_seed).shuffle(order)

    def cap(t: str) -> float:
        return b.type_caps.get(t, b.cost_cap)

    strat = StratifiedBettingEProcess(
        "greater",
        null_means={
            t: b.type_tolerances.get(t, 0.0) / cap(t) for t in {p.case_type for p in order}
        },
        max_bet=cfg.max_bet,
    )
    type_cert = {
        t: BettingEProcess(b.type_tolerances.get(t, 0.0) / cap(t), "less")
        for t in cfg.certify_types
    }
    post: PostStratifiedEProcess | None = None
    if cfg.shares is not None:
        missing = {p.case_type for p in order} - set(cfg.shares)
        if missing:
            raise ValueError(f"no known share for case types {sorted(missing)}")
        post = PostStratifiedEProcess(cfg.shares, b.tolerance / b.cost_cap, "less")
    for p in order:
        strat.update(p.case_type, max(-1.0, min(1.0, p.diff / cap(p.case_type))))
        if post is not None:
            post.update(p.case_type, max(-1.0, min(1.0, p.diff / b.cost_cap)))
        if p.case_type in type_cert:
            type_cert[p.case_type].update(max(-1.0, min(1.0, p.diff / cap(p.case_type))))
    level = w * b.alpha_block
    s_rej = strat.rejected(level)
    bayes_ok = base.decision is None or base.decision.bayes_promote
    certified = (
        post.rejected(b.alpha_certify) if post is not None else base.certified
    ) and bayes_ok
    uncertified = [t for t, e in type_cert.items() if not e.rejected(b.alpha_certify)]
    certified = certified and not uncertified
    reasons = list(base.reasons)
    if uncertified:
        reasons.append(f"not certified non-inferior per type: {', '.join(uncertified)}")
    if s_rej:
        reasons.insert(
            0,
            f"stratified betting rejects 'no type regressed' (e-value "
            f"{strat.max_e_value:.3g} >= {1 / level:.3g}, alpha share {level:g})",
        )
    blocked = bool(base.regressed_fwer) or s_rej
    outcome: Literal["promote", "hold", "block"]
    if blocked:
        outcome = "block"
    elif certified:
        outcome = "promote"
    else:
        outcome = "hold"
    return VRVerdict(
        outcome=outcome,
        reasons=tuple(reasons),
        base=base,
        stratified_e_value=strat.max_e_value,
        stratified_threshold=1.0 / level,
        stratified_rejected=s_rej,
        contributions=dict(sorted(strat.contributions.items())),
        post_stratified_e_value=None if post is None else post.max_e_value,
        type_certificates={t: e.max_e_value for t, e in type_cert.items()},
        certified=certified,
    )


# --- 6. seeded Monte Carlo (synthetic) ----------------------------------------------------


@dataclass(frozen=True)
class StratumLaw:
    """One case type in a synthetic stream: share and a law for ``X`` given the type."""

    name: str
    share: float
    law: Sampler


def _draw_stream(
    strata: Sequence[StratumLaw], n: int, rng: random.Random
) -> list[tuple[str, float]]:
    names = [s.name for s in strata]
    cum = []
    acc = 0.0
    for s in strata:
        acc += s.share
        cum.append(acc)
    laws = {s.name: s.law for s in strata}
    out = []
    for _ in range(n):
        k = rng.choices(names, cum_weights=cum, k=1)[0]
        out.append((k, laws[k].sample(rng)))
    return out


def block_rates(
    strata: Sequence[StratumLaw],
    n: int,
    reps: int,
    seed: int,
    *,
    alpha: float = 0.05,
    share: float = 0.8,
    activation: float = 1.0,
    checkpoints: Sequence[int] = (),
) -> dict[str, float]:
    """Monte Carlo P(ever block by ``n``) for three block tests on the same paths.

    ``current``: one e-process per type plus a pooled one, weighted e-Bonferroni on
    running maxima at ``alpha`` (the block family of ``evaluate_pairs``).
    ``stratified``: ``StratifiedBettingEProcess`` alone at ``alpha``.
    ``vr``: ``evaluate_pairs_vr``'s rule (``current`` at ``(1 - share) alpha`` or
    ``stratified`` at ``share alpha``). Rates are suprema over time, so they bound the
    error of every stopping rule. ``checkpoints`` adds ``{method}@{t}`` rates.
    """
    if abs(math.fsum(s.share for s in strata) - 1.0) > 1e-9:
        raise ValueError("shares must sum to 1")
    rng = random.Random(seed)
    names = [s.name for s in strata]
    k1 = len(names) + 1
    thr_cur = math.log(k1 / alpha)
    thr_cur_vr = math.log(k1 / ((1.0 - share) * alpha))
    thr_st = math.log(1.0 / alpha)
    thr_st_vr = math.log(1.0 / (share * alpha))
    marks = sorted(set(checkpoints))
    hits: dict[str, int] = {}

    def hit(key: str) -> None:
        hits[key] = hits.get(key, 0) + 1

    for _ in range(reps):
        per = {t: BettingEProcess(0.0, "greater") for t in names}
        pooled = BettingEProcess(0.0, "greater")
        strat = StratifiedBettingEProcess("greater", activation=activation)
        done = {"current": False, "stratified": False, "vr": False}
        mi = 0
        for t, (k, x) in enumerate(_draw_stream(strata, n, rng), start=1):
            per[k].update(x)
            pooled.update(x)
            strat.update(k, x)
            top = max(max(p.max_log_wealth for p in per.values()), pooled.max_log_wealth)
            done["current"] |= top >= thr_cur
            done["stratified"] |= strat.max_log_wealth >= thr_st
            done["vr"] |= top >= thr_cur_vr or strat.max_log_wealth >= thr_st_vr
            while mi < len(marks) and marks[mi] == t:
                for key, v in done.items():
                    if v:
                        hit(f"{key}@{t}")
                mi += 1
        for key, v in done.items():
            if v:
                hit(key)
    keys = ["current", "stratified", "vr"] + [f"{m}@{t}" for t in marks for m in done]
    return {key: hits.get(key, 0) / reps for key in keys}


def certificate_rates(
    strata: Sequence[StratumLaw],
    null_mean: float,
    n: int,
    reps: int,
    seed: int,
    *,
    alpha: float = 0.05,
) -> dict[str, float]:
    """Monte Carlo P(ever certify ``mean < null_mean``): plain against post-stratified,
    and against exact control variates on the stratum indicators."""
    rng = random.Random(seed)
    shares = {s.name: s.share for s in strata}
    names = sorted(shares)
    mean = [shares[t] for t in names]
    thr = math.log(1.0 / alpha)
    hits = {"plain": 0, "post_stratified": 0, "control_variate": 0}
    for _ in range(reps):
        plain = BettingEProcess(null_mean, "less")
        post = PostStratifiedEProcess(shares, null_mean, "less")
        cv = ControlVariateEProcess(
            null_mean, "less", len(names), covariate_mean=mean, theta_max=0.5
        )
        for k, x in _draw_stream(strata, n, rng):
            plain.update(x)
            post.update(k, x)
            cv.update(x, [1.0 if k == t else 0.0 for t in names])
        hits["plain"] += plain.max_log_wealth >= thr
        hits["post_stratified"] += post.max_log_wealth >= thr
        hits["control_variate"] += cv.max_log_wealth >= thr
    return {k: v / reps for k, v in hits.items()}


__all__ = [
    "CRNReport",
    "ControlVariateEProcess",
    "PostStratifiedEProcess",
    "StratifiedBettingEProcess",
    "StratumLaw",
    "VRGateConfig",
    "VRVerdict",
    "block_rates",
    "certificate_rates",
    "crn_report",
    "crn_violations",
    "cv_cases_needed",
    "evaluate_pairs_vr",
    "mixture_radius",
    "stratified_cases_needed",
]
