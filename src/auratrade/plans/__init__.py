"""Position plans, early-behaviour and exit rules, rule precedence (README §7).

Pure: no I/O, no clock. Thresholds come from ``config/risk.yaml`` (``plans:``, ``climax:``);
base counts (2.2) and regime changes (2.3) arrive as typed inputs. Sessions count from the
entry session (1). Every price level comes from code; nothing here takes an unchecked price
from the LLM.

- ``model``: the versioned plan, the per-session input (``Day``) and the outcome
- ``entry``: entry policies, the tranche-2 confirmation gate, the plan written at entry
- ``versioning``: tighten-only versions, LLM tightenings, tranche 2, splits
- ``early``: early-behaviour rules (heavy-volume reversal, dip below the pivot, time stops)
- ``exits``: strength partials, climax and late-stage sales, trails, stop-raising milestones
- ``hold``: the 8-week hold and regime-downgrade tightening
- ``cushion``: the earnings cushion, including estimated report dates
- ``levels``: hard-stop and soft-rule levels
- ``resolve``: rule precedence: the one place the rules are combined
"""
