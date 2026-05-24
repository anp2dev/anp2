"""ANP2 trust aggregation algorithm — reference implementation (trust.v1).

Cited from spec/PROTOCOL.md §6. This module is the *normative* answer to the
placeholder formula in the spec. It implements:

  1. Iterative trust-weighted scoring
     Each voter's contribution is weighted by that voter's own current trust
     score, computed as a bounded fixed-point iteration over the vote graph.
     This is a damped PageRank-style scheme rather than the depth-recursive
     trust() in PIP-001's reference pseudo-code — the recursive form is
     O(branching^depth) and brittle; the fixed-point form is O(V * iters)
     with V = vote count and converges deterministically.

  2. Exponential time decay on each vote
     contribution(vote, t_now) = vote.score * exp(-ln(2) * age_days / HALF_LIFE_DAYS)
     HALF_LIFE_DAYS defaults to 30 (per task brief; PIP-001 proposed 180 but
     30 days is more responsive for Phase 0-1 where the network is small and
     opinions change quickly).

  3. Anti-sybil dampening via vote-target diversity
     A voter who has cast votes to fewer than MIN_DISTINCT_TARGETS (default 3)
     distinct counterparties has their *outgoing* weight scaled by
       distinct_targets(v) / MIN_DISTINCT_TARGETS
     i.e., a brand-new voter with one outgoing vote contributes 1/3 of what
     they would at steady state. Cheap, easy to compute, and meaningfully
     disrupts the most common sybil pattern (N fresh accounts each casting
     one vote for the same target). Stronger graph-structural defenses
     (e.g. neighborhood-trust as proposed by adversarial-AI reviewer in
     PIP-001) are left for a future PIP.

  4. Bounded iterations with convergence detection
     Up to MAX_ITERATIONS (20) Jacobi-style updates. We stop early when
     max |trust_new[a] - trust_old[a]| < EPSILON (1e-4). For Phase 0-1
     (<10k votes) this is comfortably sub-second.

Aggregation rule (per PIP-001 §1): every unrevoked kind 6 vote contributes,
not latest-wins. A voter who flips +1 -> 0 -> +1 contributes three time-
decayed magnitudes, which is the correct longitudinal signal.

Pure stdlib. No numpy. Suitable for embedding in a single-process SQLite
relay.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from typing import Iterable

from .pow import SYBIL_NORM_CONSTANT

# --- tunables (versioned as trust.v1 — change requires a new PIP) ---
HALF_LIFE_DAYS = 30.0
DAY_SECONDS = 86400.0
MIN_DISTINCT_TARGETS = 3
MAX_ITERATIONS = 20
EPSILON = 1e-4
# Base weight every voter has even with zero incoming trust. Without this the
# algorithm has no signal at t=0 (the bootstrap problem). 1.0 means an
# unknown voter counts as much as a top-trust voter at iteration 0; iteration
# converges toward the trust-weighted form.
BOOTSTRAP_WEIGHT = 1.0


@dataclass
class Vote:
    """A single kind 6 trust_vote event, parsed."""

    voter: str
    target: str
    # PROTOCOL §4.7.1 — score is a float in [-1.0, +1.0]; integers -1/0/+1
    # remain legacy-compatible (mypy-friendly float coercion in parse_votes).
    score: float
    created_at: int  # unix seconds
    # PIP-002: declared PoW bits on this vote (None for pre-PIP-002 votes
    # that carry no `pow` tag). Feeds `sybil_factor_pow(target)`.
    pow_bits: int | None = None


@dataclass
class TrustResult:
    """Output of compute_trust()."""

    # weighted_score[agent] = sum over voters of voter_weight * sum(decayed vote contributions)
    # multiplied by the PIP-002 incoming-PoW sybil_factor when at least one
    # vote on `agent` carried a `pow` tag. See `sybil_factor_pow()`.
    weighted_score: dict[str, float] = field(default_factory=dict)
    # raw_score[agent] = same sum but with voter_weight forced to 1.0 (sybil ignored, no propagation)
    raw_score: dict[str, float] = field(default_factory=dict)
    # voter_count[agent] = number of distinct voters with any non-revoked vote on agent
    voter_count: dict[str, int] = field(default_factory=dict)
    # per-agent vote breakdown for /trust/<agent_id>
    votes_for: dict[str, list[dict]] = field(default_factory=dict)
    # PIP-002 §3 — tanh(— 2^pow_bits / NORM) for each agent that received at
    # least one PoW-tagged incoming kind 6 vote. Multiplicative on
    # weighted_score. Agents with only un-PoW'd incoming votes get 1.0 (no
    # change), preserving back-compat with pre-PIP-002 voters.
    sybil_factor_pow: dict[str, float] = field(default_factory=dict)
    iterations: int = 0
    converged: bool = False


def parse_votes(rows: Iterable[dict]) -> list[Vote]:
    """Convert raw DB rows (voter, target, content, created_at, [tags_json])
    to Vote objects.

    Rows with malformed content or out-of-range scores are coerced to 0
    (treated as a neutral / revoked vote) rather than dropped, so the
    aggregation rule (`sum over all unrevoked kind 6 events`) stays honest.

    When the row includes a `tags_json` field (storage feeds this by default
    starting from PIP-002), any `["pow","<n>"]` tag is parsed and stored as
    `Vote.pow_bits` so `sybil_factor_pow` can aggregate cumulative PoW work
    per target. Rows without `tags_json` get `pow_bits = None`.
    """
    out: list[Vote] = []
    for r in rows:
        try:
            parsed = json.loads(r["content"])
            raw = parsed.get("score", 0)
            # PROTOCOL §4.7.1 — continuous float scores in [-1.0, +1.0] are
            # accepted; integers stay legacy-compatible. NaN / Inf / string
            # / |s|>1 collapse to 0 (= withdrawal, per §4.7.2).
            if isinstance(raw, bool):
                s = 0.0
            elif isinstance(raw, (int, float)):
                fv = float(raw)
                if not (-1.0 <= fv <= 1.0) or fv != fv or fv in (float("inf"), float("-inf")):
                    s = 0.0
                else:
                    s = fv
            else:
                s = 0.0
        except Exception:
            s = 0.0
        pow_bits: int | None = None
        tags_blob = r.get("tags_json") if isinstance(r, dict) else None
        if tags_blob:
            try:
                tags = json.loads(tags_blob)
                for t in tags or []:
                    if t and len(t) >= 2 and t[0] == "pow":
                        try:
                            v = int(t[1])
                            if 0 <= v <= 256:
                                pow_bits = v
                        except (ValueError, TypeError):
                            pow_bits = None
                        break
            except (json.JSONDecodeError, TypeError):
                pow_bits = None
        out.append(
            Vote(
                voter=r["voter"],
                target=r["target"],
                score=s,
                created_at=int(r["created_at"]),
                pow_bits=pow_bits,
            )
        )
    return out


def sybil_factor_pow(target: str, votes: list[Vote]) -> float:
    """PIP-002 §3 — incoming-PoW dampening for `target`.

    Returns `tanh(— 2^pow_bits / SYBIL_NORM_CONSTANT)` summed over `target`'s
    incoming votes that carry a `pow` tag. Per the "do not break existing
    agents" rule of this phase, when `target` has zero PoW-tagged incoming
    votes the factor defaults to 1.0 (no change to base weighted_score). Once
    PoW votes start arriving the factor concentrates toward 1.0 as cumulative
    work grows; this preserves the spec's asymmetric-cost property (attacker
    burns CPU proportional to desired weight) without zeroing out pre-PIP-002
    voters.
    """
    work_sum = 0
    saw_pow = False
    for v in votes:
        if v.target != target or v.pow_bits is None:
            continue
        saw_pow = True
        # Cap exponent at 256 (same as parse_votes guard) so a corrupt-but-
        # parsed declaration cannot overflow Python ints into pathological
        # tanh argument territory.
        bits = min(256, max(0, v.pow_bits))
        work_sum += 1 << bits
    if not saw_pow:
        return 1.0
    return math.tanh(work_sum / float(SYBIL_NORM_CONSTANT))


def vote_contribution(vote: Vote, t_now: int) -> float:
    """Time-decayed magnitude of a single vote."""
    age_days = max(0.0, (t_now - vote.created_at) / DAY_SECONDS)
    return vote.score * math.exp(-math.log(2) * age_days / HALF_LIFE_DAYS)


def sybil_factor(voter: str, votes: list[Vote]) -> float:
    """Dampening factor in [0, 1] for `voter`'s outgoing weight.

    Returns count_of_distinct_targets / MIN_DISTINCT_TARGETS, clamped to 1.0.
    A voter with 0 outgoing votes returns 0 (cannot influence anyone — but
    they also have no contribution rows, so this is academic).
    """
    distinct = {v.target for v in votes if v.voter == voter}
    if not distinct:
        return 0.0
    return min(1.0, len(distinct) / float(MIN_DISTINCT_TARGETS))


def compute_trust(votes: list[Vote], t_now: int) -> TrustResult:
    """Fixed-point trust scoring.

    Algorithm (formula):

        # per-voter, per-target time-decayed contribution
        c(v, T)        = —_{vote — votes(v—T)} vote.score
                            * exp(-ln 2 — age_days / HALF_LIFE_DAYS)

        # outgoing-weight dampening for thin-graph voters
        sybil(v)       = min(1, distinct_targets(v) / MIN_DISTINCT_TARGETS)

        # voter weight derived from current trust estimate
        w(v)           = sqrt(max(0, trust_prev[v] + BOOTSTRAP_WEIGHT)) * sybil(v)

        # target's new trust
        trust_new[T]   = —_v w(v) — c(v, T)

    Iterate up to MAX_ITERATIONS or until max |—trust| < EPSILON.
    """
    if not votes:
        return TrustResult(iterations=0, converged=True)

    # index votes by (voter, target) for efficient contribution aggregation
    pair_contrib: dict[tuple[str, str], float] = {}
    voters_by_target: dict[str, set[str]] = {}
    all_agents: set[str] = set()
    sybil_cache: dict[str, float] = {}

    for v in votes:
        all_agents.add(v.voter)
        all_agents.add(v.target)
        pair_contrib[(v.voter, v.target)] = (
            pair_contrib.get((v.voter, v.target), 0.0) + vote_contribution(v, t_now)
        )
        voters_by_target.setdefault(v.target, set()).add(v.voter)

    for voter in {v.voter for v in votes}:
        sybil_cache[voter] = sybil_factor(voter, votes)

    # initialize trust to bootstrap weight everywhere
    trust_prev: dict[str, float] = {a: 0.0 for a in all_agents}
    trust_new: dict[str, float] = dict(trust_prev)

    converged = False
    iters = 0
    for iters in range(1, MAX_ITERATIONS + 1):
        for target, voters in voters_by_target.items():
            total = 0.0
            for voter in voters:
                w = math.sqrt(max(0.0, trust_prev[voter] + BOOTSTRAP_WEIGHT)) * sybil_cache.get(voter, 0.0)
                total += w * pair_contrib[(voter, target)]
            trust_new[target] = total
        # convergence check
        delta = max(
            (abs(trust_new[a] - trust_prev[a]) for a in all_agents), default=0.0
        )
        trust_prev, trust_new = dict(trust_new), trust_prev
        if delta < EPSILON:
            converged = True
            break

    # raw_score: ignore voter weight + sybil (pure decayed-sum baseline)
    raw_score: dict[str, float] = {a: 0.0 for a in all_agents}
    for (voter, target), c in pair_contrib.items():
        raw_score[target] += c

    # per-agent vote list (latest-instance per (voter, target) for display)
    votes_for: dict[str, list[dict]] = {}
    latest_per_pair: dict[tuple[str, str], Vote] = {}
    for v in votes:
        key = (v.voter, v.target)
        cur = latest_per_pair.get(key)
        if cur is None or v.created_at > cur.created_at:
            latest_per_pair[key] = v
    for (voter, target), v in latest_per_pair.items():
        votes_for.setdefault(target, []).append(
            {"voter": voter, "score": v.score, "created_at": v.created_at}
        )

    voter_count = {t: len(vs) for t, vs in voters_by_target.items()}

    # PIP-002 §3 — apply incoming-PoW sybil_factor as a multiplicative gate
    # on each target's final weighted_score. Defaults to 1.0 for agents with
    # zero PoW-tagged incoming votes so existing pre-PIP-002 voters and
    # vouch chains remain unchanged. raw_score is intentionally left alone:
    # it stays the pure decayed-sum baseline for diagnostics.
    sybil_pow: dict[str, float] = {}
    weighted_score: dict[str, float] = dict(trust_prev)
    for target in weighted_score:
        f = sybil_factor_pow(target, votes)
        sybil_pow[target] = f
        weighted_score[target] = weighted_score[target] * f

    return TrustResult(
        weighted_score=weighted_score,
        raw_score=raw_score,
        voter_count=voter_count,
        votes_for=votes_for,
        sybil_factor_pow=sybil_pow,
        iterations=iters,
        converged=converged,
    )
