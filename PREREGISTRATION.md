# Pre-registration: Two-Layer Scheduling for LLM-Driven Danmaku in Voice Livestreams

This document is the dated pre-registration referenced in Section 4.7 of the
paper ("Pre-registration Timeline"). H1–H4 (selection layer) were registered on
**2026-09-25**, before any selection-layer experiment ran. H5–H7 (aggregation
layer) were added on **2026-09-26**. The pilot-informed amendment in §8.2 was
recorded the same day, before any confirmatory aggregation run. The git history
of the development repository timestamps both events; this file is the verbatim
hypothesis text, archived publicly with the code release.

- Study: when does scheduling matter for a single-speech-channel AI VTuber?
- Design: closed-loop replay harness over synthetic danmaku streams generated
  from logged production messages; frozen scorer/judge caches; 5 seeds per
  condition (confirmatory runs post-amendment, freshly seeded).
- Service model: single speech channel, placeholder-calibrated slot durations.
- Metrics: precise-reply rate, mean replied judge score, slot-commitment
  (occupancy) rate, nominal coverage, verified mention rate (VMR), high-value
  waiting time, LLM-call counts.

## 1. Selection layer hypotheses (registered 2026-09-25, before any selection run)

**H1.** Under overload (λ ≥ 1.8μ) with topic drift, a fixed promotion threshold
either starves during the flooding phase (its precise-reply rate there is
significantly lower than in the other phases) or floods (its mean replied score
collapses); the adaptive scheduler avoids both failure modes.

**H2.** greedy has no threshold and keeps answering through the flooding
phase — its precise-reply rate does not drop, but its mean replied score is
significantly below that of the full adaptive scheduler.

**H3.** If H1/H2 still fail at λ ≈ 3.6μ with drift (full ≈ greedy ≈ fixed), we
accept the strong negative result that scheduling adds nothing; if the gap
appears only under overload with drift, scheduling acts as overload insurance.

**H4.** The slot-commitment (occupancy) rate decreases monotonically with λ and
approaches zero at λ ≈ 3.6μ, i.e., commitment is a degenerate low-load
behavior.

## 2. Aggregation layer hypotheses (registered 2026-09-26; H5/H6 are the pilot-amended versions)

**H5.** Under overload (λ ≥ 1.8μ), the merging scheduler's total coverage
significantly exceeds greedy's, meeting the *amplitude criterion*: a paired
per-seed coverage gain (merging minus greedy) of at least 8 percentage points
with all five seeds positive (an admissible band of +6 to +14 points; any
reversed seed must be reported and the claim downgraded). Absolute coverage
levels are reported descriptively only. H5 and H7 count as supported only if
all five seeds at λ ≈ 3.6μ with drift agree in sign and the λ ≈ 1.8μ drift
condition agrees as well.

**H6.** The merging scheduler degrades neither the high-value precise-reply
rate nor the high-value waiting time relative to greedy (non-degradation
tolerances: rate difference ≥ −2 points; waiting difference ≤ +10 s).

**H7.** The merging scheduler's low-tier coverage is significantly above
greedy's, i.e., the coverage gain comes mainly from the low tier rather than
from further tilting toward high-value messages.

## 8. Amendments

### 8.1 Addition of aggregation-layer hypotheses (2026-09-26)

H5–H7 were added after the selection-layer experiments completed, before any
aggregation-layer confirmatory run.

### 8.2 Pilot-informed amendment of H5/H6 (2026-09-26, same day)

A pilot smoke run showed nominal coverage of 46.0% vs. 36.1% (main arm vs.
baseline), far below the range initially imagined for the amplitude criterion.
H5's amplitude criterion and H6's non-degradation tolerances were therefore
adjusted to the values stated above. The amendment was pilot-informed; all
confirmatory numbers reported in the paper come from post-amendment seeded
runs, not from the pilot. This amendment is disclosed in the paper
(Section 4.7 and Threats to Validity).
