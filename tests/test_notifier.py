"""Which events reach Telegram, and how they read there."""

from __future__ import annotations

import asyncio

from app.alerts.notifier import Notifier
from app.decision.service import DecisionOutcome, prefilter_wait
from app.engine import _setup_message
from app.market_data.service import MarketDataService
from app.market_data.state import MarketState
from app.reconciliation import reconciler as reconciliation
from app.reconciliation.reconciler import Reconciler
from app.strategy.trend_pullback import evaluate_trend_pullback
from tests.conftest import make_settings
from tests.helpers import long_state


class FakeTelegram:
    enabled = True

    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send(self, text: str) -> bool:
        self.sent.append(text)
        return True


def notifier() -> tuple[Notifier, FakeTelegram]:
    tg = FakeTelegram()
    return Notifier(None, tg, label="[DEMO]"), tg  # type: ignore[arg-type]


async def test_only_errors_or_explicit_alerts_reach_telegram():
    n, tg = notifier()
    await n.info("x", "ROUTINE", "logged only")
    await n.warning("x", "STREAM_DISCONNECTED", "logged only")
    await n.error("x", "SOMETHING_BROKE", "sent", dedup_key="a")
    await n.critical("x", "DRAWDOWN_BREAKER", "sent", dedup_key="b")
    await n.info("x", "ORDER_FILLED", "sent", alert=True)
    await n.error("x", "STREAM_DISCONNECTED", "kept off Telegram", alert=False, dedup_key="c")
    assert tg.sent == [
        "[DEMO] ❌ Something broke\nsent",
        "[DEMO] 🚨 Drawdown limit hit\nsent",
        "[DEMO] ✅ Order filled\nsent",
    ]


async def test_alerts_are_deduplicated():
    n, tg = notifier()
    for _ in range(3):
        await n.critical("x", "TASK_CRASHED", "boom")
    assert len(tg.sent) == 1


class FailingStream:
    def __init__(self, failures: int) -> None:
        self.failures = failures

    async def stream_quotes(self):
        from app.broker.capital import CapitalTransportError

        if self.failures:
            self.failures -= 1
            raise CapitalTransportError("connection reset")
        yield None  # connected again (heartbeat)
        await asyncio.sleep(3600)


async def test_stream_disconnects_do_not_reach_telegram(monkeypatch):
    n, tg = notifier()
    svc = MarketDataService(make_settings(), FailingStream(3), None, n, MarketState("EUR_USD", 0.0001))  # type: ignore[arg-type]
    real_wait_for = asyncio.wait_for
    monkeypatch.setattr(asyncio, "wait_for", lambda aw, timeout: real_wait_for(aw, 0))
    stop = asyncio.Event()
    task = asyncio.create_task(svc.run_stream(stop))
    await asyncio.sleep(0.05)
    stop.set()
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    assert svc.state.reconnects == 3 and svc.state.stream_connected
    assert tg.sent == []  # 3 disconnects and a reconnect: logged, not sent


class BrokenBroker:
    async def get_positions(self):
        raise RuntimeError("HTTP 503")


async def test_reconciliation_alerts_only_after_repeated_failures():
    n, tg = notifier()
    rec = Reconciler(make_settings(), BrokenBroker(), None, n, None)  # type: ignore[arg-type]
    for _ in range(reconciliation.RECONCILE_ALERT_AFTER_FAILURES - 1):
        assert not await rec.reconcile_once()
    assert tg.sent == []
    assert not await rec.reconcile_once()
    assert len(tg.sent) == 1 and tg.sent[0].startswith("[DEMO] ❌ Reconciliation failing")


def _strategy():
    s = make_settings()
    st = evaluate_trend_pullback(long_state(), 1.10245, 1.10255, s)
    assert st.candidate and st.trade_plan
    return st


def test_setup_message_shows_plan_and_model_verdict():
    st = _strategy()
    buy = DecisionOutcome(source="LLM", decision="BUY", setup=st.setup, confidence=0.8,
                          reason_codes=["HTF_BULLISH"], valid=True)
    text = _setup_message("EUR_USD", st, buy)
    first, plan, verdict = text.split("\n")
    assert first == f"BUY EUR_USD · {st.setup}"
    assert plan.startswith("Entry ") and " SL " in plan and " TP " in plan and "R:R" in plan
    assert verdict == "Model: BUY (80%) → checking risk"

    wait = DecisionOutcome(source="LLM", decision="WAIT", setup=st.setup, confidence=0.55,
                           reason_codes=["NO_ROOM_TO_TARGET"], valid=True)
    assert _setup_message("EUR_USD", st, wait).endswith("Model: WAIT (55%), no trade (NO_ROOM_TO_TARGET)")
    assert _setup_message("EUR_USD", st, prefilter_wait(["NEWS_BLACKOUT"])).endswith("No trade: NEWS_BLACKOUT")
