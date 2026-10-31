"""Pure guardrail checks on the LLM's decision (README §8). 100% branch coverage, enforced in CI.

No I/O and no clock: the caller builds the context and logs the findings. Every threshold comes
from ``config/risk.yaml`` (``portfolio:``).

- ``model``: outcomes (approve / clip / reject) and findings
- ``portfolio``: the portfolio manager's limits (#5): the core band, the satellite, no increase
"""
