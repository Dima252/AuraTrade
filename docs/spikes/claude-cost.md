# Spike 0.3g: Claude cost, batch turnaround and Portfolio Manager timing

**Run:** 2026-10-09, `uv run --group spikes python -m spikes.claude_cost <build|batch|pm>`, model `claude-opus-5-5`, Python SDK 1.12.1. **Paid, approved by the user:** total spend about **$0.26** (batch $0.155, Portfolio Manager call $0.104; token counting is free). Raw output in `spikes/claude_cost/output/` (git-ignored).
**Decision: ADJUST** (proposed, for approval at 0.4): README §9's cost estimate is about **2–3× too high**. Measured, the Analyst costs about **$0.016 a request** in the batch, so about **$13 a month** at a full 40 requests a session, and **about $17–22 a month all in**. The Phase 4 daily cost limit should be set from these numbers (below). Batch turnaround was **2 h 20 min**, which the 19:00 / 21:00 / 06:00 collect schedule already covers.

## What was sent

Five real liquid stocks passing a simple trend filter (NVDA, AAPL, AMD, LITE, DELL), each as one Analyst request:
- a daily chart (1 year, 800×600) and a weekly chart (2 years, 600×400), drawn with `mplfinance`: candles, moving averages, volume, the pivot line, an RS-line panel
- compact JSON facts: moving averages, extension, 52-week range, returns, RS score, RS-line state, ADR, ATR, up/down volume, the code pivot (15-session high) and structural stop (10-session low), base count, the last 4 SEC quarters (revenue and EPS growth), the next earnings date, the last 4 surprises
- up to 5 headlines in an `<untrusted_headlines>` block
- a ~1,800-token system prompt (strategies, regime labels, price-level rules, output rules), marked `cache_control: ephemeral`
- structured output (`output_config.format`, JSON schema matching README §9's Analyst schema), adaptive thinking

The same five went in one batch twice, at effort `medium` and at `high`: 10 requests. **The facts are spike-grade** (simple levels, a stub regime): they give realistic sizes, not real verdicts.

## Analyst batch

| | `medium` | `high` |
|---|---|---|
| Input tokens per request (incl. the 2 images) | about 2,240 uncached + 1,786 system prompt = **~4,000** (count_tokens: 3,400–3,460 before the schema) | same |
| Output tokens (incl. thinking), average | **657** (394–886) | **838** (737–997) |
| Of which thinking | 0–489 | 360–620 |
| **Cost per request (batch prices)** | **$0.0155** | **$0.0156** |
| Projected month: 40 requests × 21 sessions, no verdict cache | **$13.0** | **$13.1** |
| Decisions | DELL BUY (conviction 4), 4 WATCH | 5 WATCH |

README §9 assumed about 8,000 input tokens and 2,000–4,000 output tokens a request ($1.45–2.25 a session, $30–47 a month). **The measured request is half that size, and thinking is far shorter than assumed.** Effort barely changes the cost: the input dominates, and `high` adds about 180 output tokens. So effort can be chosen on quality alone in task 4.5.

**Prompt caching inside the batch:** 8 of 10 requests wrote the cache and only 2 read it. That's the "best-effort for concurrent requests" behaviour the README expected. With a 1,786-token prefix, a cache write costs about $0.0009 more than no caching and a hit saves about $0.0034, so the break-even hit rate is about 21%. Observed: 20%. **Caching in the batch is roughly cost-neutral as configured.** To test in Phase 4: warm the cache with one real-time request just before submitting (1-hour TTL), so the batch's requests read it.

**Rule-following, all 10 answers:**
- `strategy` equal to the request: 10 of 10.
- Pivot returned unchanged: 10 of 10 (no adjustments were proposed).
- Stop equal to or tighter than the code's structural stop: 10 of 10. LITE's code stop sat 22% below the pivot. `medium` returned it unchanged and wrote that "a VCP entry cannot be funded within risk rules"; `high` tightened it to 1046.50. No answer loosened a stop, so the "Price levels" guardrail would have rejected nothing.
- Soft length caps were stretched: `reasoning` exceeded ~80 words in 7 of 10 (76–97 words); `watch_note` exceeded 20 words once (22). These are soft caps; code can truncate or accept them. Not a cost issue.
- `medium` and `high` disagreed on one stock (DELL: BUY 4 vs WATCH 3). Five pairs can't show which is better; that's task 4.5's job.
- `usage.output_tokens_details.thinking_tokens` is populated, so thinking spend can be logged per request (README §9 says thinking dominates; here it is 0–620 tokens, less than half the output).

**Turnaround:** submitted 10:35 ET, ended 12:55 ET: **2 h 20 min** for 10 requests during US market hours. With `analyst_submit` at 17:50, a similar batch would be ready around 20:10 and picked up by the 21:00 `analyst_collect`. The 06:00 fail-closed deadline (README §3) leaves a 12-hour margin. One sample, so the collect schedule stays as is.

## Portfolio Manager (real-time, effort `high`, tools)

One call shaped like the 08:45 decision step: the account ($5,000, multiplier 0.5, PDT allowance), the regime, the five setups with stub BUY verdicts, and four strict tools (`get_news`, `get_chart`, `get_fundamentals`, `get_position_history`) with `tool_choice: auto`. Server-side fallbacks were enabled (`fallbacks: "default"`, beta `server-side-fallback-2026-07-01`), and no fallback was needed.

| Turn | Seconds | Input (cache) | Output (thinking) | Tools called | Cost |
|---|---|---|---|---|---|
| 1 | 17.5 | 3,355 + 1,668 written | 960 (733) | get_news ×2, get_fundamentals, get_chart | $0.041 |
| 2 | 20.4 | 6,648 + 1,668 read | 1,815 (792) | none, final plan | $0.063 |
| **Total** | **39 s** | | | | **$0.104** |

- **The 09:10 deadline is comfortable:** 39 seconds of a 25-minute window. Even five tool rounds would take a few minutes.
- The cache works across turns of one conversation (turn 2 read the 1,668-token prefix).
- The final plan validated against the schema. It kept the computed regime, cited headline risk, and funded DELL and AAPL at 0.5% risk each (the multiplier applied), with Minervini partials and a 21-EMA trail. It rejected LITE (stop 22.5% wide), NVDA (weak RS, earnings inside the likely holding period) and AMD (extended, and a second AI/semiconductor bet), reasons a trader would give.
- With no open positions, it wrote those "why not" notes into `position_actions`. **The real schema (task 4.2) needs a separate `notes` field** so position actions stay machine-readable.
- README §9 assumed $0.30–0.60 on days the Portfolio Manager runs; measured $0.10 with 4 tool calls. Allow up to ~$0.30 for busy mornings (more setups, EP reviews, more tool rounds).

## Monthly estimate, measured vs README

| Role | README §9 | Measured basis | Estimate |
|---|---|---|---|
| Analyst (≤ 40 a session, batch) | $30–47 | $0.0155 a request, no verdict cache | **≤ $13** |
| Portfolio Manager | $5–12 | $0.10–0.30 a day it runs | **$2–6** |
| EOD review + weekly review | $3–5 | similar in size to a PM turn | **$2–3** |
| **Total** | **$35–65** | | **about $17–22** |

These are upper estimates (a full 40 requests every session, no verdict cache). Real prompts in Phase 4 will grow somewhat (the previous verdict and note, real base data, richer EP context), so keep a margin.

## Proposals for 0.4 / Phase 4

1. **README §9:** replace the estimate with the measured one (~$0.016 a request, ~$17–22 a month), keeping the note that it is an upper bound and will be re-measured in 4.6.
2. **Daily cost limit for 4.1/4.6:** alert above **$1.00 a session** for the Analyst batch (about 1.6× the measured $0.62 for 40 requests) and above **$0.50** for each real-time call. The Anthropic workspace limit can stay at the user's $20 until 4.6, then about $40.
3. **Effort:** choose on quality in 4.5. Cost isn't a reason to prefer `medium`.
4. **Caching:** test cache pre-warming before batch submission; otherwise caching in the batch is neutral.
5. **Length caps:** enforce `reasoning` and `watch_note` limits in code (truncate when storing). The model stretches them slightly.

## Not covered

- Quality. Five spike-grade setups can't judge verdicts; that is 4.5 (prompt sanity set) and the scorecard from 4.6.
- EP review cost (a larger morning context in earnings season).
- Batch turnaround at the planned 17:50 submission time (only one daytime sample).
- Refusals: none occurred, so the fallback path was configured but not exercised.
