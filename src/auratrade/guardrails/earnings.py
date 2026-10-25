"""The earnings blackout for new entries and tranche 2 (archived README §8), EPs exempt.

Sessions are counted by the caller with the market calendar:

- ``next_report_in``: sessions from the entry session to the next report's session (0: the
  report is on the entry session); None when no next date is known.
- ``last_report_ago``: sessions since the last report (0: today); None with no report history.

A known next report within ``earnings_blackout_sessions`` blocks the entry. With no known date,
the next report is estimated at the last + ``earnings_estimate_sessions``, and the blackout is
``earnings_estimate_window_sessions`` either side of the estimate. With no report history at
all, the entry is rejected.
"""

from auratrade.config import GuardrailsConfig, Strategy
from auratrade.guardrails.model import Finding, GuardrailRule, reject


def earnings_blackout(
    strategy: Strategy,
    *,
    next_report_in: int | None,
    last_report_ago: int | None,
    config: GuardrailsConfig,
) -> Finding | None:
    if strategy is Strategy.EPISODIC_PIVOT:
        return None
    if next_report_in is not None:
        if next_report_in < 0:
            return reject(
                GuardrailRule.INVALID_INPUT, "the next report is in the past", next_report_in
            )
        if next_report_in <= config.earnings_blackout_sessions:
            return reject(
                GuardrailRule.EARNINGS_BLACKOUT,
                f"the next report is {next_report_in} sessions away",
                next_report_in,
            )
        return None
    if last_report_ago is None or last_report_ago < 0:
        return reject(GuardrailRule.EARNINGS_BLACKOUT, "no report history to estimate the next")
    estimate_in = config.earnings_estimate_sessions - last_report_ago
    if abs(estimate_in) <= config.earnings_estimate_window_sessions:
        return reject(
            GuardrailRule.EARNINGS_BLACKOUT,
            f"inside the estimated report window ({estimate_in} sessions from the estimate)",
            estimate_in,
        )
    return None
