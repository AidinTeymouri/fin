"""Tests for the GBM market data simulator."""

import math
import random

import numpy as np
import pytest

import app.market.simulator as simulator
from app.market.cache import price_cache
from app.market.simulator import (
    TICKER_CONFIG,
    TECH_TICKERS,
    FINANCE_TICKERS,
    _build_correlation_matrix,
    Simulator,
)


def test_ticker_config_has_required_fields():
    for ticker, cfg in TICKER_CONFIG.items():
        assert "seed" in cfg, f"{ticker} missing seed"
        assert "drift" in cfg, f"{ticker} missing drift"
        assert "vol" in cfg, f"{ticker} missing vol"
        assert cfg["seed"] > 0, f"{ticker} seed must be positive"
        assert cfg["vol"] > 0, f"{ticker} volatility must be positive"


def test_correlation_matrix_is_symmetric():
    tickers = list(TICKER_CONFIG.keys())
    corr = _build_correlation_matrix(tickers)
    np.testing.assert_array_almost_equal(corr, corr.T)


def test_correlation_matrix_has_ones_on_diagonal():
    tickers = list(TICKER_CONFIG.keys())
    corr = _build_correlation_matrix(tickers)
    np.testing.assert_array_almost_equal(np.diag(corr), np.ones(len(tickers)))


def test_correlation_matrix_tech_cluster():
    tickers = list(TICKER_CONFIG.keys())
    corr = _build_correlation_matrix(tickers)
    # Find two tech tickers
    i = tickers.index("AAPL")
    j = tickers.index("GOOGL")
    assert corr[i, j] == 0.6


def test_correlation_matrix_finance_cluster():
    tickers = list(TICKER_CONFIG.keys())
    corr = _build_correlation_matrix(tickers)
    i = tickers.index("JPM")
    j = tickers.index("V")
    assert corr[i, j] == 0.5


def test_correlation_matrix_cross_sector():
    tickers = list(TICKER_CONFIG.keys())
    corr = _build_correlation_matrix(tickers)
    i = tickers.index("AAPL")
    j = tickers.index("JPM")
    assert corr[i, j] == 0.2


def test_correlation_matrix_is_positive_definite():
    """A valid correlation matrix must be positive definite for Cholesky."""
    tickers = list(TICKER_CONFIG.keys())
    corr = _build_correlation_matrix(tickers)
    # Cholesky will raise if not positive definite
    np.linalg.cholesky(corr)


def test_simulator_initializes_with_seed_prices():
    sim = Simulator()
    for ticker, cfg in TICKER_CONFIG.items():
        assert sim._prices[ticker] == cfg["seed"]


def test_simulator_step_produces_valid_prices():
    sim = Simulator()
    sim._step()
    for ticker in TICKER_CONFIG:
        assert sim._prices[ticker] >= 0.01, f"{ticker} price below floor"


def test_simulator_step_changes_prices():
    """After stepping, at least some prices should have changed."""
    sim = Simulator()
    before = dict(sim._prices)
    sim._step()
    changed = sum(1 for t in TICKER_CONFIG if sim._prices[t] != before[t])
    assert changed > 0, "No prices changed after step"


def test_simulator_price_floor():
    """Prices should never go below 0.01."""
    sim = Simulator()
    # Force very low prices
    for ticker in sim._prices:
        sim._prices[ticker] = 0.02
    sim._step()
    for ticker in sim._prices:
        assert sim._prices[ticker] >= 0.01


# ---------------------------------------------------------------------------
# GBM math
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def clean_cache():
    price_cache._prices.clear()
    yield
    price_cache._prices.clear()


def _seed(n: int) -> None:
    np.random.seed(n)
    random.seed(n)


def _run_steps(seed: int, steps: int) -> dict[str, float]:
    _seed(seed)
    sim = Simulator()
    for _ in range(steps):
        sim._step()
    return dict(sim._prices)


def test_seeded_simulation_is_deterministic():
    assert _run_steps(42, 200) == _run_steps(42, 200)


def test_different_seeds_diverge():
    assert _run_steps(1, 50) != _run_steps(2, 50)


def test_single_step_matches_gbm_formula(monkeypatch):
    """With events off and a fixed draw, one step is exactly S * (1 + mu*dt + sigma*sqrt(dt)*Z)."""
    monkeypatch.setattr(simulator, "EVENT_PROBABILITY", 0.0)
    monkeypatch.setattr(np.random, "standard_normal", lambda n: np.full(n, 1.5))
    sim = Simulator(tickers=["AAPL"])  # one ticker: Cholesky factor is [[1]]
    cfg = TICKER_CONFIG["AAPL"]
    dt = sim._dt
    assert dt == pytest.approx(0.5 / (252 * 6.5 * 3600))

    sim._step()
    expected = cfg["seed"] * (1 + cfg["drift"] * dt + cfg["vol"] * math.sqrt(dt) * 1.5)
    assert sim._prices["AAPL"] == pytest.approx(expected, rel=1e-12)
    assert price_cache.get("AAPL").price == round(expected, 2)


def test_zero_draw_applies_pure_drift(monkeypatch):
    monkeypatch.setattr(simulator, "EVENT_PROBABILITY", 0.0)
    monkeypatch.setattr(np.random, "standard_normal", lambda n: np.zeros(n))
    sim = Simulator()
    sim._step()
    for ticker, cfg in TICKER_CONFIG.items():
        assert sim._prices[ticker] == pytest.approx(cfg["seed"] * (1 + cfg["drift"] * sim._dt))


def test_return_volatility_matches_configured_vol(monkeypatch):
    """Sample std of per-step log returns ~ sigma * sqrt(dt)."""
    monkeypatch.setattr(simulator, "EVENT_PROBABILITY", 0.0)
    _seed(7)
    sim = Simulator(tickers=["TSLA"])
    prices = [sim._prices["TSLA"]]
    for _ in range(5_000):
        sim._step()
        prices.append(sim._prices["TSLA"])
    log_returns = np.diff(np.log(prices))
    expected = TICKER_CONFIG["TSLA"]["vol"] * math.sqrt(sim._dt)
    assert np.std(log_returns) == pytest.approx(expected, rel=0.05)


def test_tech_tickers_move_together(monkeypatch):
    """Correlated draws: AAPL/MSFT returns correlate ~0.6, AAPL/JPM ~0.2."""
    monkeypatch.setattr(simulator, "EVENT_PROBABILITY", 0.0)
    _seed(11)
    sim = Simulator(tickers=["AAPL", "MSFT", "JPM"])
    history = {t: [sim._prices[t]] for t in sim.get_tickers()}
    for _ in range(5_000):
        sim._step()
        for t in history:
            history[t].append(sim._prices[t])
    r = {t: np.diff(np.log(p)) for t, p in history.items()}
    assert np.corrcoef(r["AAPL"], r["MSFT"])[0, 1] == pytest.approx(0.6, abs=0.05)
    assert np.corrcoef(r["AAPL"], r["JPM"])[0, 1] == pytest.approx(0.2, abs=0.05)


@pytest.mark.parametrize("seed", [0, 1])
def test_prices_stay_positive_over_long_run(seed):
    _seed(seed)
    sim = Simulator()
    for _ in range(1_000):
        sim._step()
        assert all(p > 0 for p in sim._prices.values())
    assert all(u.price > 0 for u in price_cache.get_all())


def test_random_events_move_price_between_2_and_5_percent(monkeypatch):
    """With diffusion off and events forced, every step is a 2-5% jump up or down."""
    monkeypatch.setattr(simulator, "EVENT_PROBABILITY", 1.0)
    _seed(3)
    sim = Simulator(tickers=["AAPL"])
    sim._params["AAPL"] = {"seed": 100.0, "drift": 0.0, "vol": 0.0}
    moves = []
    for _ in range(500):
        sim._prices["AAPL"] = 100.0
        sim._step()
        moves.append(sim._prices["AAPL"] / 100.0 - 1)

    magnitudes = [abs(m) for m in moves]
    assert min(magnitudes) >= simulator.EVENT_MIN_PCT - 1e-12
    assert max(magnitudes) <= simulator.EVENT_MAX_PCT + 1e-12
    assert any(m > 0 for m in moves) and any(m < 0 for m in moves)


def test_events_are_rare_by_default():
    """At the default probability, roughly 0.5% of ticker-steps carry a jump."""
    _seed(5)
    sim = Simulator(tickers=["AAPL"])
    sim._params["AAPL"] = {"seed": 100.0, "drift": 0.0, "vol": 0.0}
    jumps = 0
    steps = 10_000
    for _ in range(steps):
        sim._prices["AAPL"] = 100.0
        sim._step()
        jumps += sim._prices["AAPL"] != 100.0
    assert jumps / steps == pytest.approx(simulator.EVENT_PROBABILITY, rel=0.4)
