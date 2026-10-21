"""Production wiring for the job runner: settings → concrete dependencies."""

from __future__ import annotations

from sqlalchemy import Engine

from auratrade.config import Settings, load_risk_config, load_settings
from auratrade.db.engine import engine_from_settings
from auratrade.jobs.base import RunnerDeps, SessionCalendar
from auratrade.jobs.invoke import JobInvoker, LambdaInvoker
from auratrade.jobs.runs import JobRunStore
from auratrade.log import configure_logging
from auratrade.notify.alerts import Alerter, LogAlerter
from auratrade.notify.telegram import TelegramNotifier
from auratrade.storage import make_store


def load_runtime_settings() -> Settings:
    from auratrade.parameters import load_into_environment

    load_into_environment()  # AWS: the secrets under SSM_PARAMETER_PATH; a no-op locally
    return load_settings()


def make_alerter(settings: Settings) -> Alerter:
    if settings.telegram_bot_token is None or not settings.telegram_chat_id:
        return LogAlerter()
    token = settings.telegram_bot_token.get_secret_value()
    return TelegramNotifier(token, settings.telegram_chat_id)


def make_job_run_store(engine: Engine) -> JobRunStore:
    from auratrade.db.job_runs import SqlJobRunStore

    return SqlJobRunStore(engine)


def make_calendar(engine: Engine) -> SessionCalendar:
    from auratrade.calendar.sessions import DbCalendar

    return DbCalendar(engine)


def build_deps(settings: Settings, invoker: JobInvoker | None) -> RunnerDeps:
    """`invoker=None` means Lambda: chained successors are invoked asynchronously."""
    redactor = configure_logging(settings.secret_values())
    if invoker is None:
        import boto3

        invoker = LambdaInvoker(
            boto3.client("lambda"),
            function_template=settings.job_function_template,
            environment=settings.environment,
        )
    # One engine (and pool) for the store and the calendar; it connects lazily, so a
    # database outage surfaces inside the runner, which records and alerts it.
    engine = engine_from_settings(settings)
    return RunnerDeps(
        settings=settings,
        risk=load_risk_config(settings.risk_config_path),
        store=make_job_run_store(engine),
        calendar=make_calendar(engine),
        alerter=make_alerter(settings),
        invoker=invoker,
        storage=make_store(settings),
        redactor=redactor,
    )
