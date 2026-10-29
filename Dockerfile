# One image for every job's Lambda function (README §12); each function sets its own handler,
# `auratrade.jobs.<job>.handler`. Built and pushed by `cdk deploy` (infra/stacks/trading.py).
# The `mcp` target adds the remote MCP server on top (infra/stacks/public_site.py).
FROM ghcr.io/astral-sh/uv:0.10.12 AS uv

FROM public.ecr.aws/lambda/python:3.13 AS jobs

COPY --from=uv /uv /bin/uv
WORKDIR ${LAMBDA_TASK_ROOT}

# Dependencies first, from the lock file, so a code change rebuilds only the last layers.
# The project itself, the dev and infra groups stay out of the image.
COPY pyproject.toml uv.lock ./
RUN uv export --frozen --no-default-groups --no-dev --no-emit-project -o /tmp/requirements.txt \
    && uv pip install --python /var/lang/bin/python3 --target ${LAMBDA_TASK_ROOT} \
        --compile-bytecode --no-cache -r /tmp/requirements.txt \
    && rm /tmp/requirements.txt pyproject.toml uv.lock

COPY config/ ${LAMBDA_TASK_ROOT}/config/
COPY src/auratrade/ ${LAMBDA_TASK_ROOT}/auratrade/
# The code's own layout puts risk.yaml two levels above the package; here it sits beside it.
ENV RISK_CONFIG_PATH=${LAMBDA_TASK_ROOT}/config/risk.yaml
# matplotlib (the Portfolio Manager's charts) writes its cache here; the rest of the filesystem is read-only.
ENV MPLCONFIGDIR=/tmp/matplotlib
RUN /var/lang/bin/python3 -m compileall -q ${LAMBDA_TASK_ROOT}/auratrade

CMD ["auratrade.jobs.watchdog.handler"]

# The remote MCP server (README §5.10): the jobs' layers, the `mcp` group and the documents its
# resources serve. Its own target, so the jobs' functions never carry it.
FROM jobs AS mcp
COPY pyproject.toml uv.lock ./
RUN uv export --frozen --only-group mcp --no-emit-project -o /tmp/requirements.txt \
    && uv pip install --python /var/lang/bin/python3 --target ${LAMBDA_TASK_ROOT} \
        --compile-bytecode --no-cache -r /tmp/requirements.txt \
    && rm /tmp/requirements.txt pyproject.toml uv.lock
COPY README.md ${LAMBDA_TASK_ROOT}/
COPY docs/runbook.md ${LAMBDA_TASK_ROOT}/docs/
COPY docs/backtests/portfolio.md ${LAMBDA_TASK_ROOT}/docs/backtests/
# The dashboard's page inlines the site's drawing (task 6.6).
COPY web/chart.js web/chart.css ${LAMBDA_TASK_ROOT}/web/
ENV AURATRADE_DOCS_ROOT=${LAMBDA_TASK_ROOT}
CMD ["auratrade.mcp_server.remote.handler"]
