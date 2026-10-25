# Executing the close-entry restructure (task 3.8, after the failed gate)

**Status 2026-10-25.** This page turns the recommendation in [strategy-review-2026-10-25.md](strategy-review-2026-10-25.md) (B′ + A′: confirmation at the 15:40 decision price, two pre-registered candidates) into an ordered set of tasks. It is for the user and for the agents of the next sessions. **Step 0 is done:** the user answered every question as the review recommended (§2), recorded as #144 and #145. **Step 1 is drafted** ([close-entry-preregistration.md](backtests/close-entry-preregistration.md)) and awaits the user's approval. Nothing is built, nothing has run on real data and the holdout (2023 on) stays closed.

The order follows one rule. **Every choice that can move the result is fixed and committed before the code that measures it touches real data.** Then the tuning period gets exactly one scored run, and the holdout is opened at most once.

## 1. The path at a glance

| Step | What | Who | Size | Gate to the next step |
|---|---|---|---|---|
| 0 | Answer the review's §9 questions, choose the fallback now | 👤 user | minutes | All seven answered |
| 1 | Decision #144 and the pre-registration, committed | agent + 👤 approval | S (1 session) | The user approves the pre-registration; its commit hash is recorded |
| 2 | Build the pure layer (config, entry policy, stop, guardrail width) | agent | S (1 session) | 100% branch coverage on the changed guardrail and entry code; ruff and mypy clean |
| 3 | Build the simulator entry and the pool for both candidates | agent | S–M (1–2 sessions) | Known-answer tests pass; the blind mechanics check passes (§5) |
| 4 | Build the report (gate, per-year rule, 21-session blocks, diagnostics) | agent | S (½–1 session) | The report renders on synthetic results |
| 5 | **The one scored run** on 2017–2022, then reading it by the pre-registered rules | agent, then 👤 | ½ session | The user reads the report |
| 6a | On a pass: freeze, open the holdout once, finish 3.8 | agent + 👤 | S (1 session) | Holdout point estimate positive |
| 6b | On a fail: stop tuning; start the chosen fallback | 👤 | n/a | n/a |
| P | **In parallel (if approved):** Phase 4 in `dry_run` | agent | as planned | 4.5 and 4.6 need the user's OK (paid) |
| P′ | **Running anyway:** the 1.8 acceptance on `paper`, the slippage measurement | agent + 👤 | n/a | n/a |

That is about 4–6 sessions to a verdict on the tuning period, plus one for the holdout if the run passes. Steps 1–5 make no paid calls and create no AWS resources: they use the Parquet history and daily bars only. The optional minute-bar sample in §5 uses the free Alpaca tier.

## 2. Step 0: the user's answers (done 2026-10-25)

The review's §9 holds the questions. Some of them change the shape of this plan, so they came first. **The user chose the review's answer to all seven, and F as the fallback.** For question 6, that means kept out until the scorecard has spoken (not the half-risk starter):

| # | Question | Review's answer | What it changes here |
|---|---|---|---|
| 1 | Which option | B′ + A′, drop C | The whole plan; with D instead, this page is void |
| 2 | 15:40 marketable limit or limit-on-close | Marketable limit OTO with the stop leg | The fill rule in the pre-registration; LOC also breaks CLAUDE.md §3.5 for the after-hours window |
| 3 | Small caps at 15:40 | Keep them; for entries only, fall back to the delayed SIP last trade | A live design point for #144 and Phase 5; the backtest reports the ≥ $100M tier separately either way |
| 4 | How many candidates | Two, plus the two comparisons in the review's §8.4 | The pre-registration's candidate list; anything more is a third trial |
| 5 | Phase 4 in `dry_run` now, gate moved to "before capital" | Yes | Whether track P starts now; a README §15 change (decision #145) |
| 6 | EP "positive but not passing" | Starter strategy at half risk, or keep out | Fixed in the pre-registration's §8.5 equivalent; **must be decided now, not after the run** |
| 7 | The ranking's tightness component | Leave it (#138); the Analyst sees the width as a fact | Nothing, unless the user wants a third variant |

**One more answer the review left open: the fallback if nothing passes.** Its §8.5 said "the user chooses D or F or pause" after the run. Choosing *after* seeing the result invites a fourth option ("just one more variant"), so it was chosen now: **F**.

## 3. Step 1: decision #144 and the pre-registration (S)

Branch `phase-3/close-entry`. One PR, docs only.

1. **`docs/backtests/close-entry-preregistration.md`**, written from the review's §8 with the user's answers. Every threshold carries its source (the README, a decision number or a named author), as in [baseline-preregistration.md](backtests/baseline-preregistration.md). The review leaves five things open, and they must be fixed in it:
   - **The 21-session blocks.** The secondary interval resamples consecutive 21-session blocks of the trading calendar, non-overlapping, starting at the first tuning session. A trailing block shorter than 21 sessions is kept. The same draws (2,000) and seed (38) as the primary.
   - **The per-year rule.** Positive in at least 4 of the 6 tuning years, and the expectancy with the best year removed is at least zero. "Best year" means the year with the highest per-year expectancy for that strategy.
   - **The decision price in the backtest is the close.** For entries this is a small look-ahead: the gate reads the range position and the price after the last 20 minutes. Pre-register the check in §5 (step 3d) and the threshold that would invalidate the run. My proposal: if the 15:40 and close gate decisions disagree on more than 15% of a sampled 200 entry sessions, the run is reported but not used to adopt anything until the gap is understood.
   - **The fill.** The decision price (the close) plus the liquidity-tier slippage, with no fill if that exceeds the limit (decision price × 1.005). Identical to the tranche-2 rule in [sim/trade.py:454](../src/auratrade/sim/trade.py#L454).
   - **The fallback and the EP path** from Step 0, quoted word for word.
2. **README** (the spec, which code must match):
   - §7.1 gains the `CLOSE` entry policy and the trigger-day-low stop, **marked as a 3.8 candidate (#144)**. `risk.yaml`'s `entry:` defaults do not change until a pass (Step 6a), so the spec isn't rewritten twice on a failure.
   - §15 gets the per-year rule and the secondary statistic as additions to the gate.
   - If Question 5 is yes: §15 moves the gate from "before Phase 4" to "before capital" (decision #145).
   - §16 records #144 (and #145).
3. **The tracker** in [execution-plan.md](execution-plan.md) gets the sub-tasks in §8 of this page.
4. **The commit hash of the approved pre-registration goes into the report header** in Step 4. That hash is the proof the rules came first.

**Done when** the user has approved the pre-registration text and it is merged (or committed on the branch, if the user prefers to keep the experiment on one branch).

## 4. Step 2: the pure layer (S)

Pure functions, unit-tested on hand-built data with known answers (CLAUDE.md §6):

| Change | Where | Notes |
|---|---|---|
| `TriggerKind.CLOSE`, `StopKind.TRIGGER_DAY_LOW` | [config.py:782](../src/auratrade/config.py#L782) | Typed; the config hash changes |
| Candidate entry policies as a named alternative, not the default | `config/risk.yaml` `plans:` | e.g. an `entry_candidates:` block the backtest selects; `entry:` stays as it is |
| Momentum gets the `BREAKOUT` gate | `risk.yaml` | It has no gate today (the review's §8.1) |
| The close trigger: the gate on the decision price, the max chase from the level | `plans/entry.py`, `execution/triggers.py` | Reuses `confirmation_gate` ([plans/entry.py:84](../src/auratrade/plans/entry.py#L84)) |
| The EP gate: price ≥ the gap open, range position ≥ 0.5, volume ≥ 3 × the 50-day average | `strategies/episodic_pivot.py` (a pure helper) | Replaces neither the 09:36 proxy nor its tests; the old path stays for the reference run |
| The stop: max(structural, trigger day's low), the width **measured from the limit** | `guardrails/levels.py` | 100% branch coverage (enforced in CI); a too-wide stop is rejected, never tightened |
| Sizing on the limit, whole shares floored | `execution/sizing.py` | Already the rule; add the test cases |

**Done when** the tests pass, the guardrail coverage stays at 100%, and ruff and mypy are clean. No real data has been touched.

## 5. Step 3: the simulator and the pool (S–M)

1. **3a. Simulator.** A `CLOSE` entry in [sim/trade.py](../src/auratrade/sim/trade.py) goes through the tranche-2 code path, so there is still one fill model (CLAUDE.md §6). It is a single tranche with its own GTC stop from the fill. The plan's session counter starts at the entry session, as now.
2. **3b. Pool.** [backtest/pool.py](../backtest/pool.py):
   - `reachable` reads the gate on the trigger session's close instead of "the high reaches the pivot" ([pool.py:313](../backtest/pool.py#L313));
   - EPs are listed from the open (gap ≥ 10%), with features from the prior close (`known_at`, unchanged since #143), and gated at the close;
   - a unit is retried on each listed session until it fills, as now;
   - both candidates and the two comparisons come from **one invocation** of `backtest.baseline pool`.
3. **3c. Known-answer tests.** About 6 setups with hand-computed entry, stop, fill and first exit. Use hand-built series, not real symbols, so the tests don't preview the tuning period.
4. **3d. The blind mechanics check.** One run of the full pipeline over 2017–2022 that **writes no R, P&L or expectancy**, only counts and invariants:
   - gate pass rates per condition;
   - stop-width rejections and the distribution of stop widths;
   - fills vs misses at the limit;
   - whole-share skips;
   - every position has a stop below its fill, and no trade has an entry after its exit.

   These are pre-registered diagnostics, and the pre-registration forbids changing thresholds on them. They let the code be debugged without peeking at the outcome. The same step takes the **15:40-vs-close sample**: about 200 entry sessions drawn at random with a fixed seed, using minute bars already cached from 3.8 where they exist and the free tier otherwise. It reports how often the gate decides differently at 15:40 than at the close.

   *Discipline:* the agent running 3d must not print or log outcome columns. Build the blind mode as a flag on the report that drops them, not as a promise.

**Done when** the known-answer tests pass and the blind check shows no broken invariants. If 3d reveals a bug, fix it and re-run 3d; that is allowed because it is blind. If 3d reveals that a pre-registered rule can't work as written (say, near-zero gate passes for a strategy), **stop and bring it to the user** before Step 5. Don't adjust the rule silently: that is a third candidate.

## 6. Step 4: the report (S)

`docs/backtests/close-entry.md`, generated, with:
- the pre-registration's commit hash and the `risk.yaml` hash at the top;
- the gate per candidate and per strategy (primary interval), the 21-session secondary beside it, and the per-year rule's verdict;
- the minimum-sample rule (300 closed trades);
- every diagnostic in the review's §8.3, including the missed-entry expectancy that 3.8 pre-registered and never reported;
- the ≥ $100M dollar-volume tier apart;
- the two comparisons;
- the 15:40-vs-close disagreement rate from 3d.

Add a `block` parameter to [`block_bootstrap`](../src/auratrade/journal/scorecard.py#L171), defaulting to 1 so the existing reports don't change, with a test. Render the report on synthetic trade lists first.

## 7. Steps 5 and 6: the run and what follows

**Step 5. The one scored run.** One invocation on 2017–2022 under the regime `cu+2_corr-2_c2_dist8`. Commit the report as produced, before anyone reads it for meaning. Then apply the pre-registered reading, with no new splits:

| Outcome | Action |
|---|---|
| A strategy passes (interval, per-year rule, ≥ 300 trades) | Step 6a for the strategies that pass |
| Positive point estimate, fails the interval (the likely EP case) | The path the user fixed in Step 0, Question 6 |
| Nothing passes | Step 6b. **No further runs on 2017–2022**, whatever the report suggests |
| The 15:40-vs-close check exceeds its threshold | Reported; nothing adopted until the user decides |

**Step 6a. On a pass** (1 session, with the user):
1. Freeze the passing entry policies, the fundable lists and the 5-pillar regime in `risk.yaml`, and record the hash.
2. Open the holdout (2023 on) **once, for everything at once**: the gate's holdout estimate, the regime's holdout checks (≤ ~12 switches a year, the 2023 recovery), and the sign of each adopted comparison.
3. Make the README's §7.1 candidate the default. Update `entry:` and tick 3.8.
4. Phase 5 then builds the live 15:40 entry: the OTO marketable limit placed by the 15:40 job, and the SIP-reference fallback if Question 3 was yes. Most of it is the existing tranche-2 order path. The opening range, the 09:51 run and two-tranche entries become dead code there, to be removed in Phase 5, not now.

**Step 6b. On a fail:** write the finding plainly to the report and to `strategy-decision.md`. Start the fallback chosen in Step 0. For F, that is Phase 4 as planned, plus a pre-registered BUY-vs-SKIP bar on the scorecard (the review's §4.2) written before 4.6 deploys.

## 8. Tracker entries to add on approval

Under Phase 3 in [execution-plan.md](execution-plan.md), after 3.8:

```
- [ ] 3.8a 👤 Strategy decision: answers to the review's §9 + the fallback; decisions #144 (#145)
- [ ] 3.8b Pre-register the close entry (`docs/backtests/close-entry-preregistration.md`), approved and committed before any code reads real data
- [ ] 3.8c Build the close entry: pure layer, simulator, pool, report; known-answer tests; the blind mechanics check
- [ ] 3.8d The one scored run on 2017-2022 and its pre-registered reading
- [ ] 3.8e (on a pass only) Freeze, open the holdout once, finish 3.8
```

3.8 itself stays unticked until 3.8d or 3.8e settles it.

## 9. The parallel tracks

**P: Phase 4 in `dry_run`** (only if Question 5 is yes):
- **Separate branches** (`phase-4/...`) in their own worktree, as in the 2026-10-09 parallel build. Phase 4 touches `llm/`, prompts and jobs. The close entry touches `config.py`, `risk.yaml`, `plans/`, `guardrails/`, `sim/` and `backtest/`. The overlap is `config.py` and `risk.yaml`: merge the close-entry PRs first when both are open.
- Verdicts are policy-independent (BUY/WATCH/SKIP on the evening scan), so nothing in Phase 4 waits on the run. The scorecard re-scores the verdicts under whatever entry policy wins.
- 4.1–4.4 cost nothing beyond fixtures. **4.5 (the paid sanity set) and 4.6 (the deploy, about $20–40 a month) each need the user's OK with a cost estimate**, as CLAUDE.md §3.3 requires.
- Load the `claude-api` skill before any LLM code.

**P′: unaffected and running:**
- the 1.8 acceptance on `paper`: 10 unattended sessions, and the stop hand check at the first fill;
- `paper` needs `alembic upgrade head` (0007) before its next deploy;
- comparing the paper fills with `sim.slippage` is a measurement only. It may be reported beside the run but never changes the run's slippage.

## 10. Risks to the execution, and the guard for each

| Risk | Guard |
|---|---|
| Debugging on real outcomes becomes tuning | Steps 2–3 test on hand-built data; 3d is blind by a code flag; the scored run happens once |
| "Just one more variant" after a near miss | Fallback and the EP path fixed in Step 0; the pre-registration lists what is not allowed (the review's §8.6) |
| The close as the decision price flatters the gate | The 15:40-vs-close sample with a threshold fixed in advance |
| Stop-width rejections rise sharply for VCPs (structural stop far from the close) | Reported in 3d; if a strategy is nearly empty, back to the user before Step 5, not a silent cap change |
| Parallel branches collide on `config.py` / `risk.yaml` | Close-entry PRs merge first; Phase 4 rebases |
| The live universe is narrower than the backtest's (IEX spread cap) | The ≥ $100M tier reported apart; Question 3's fallback designed in #144, built in Phase 5 |
| A pass driven by 2020 alone | The per-year rule, which is part of the gate |
| The spec drifts from the code | README §7.1 marks the candidate in Step 1 and promotes it only in 6a |

## 11. What I'd expect, honestly

The review puts the odds of any pass on the tuning period at about one in three. EP at the close is the likeliest, and for it a pass depends as much on sample size as on edge. Plan the time on the assumption that the failure branch is the more likely one. That is why the fallback is chosen in Step 0, and why track P (if approved) is worth starting now: under F, the Phase 4 work is the next thing to build either way.
