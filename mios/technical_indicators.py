"""
technical_indicators.py - Technical Indicator Engine for MIOS

Computes the five most useful indicators for confirming breakout signals:

  ┌──────────────────┬──────────────────────────────────────────────────────┐
  │ Indicator        │ Role                                                 │
  ├──────────────────┼──────────────────────────────────────────────────────┤
  │ RSI (14)         │ Momentum — confirms strength without being overbought │
  │ MACD (12,26,9)   │ Trend/momentum crossover — direction alignment        │
  │ Bollinger Bands  │ Volatility — squeeze → expansion confirms breakout    │
  │ EMA (20 / 50)    │ Trend filter — price must be on the correct side      │
  │ ADX (14)         │ Trend strength — ADX > 25 means a real trend move     │
  └──────────────────┴──────────────────────────────────────────────────────┘

Public API
----------
  compute_indicators(df)      → TechnicalSnapshot
  TechnicalFilter.enrich(signal, df) → (signal_with_notes, confirm_count, score_boost)
  TechnicalFilter.filter_signals(signals, stock_data) → filtered list

Integration
-----------
  The Scanner can pass OHLCV DataFrames here to get a TechnicalSnapshot.
  The RiskManager quality_score gets a +boost when indicators confirm direction.
"""

import logging
from dataclasses import dataclass, field
from typing import Optional

import pandas as pd

from mios.scanner import Signal

logger = logging.getLogger(__name__)

# ── Indicator defaults ────────────────────────────────────────────────────────
RSI_PERIOD: int = 14
MACD_FAST: int = 12
MACD_SLOW: int = 26
MACD_SIGNAL: int = 9
BB_PERIOD: int = 20
BB_STD: float = 2.0
EMA_FAST: int = 20
EMA_SLOW: int = 50
ADX_PERIOD: int = 14


# ── TechnicalSnapshot ─────────────────────────────────────────────────────────

@dataclass
class TechnicalSnapshot:
    """
    Holds the most-recent value of every indicator for one ticker.

    Attributes:
        ticker          : Yahoo Finance symbol.
        close           : Latest close price.
        rsi             : RSI-14 (0–100). >70 overbought, <30 oversold.
        macd            : MACD line (fast EMA − slow EMA).
        macd_signal     : MACD signal line (EMA of MACD).
        macd_hist       : MACD histogram (macd − signal).
        bb_upper        : Bollinger upper band.
        bb_middle       : Bollinger middle band (SMA-20).
        bb_lower        : Bollinger lower band.
        bb_width        : (upper − lower) / middle — volatility proxy.
        bb_pct          : Where price sits within the bands (0=lower, 1=upper).
        ema_fast        : EMA-20.
        ema_slow        : EMA-50.
        adx             : ADX-14 — trend strength (>25 = trending).
        di_plus         : +DI directional indicator.
        di_minus        : -DI directional indicator.
        trend           : 'UP', 'DOWN', or 'NEUTRAL' based on EMA alignment.
        confirmations   : Number of indicators that confirm the current trend.
    """
    ticker: str
    close: float
    # RSI
    rsi: Optional[float] = None
    # MACD
    macd: Optional[float] = None
    macd_signal: Optional[float] = None
    macd_hist: Optional[float] = None
    # Bollinger Bands
    bb_upper: Optional[float] = None
    bb_middle: Optional[float] = None
    bb_lower: Optional[float] = None
    bb_width: Optional[float] = None
    bb_pct: Optional[float] = None
    # EMAs
    ema_fast: Optional[float] = None
    ema_slow: Optional[float] = None
    # ADX
    adx: Optional[float] = None
    di_plus: Optional[float] = None
    di_minus: Optional[float] = None
    # Summary
    trend: str = "NEUTRAL"
    confirmations: int = 0
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        """Flat dict for DataFrame rows or JSON serialisation."""
        return {
            "ticker": self.ticker,
            "close": self.close,
            "rsi": self.rsi,
            "macd": self.macd,
            "macd_signal": self.macd_signal,
            "macd_hist": self.macd_hist,
            "bb_upper": self.bb_upper,
            "bb_middle": self.bb_middle,
            "bb_lower": self.bb_lower,
            "bb_width": self.bb_width,
            "bb_pct": self.bb_pct,
            "ema_fast": self.ema_fast,
            "ema_slow": self.ema_slow,
            "adx": self.adx,
            "di_plus": self.di_plus,
            "di_minus": self.di_minus,
            "trend": self.trend,
            "confirmations": self.confirmations,
        }


# ── Low-level indicator calculations ─────────────────────────────────────────

def _rsi(close: pd.Series, period: int = RSI_PERIOD) -> pd.Series:
    """Wilder-smoothed RSI."""
    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = (-delta).clip(lower=0)
    # Use EWM with alpha=1/period (Wilder's smoothing)
    avg_gain = gain.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, float("nan"))
    return 100 - (100 / (1 + rs))


def _macd(
    close: pd.Series,
    fast: int = MACD_FAST,
    slow: int = MACD_SLOW,
    signal: int = MACD_SIGNAL,
) -> tuple[pd.Series, pd.Series, pd.Series]:
    """Return (macd_line, signal_line, histogram)."""
    ema_fast = close.ewm(span=fast, adjust=False).mean()
    ema_slow = close.ewm(span=slow, adjust=False).mean()
    macd_line = ema_fast - ema_slow
    signal_line = macd_line.ewm(span=signal, adjust=False).mean()
    histogram = macd_line - signal_line
    return macd_line, signal_line, histogram


def _bollinger(
    close: pd.Series,
    period: int = BB_PERIOD,
    num_std: float = BB_STD,
) -> tuple[pd.Series, pd.Series, pd.Series]:
    """Return (upper, middle, lower)."""
    middle = close.rolling(period).mean()
    std = close.rolling(period).std(ddof=0)
    upper = middle + num_std * std
    lower = middle - num_std * std
    return upper, middle, lower


def _ema(close: pd.Series, span: int) -> pd.Series:
    return close.ewm(span=span, adjust=False).mean()


def _adx(
    high: pd.Series,
    low: pd.Series,
    close: pd.Series,
    period: int = ADX_PERIOD,
) -> tuple[pd.Series, pd.Series, pd.Series]:
    """
    Return (ADX, +DI, -DI) using Wilder's smoothing.

    ADX > 25 → trending; < 20 → choppy.
    """
    prev_high = high.shift(1)
    prev_low = low.shift(1)
    prev_close = close.shift(1)

    # True Range
    tr = pd.concat(
        [
            high - low,
            (high - prev_close).abs(),
            (low - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)

    # Directional movements
    dm_plus = high - prev_high
    dm_minus = prev_low - low

    dm_plus = dm_plus.where((dm_plus > dm_minus) & (dm_plus > 0), 0.0)
    dm_minus = dm_minus.where((dm_minus > dm_plus) & (dm_minus > 0), 0.0)

    # Wilder smooth
    atr_s = tr.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    di_plus_s = dm_plus.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    di_minus_s = dm_minus.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()

    di_plus = 100 * di_plus_s / atr_s.replace(0, float("nan"))
    di_minus = 100 * di_minus_s / atr_s.replace(0, float("nan"))

    dx = 100 * (di_plus - di_minus).abs() / (di_plus + di_minus).replace(0, float("nan"))
    adx = dx.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()

    return adx, di_plus, di_minus


# ── Public compute function ───────────────────────────────────────────────────

def compute_indicators(df: pd.DataFrame, ticker: str = "") -> Optional[TechnicalSnapshot]:
    """
    Compute all technical indicators for a single OHLCV DataFrame.

    Args:
        df    : DataFrame with columns [Open, High, Low, Close, Volume],
                indexed by datetime. At least 60 rows recommended.
        ticker: Optional ticker label (used for logging).

    Returns:
        TechnicalSnapshot or None if there is insufficient data.
    """
    required_cols = {"High", "Low", "Close"}
    if not required_cols.issubset(df.columns):
        logger.warning("%s: DataFrame missing required columns", ticker)
        return None

    min_bars = max(MACD_SLOW + MACD_SIGNAL, BB_PERIOD, EMA_SLOW, ADX_PERIOD) + 5
    if len(df) < min_bars:
        logger.debug("%s: Only %d bars — need %d for reliable indicators", ticker, len(df), min_bars)
        return None

    close = df["Close"]
    high = df["High"]
    low = df["Low"]
    latest_close = float(close.iloc[-1])

    snap = TechnicalSnapshot(ticker=ticker, close=round(latest_close, 2))

    # ── RSI ───────────────────────────────────────────────────────────────────
    try:
        rsi_series = _rsi(close)
        snap.rsi = round(float(rsi_series.iloc[-1]), 2)
    except Exception as exc:
        logger.debug("%s RSI failed: %s", ticker, exc)

    # ── MACD ──────────────────────────────────────────────────────────────────
    try:
        macd_line, sig_line, hist = _macd(close)
        snap.macd = round(float(macd_line.iloc[-1]), 4)
        snap.macd_signal = round(float(sig_line.iloc[-1]), 4)
        snap.macd_hist = round(float(hist.iloc[-1]), 4)
    except Exception as exc:
        logger.debug("%s MACD failed: %s", ticker, exc)

    # ── Bollinger Bands ───────────────────────────────────────────────────────
    try:
        bb_upper, bb_mid, bb_lower = _bollinger(close)
        upper = float(bb_upper.iloc[-1])
        mid = float(bb_mid.iloc[-1])
        lower = float(bb_lower.iloc[-1])
        band_width = (upper - lower) / mid if mid else 0.0
        # %B: 0 = at lower band, 1 = at upper band
        bb_pct = (latest_close - lower) / (upper - lower) if (upper - lower) else 0.5

        snap.bb_upper = round(upper, 2)
        snap.bb_middle = round(mid, 2)
        snap.bb_lower = round(lower, 2)
        snap.bb_width = round(band_width, 4)
        snap.bb_pct = round(bb_pct, 3)
    except Exception as exc:
        logger.debug("%s Bollinger failed: %s", ticker, exc)

    # ── EMAs ──────────────────────────────────────────────────────────────────
    try:
        snap.ema_fast = round(float(_ema(close, EMA_FAST).iloc[-1]), 2)
        snap.ema_slow = round(float(_ema(close, EMA_SLOW).iloc[-1]), 2)
    except Exception as exc:
        logger.debug("%s EMA failed: %s", ticker, exc)

    # ── ADX ───────────────────────────────────────────────────────────────────
    try:
        adx_s, dip, dim = _adx(high, low, close)
        snap.adx = round(float(adx_s.iloc[-1]), 2)
        snap.di_plus = round(float(dip.iloc[-1]), 2)
        snap.di_minus = round(float(dim.iloc[-1]), 2)
    except Exception as exc:
        logger.debug("%s ADX failed: %s", ticker, exc)

    # ── Trend summary ─────────────────────────────────────────────────────────
    if snap.ema_fast and snap.ema_slow:
        if snap.ema_fast > snap.ema_slow and latest_close > snap.ema_fast:
            snap.trend = "UP"
        elif snap.ema_fast < snap.ema_slow and latest_close < snap.ema_fast:
            snap.trend = "DOWN"
        else:
            snap.trend = "NEUTRAL"

    logger.debug(
        "%s → RSI=%.1f  MACD=%.3f  BB%%=%.2f  ADX=%.1f  Trend=%s",
        ticker,
        snap.rsi or 0,
        snap.macd or 0,
        snap.bb_pct or 0,
        snap.adx or 0,
        snap.trend,
    )

    return snap


# ── TechnicalFilter ───────────────────────────────────────────────────────────

class TechnicalFilter:
    """
    Enriches Scanner signals with technical indicator confirmations and
    applies a multi-indicator filter to reduce false breakouts.

    Scoring approach
    ----------------
    Each indicator can contribute one confirmation (True/False):

      LONG confirmations:
        1. RSI in 50–70 (momentum without being overbought)
        2. MACD histogram positive and growing (rising momentum)
        3. Price above BB middle (bullish bias) — or >BB upper (power breakout)
        4. EMA20 > EMA50 AND price > EMA20 (uptrend alignment)
        5. ADX > 25 (real trend, not a fakeout)

      SHORT confirmations (mirrored):
        1. RSI in 30–50
        2. MACD histogram negative and falling
        3. Price below BB middle
        4. EMA20 < EMA50 AND price < EMA20
        5. ADX > 25

    The quality_score from RiskManager gets boosted by up to +15 pts
    (3 pts per confirmation) to reward highly-confirmed setups.
    """

    def __init__(
        self,
        min_confirmations: int = 2,
        rsi_long_min: float = 45.0,
        rsi_long_max: float = 75.0,
        rsi_short_min: float = 25.0,
        rsi_short_max: float = 55.0,
        adx_trend_threshold: float = 20.0,
        score_boost_per_confirmation: int = 3,
    ):
        """
        Args:
            min_confirmations           : Minimum confirmations required to pass
                                          the filter (signals with fewer are kept
                                          but flagged with reduced score).
            rsi_long_min / max          : RSI acceptance range for longs.
            rsi_short_min / max         : RSI acceptance range for shorts.
            adx_trend_threshold         : Minimum ADX to count as trending.
            score_boost_per_confirmation: Quality score added per confirmation.
        """
        self.min_confirmations = min_confirmations
        self.rsi_long_min = rsi_long_min
        self.rsi_long_max = rsi_long_max
        self.rsi_short_min = rsi_short_min
        self.rsi_short_max = rsi_short_max
        self.adx_threshold = adx_trend_threshold
        self.boost_per = score_boost_per_confirmation

    # ── Confirmation checkers ─────────────────────────────────────────────────

    def _check_rsi(self, snap: TechnicalSnapshot, direction: str) -> tuple[bool, str]:
        if snap.rsi is None:
            return False, "RSI: N/A"
        if direction == "LONG":
            ok = self.rsi_long_min <= snap.rsi <= self.rsi_long_max
            label = f"RSI {snap.rsi:.1f} {'✓ in bullish zone' if ok else '✗ outside 45–75'}"
        else:
            ok = self.rsi_short_min <= snap.rsi <= self.rsi_short_max
            label = f"RSI {snap.rsi:.1f} {'✓ in bearish zone' if ok else '✗ outside 25–55'}"
        return ok, label

    def _check_macd(self, snap: TechnicalSnapshot, direction: str) -> tuple[bool, str]:
        if snap.macd is None or snap.macd_hist is None:
            return False, "MACD: N/A"
        if direction == "LONG":
            ok = snap.macd_hist > 0 and snap.macd > snap.macd_signal
            label = f"MACD hist {snap.macd_hist:+.3f} {'✓ bullish' if ok else '✗ not bullish'}"
        else:
            ok = snap.macd_hist < 0 and snap.macd < snap.macd_signal
            label = f"MACD hist {snap.macd_hist:+.3f} {'✓ bearish' if ok else '✗ not bearish'}"
        return ok, label

    def _check_bb(self, snap: TechnicalSnapshot, direction: str) -> tuple[bool, str]:
        if snap.bb_pct is None or snap.bb_middle is None:
            return False, "BB: N/A"
        if direction == "LONG":
            ok = snap.bb_pct > 0.5    # above mid-band; >1.0 = full breakout above upper
            label = (
                f"BB%B {snap.bb_pct:.2f} "
                f"{'✓ above mid (power breakout)' if snap.bb_pct >= 1.0 else '✓ above mid-band' if ok else '✗ below mid-band'}"
            )
        else:
            ok = snap.bb_pct < 0.5
            label = (
                f"BB%B {snap.bb_pct:.2f} "
                f"{'✓ below mid (breakdown)' if ok else '✗ above mid-band'}"
            )
        return ok, label

    def _check_ema(self, snap: TechnicalSnapshot, direction: str) -> tuple[bool, str]:
        if snap.ema_fast is None or snap.ema_slow is None:
            return False, "EMA: N/A"
        if direction == "LONG":
            ok = snap.ema_fast > snap.ema_slow and snap.close > snap.ema_fast
            label = (
                f"EMA20={snap.ema_fast:.2f} EMA50={snap.ema_slow:.2f} "
                f"{'✓ bullish stack' if ok else '✗ no uptrend alignment'}"
            )
        else:
            ok = snap.ema_fast < snap.ema_slow and snap.close < snap.ema_fast
            label = (
                f"EMA20={snap.ema_fast:.2f} EMA50={snap.ema_slow:.2f} "
                f"{'✓ bearish stack' if ok else '✗ no downtrend alignment'}"
            )
        return ok, label

    def _check_adx(self, snap: TechnicalSnapshot, direction: str) -> tuple[bool, str]:
        if snap.adx is None:
            return False, "ADX: N/A"
        ok = snap.adx >= self.adx_threshold
        if ok and direction == "LONG":
            ok = ok and (snap.di_plus or 0) > (snap.di_minus or 0)
        elif ok and direction == "SHORT":
            ok = ok and (snap.di_minus or 0) > (snap.di_plus or 0)
        label = (
            f"ADX {snap.adx:.1f} "
            f"(+DI={snap.di_plus:.1f} -DI={snap.di_minus:.1f}) "
            f"{'✓ trending' if snap.adx >= self.adx_threshold else '✗ choppy (<' + str(self.adx_threshold) + ')'}"
        )
        return ok, label

    # ── Public methods ────────────────────────────────────────────────────────

    def enrich(
        self,
        signal: Signal,
        snap: TechnicalSnapshot,
    ) -> tuple[Signal, int, int]:
        """
        Add indicator notes to a Signal and count confirmations.

        Args:
            signal : Scanner Signal to enrich.
            snap   : Pre-computed TechnicalSnapshot for the same ticker.

        Returns:
            (enriched_signal, confirm_count, score_boost)
              enriched_signal — same Signal object with notes appended
              confirm_count   — number of indicators that agree with direction
              score_boost     — quality score points to add in RiskManager
        """
        direction = "LONG" if "LONG" in signal.signal_type else "SHORT"
        checks = [
            self._check_rsi(snap, direction),
            self._check_macd(snap, direction),
            self._check_bb(snap, direction),
            self._check_ema(snap, direction),
            self._check_adx(snap, direction),
        ]

        confirm_count = sum(1 for ok, _ in checks if ok)
        signal.notes.append(f"Technical confirmations: {confirm_count}/5")
        for _, label in checks:
            signal.notes.append(f"  {label}")

        score_boost = confirm_count * self.boost_per
        logger.info(
            "%s [%s] — %d/5 confirmations, score boost +%d",
            signal.ticker, direction, confirm_count, score_boost,
        )
        return signal, confirm_count, score_boost

    def filter_signals(
        self,
        signals: list[Signal],
        stock_data: dict[str, "pd.DataFrame"],
        drop_below_min: bool = False,
    ) -> list[Signal]:
        """
        Compute indicators for every signal and enrich each with indicator notes.

        Args:
            signals        : Raw Signal objects from Scanner.
            stock_data     : Dict {ticker: DataFrame} — same data used for scanning.
            drop_below_min : If True, signals with fewer than min_confirmations
                             are discarded entirely. If False (default), they are
                             kept but their score is not boosted.

        Returns:
            List of (optionally filtered) Signal objects with notes enriched.
        """
        enriched: list[Signal] = []

        for signal in signals:
            df = stock_data.get(signal.ticker)
            if df is None:
                logger.debug("%s: No DataFrame for indicator calculation", signal.ticker)
                enriched.append(signal)
                continue

            snap = compute_indicators(df, ticker=signal.ticker)
            if snap is None:
                logger.debug("%s: Insufficient data for indicators", signal.ticker)
                enriched.append(signal)
                continue

            signal, confirm_count, _ = self.enrich(signal, snap)

            if drop_below_min and confirm_count < self.min_confirmations:
                logger.info(
                    "%s dropped — only %d/%d confirmations",
                    signal.ticker, confirm_count, self.min_confirmations,
                )
                continue

            enriched.append(signal)

        logger.info(
            "TechnicalFilter complete — %d/%d signals passed",
            len(enriched), len(signals),
        )
        return enriched

    def get_snapshots(
        self,
        tickers: list[str],
        stock_data: dict[str, "pd.DataFrame"],
    ) -> list[TechnicalSnapshot]:
        """
        Compute TechnicalSnapshot for a list of tickers (no signal needed).
        Useful for the dashboard's sector/watchlist view.

        Args:
            tickers    : List of ticker symbols.
            stock_data : Dict {ticker: DataFrame}.

        Returns:
            List of TechnicalSnapshot objects (excludes tickers with bad data).
        """
        snapshots: list[TechnicalSnapshot] = []
        for ticker in tickers:
            df = stock_data.get(ticker)
            if df is None:
                continue
            snap = compute_indicators(df, ticker=ticker)
            if snap:
                snapshots.append(snap)
        return snapshots
