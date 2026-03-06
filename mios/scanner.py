"""
scanner.py - Breakout & Opportunity Scanner for MIOS

Scans a watchlist of stocks and flags those that meet breakout criteria:

  1. Price Breakout  – today's close crosses above the recent N-day high.
  2. Volume Spike    – today's volume is significantly above the average.
  3. Momentum Filter – minimum % price move on the breakout day.

Detected signals are passed to the RiskManager to generate full trade setups.
"""

import logging
from dataclasses import dataclass, field
from typing import Optional

import pandas as pd

from mios.data_fetcher import DataFetcher, NIFTY50_TICKERS

logger = logging.getLogger(__name__)


# ── Signal dataclass ─────────────────────────────────────────────────────────

@dataclass
class Signal:
    """
    Represents a detected breakout signal for a single stock.

    Attributes:
        ticker          : Yahoo Finance ticker symbol (e.g. 'RELIANCE.NS').
        signal_type     : 'BREAKOUT_LONG' or 'BREAKOUT_SHORT'.
        close           : Closing price on the signal bar.
        high            : Intraday high of the signal bar.
        low             : Intraday low of the signal bar.
        volume          : Volume on the signal bar.
        avg_volume      : Average volume over the look-back window.
        volume_ratio    : volume / avg_volume (e.g. 2.5 → 2.5× avg).
        change_pct      : % price change vs previous close.
        resistance_level: N-day high that was broken (for longs).
        support_level   : N-day low that was broken (for shorts).
        atr             : Average True Range — used for SL/target sizing.
        notes           : Human-readable description of why signal fired.
    """
    ticker: str
    signal_type: str              # 'BREAKOUT_LONG' | 'BREAKOUT_SHORT'
    close: float
    high: float
    low: float
    volume: int
    avg_volume: float
    volume_ratio: float
    change_pct: float
    resistance_level: Optional[float] = None
    support_level: Optional[float] = None
    atr: Optional[float] = None
    notes: list[str] = field(default_factory=list)


# ── Scanner class ─────────────────────────────────────────────────────────────

class Scanner:
    """
    Scans stocks for technical breakout opportunities.

    Breakout long  → price closes above the N-day high AND volume spikes.
    Breakout short → price closes below the N-day low  AND volume spikes.
    """

    def __init__(
        self,
        lookback_days: int = 20,
        volume_spike_threshold: float = 1.5,
        min_price_change_pct: float = 0.5,
        atr_period: int = 14,
        data_period: str = "60d",
    ):
        """
        Args:
            lookback_days           : Rolling window to find support/resistance.
            volume_spike_threshold  : Minimum volume ratio to qualify as a spike
                                      (e.g. 1.5 → volume must be ≥ 1.5× average).
            min_price_change_pct    : Minimum absolute % move on the signal day.
            atr_period              : Period for Average True Range calculation.
            data_period             : Historical data window passed to DataFetcher
                                      (must be long enough for lookback_days).
        """
        self.lookback_days = lookback_days
        self.volume_spike_threshold = volume_spike_threshold
        self.min_price_change_pct = min_price_change_pct
        self.atr_period = atr_period
        # Fetch daily bars; intraday available too (pass '15m' etc.)
        self.fetcher = DataFetcher(period=data_period, interval="1d")

    # ── ATR calculation ───────────────────────────────────────────────────────

    def _compute_atr(self, df: pd.DataFrame) -> float:
        """
        Compute the Average True Range (ATR) over self.atr_period bars.

        True Range = max(High-Low, |High-PrevClose|, |Low-PrevClose|)
        ATR        = rolling mean of TR over the chosen period.
        """
        high = df["High"]
        low = df["Low"]
        prev_close = df["Close"].shift(1)

        tr = pd.concat(
            [
                high - low,
                (high - prev_close).abs(),
                (low - prev_close).abs(),
            ],
            axis=1,
        ).max(axis=1)

        atr = tr.rolling(self.atr_period).mean().iloc[-1]
        return float(atr) if pd.notna(atr) else float(high.iloc[-1] - low.iloc[-1])

    # ── Core signal detection ─────────────────────────────────────────────────

    def _analyse_stock(self, ticker: str, df: pd.DataFrame) -> Optional[Signal]:
        """
        Run breakout logic on a single stock's OHLCV DataFrame.

        Returns a Signal if criteria are met, otherwise None.
        """
        if len(df) < self.lookback_days + 2:
            logger.debug("%s: Not enough bars (%d)", ticker, len(df))
            return None

        # ── Latest bar ────────────────────────────────────────────────────────
        today = df.iloc[-1]
        yesterday = df.iloc[-2]

        today_close = float(today["Close"])
        today_high = float(today["High"])
        today_low = float(today["Low"])
        today_vol = float(today["Volume"])
        prev_close = float(yesterday["Close"])

        # ── Rolling support / resistance (exclude today) ──────────────────────
        lookback = df.iloc[-(self.lookback_days + 1) : -1]   # N days before today
        resistance = float(lookback["High"].max())            # N-day high
        support = float(lookback["Low"].min())                # N-day low

        # ── Volume stats ──────────────────────────────────────────────────────
        avg_volume = float(lookback["Volume"].mean())
        volume_ratio = today_vol / avg_volume if avg_volume > 0 else 0.0

        # ── Price change ──────────────────────────────────────────────────────
        change_pct = ((today_close - prev_close) / prev_close) * 100 if prev_close else 0.0

        # ── ATR ───────────────────────────────────────────────────────────────
        atr = self._compute_atr(df)

        # ── Filter: volume must spike ─────────────────────────────────────────
        if volume_ratio < self.volume_spike_threshold:
            logger.debug(
                "%s: Volume ratio %.2f below threshold %.2f — skipped",
                ticker, volume_ratio, self.volume_spike_threshold,
            )
            return None

        # ── Filter: minimum price move ────────────────────────────────────────
        if abs(change_pct) < self.min_price_change_pct:
            logger.debug(
                "%s: Price change %.2f%% below minimum %.2f%% — skipped",
                ticker, change_pct, self.min_price_change_pct,
            )
            return None

        notes: list[str] = []
        signal_type: Optional[str] = None

        # ── BREAKOUT LONG: close > N-day high ─────────────────────────────────
        if today_close > resistance and change_pct > 0:
            signal_type = "BREAKOUT_LONG"
            notes.append(f"Close {today_close:.2f} broke above {self.lookback_days}d high {resistance:.2f}")
            notes.append(f"Volume {volume_ratio:.1f}× average — confirmed surge")

        # ── BREAKOUT SHORT: close < N-day low ─────────────────────────────────
        elif today_close < support and change_pct < 0:
            signal_type = "BREAKOUT_SHORT"
            notes.append(f"Close {today_close:.2f} broke below {self.lookback_days}d low {support:.2f}")
            notes.append(f"Volume {volume_ratio:.1f}× average — confirmed break")

        if signal_type is None:
            logger.debug("%s: No breakout pattern detected", ticker)
            return None

        logger.info("Signal detected → %s [%s]", ticker, signal_type)

        return Signal(
            ticker=ticker,
            signal_type=signal_type,
            close=round(today_close, 2),
            high=round(today_high, 2),
            low=round(today_low, 2),
            volume=int(today_vol),
            avg_volume=round(avg_volume, 0),
            volume_ratio=round(volume_ratio, 2),
            change_pct=round(change_pct, 2),
            resistance_level=round(resistance, 2),
            support_level=round(support, 2),
            atr=round(atr, 2),
            notes=notes,
        )

    # ── Public API ────────────────────────────────────────────────────────────

    def scan(self, tickers: list[str] = NIFTY50_TICKERS) -> list[Signal]:
        """
        Scan a list of tickers and return all detected signals.

        Args:
            tickers: List of Yahoo Finance tickers to scan.
        Returns:
            List of Signal objects (may be empty if no setups found).
        """
        logger.info("Starting scan for %d tickers …", len(tickers))
        all_data = self.fetcher.get_multiple_stocks(tickers)
        signals: list[Signal] = []

        for ticker, df in all_data.items():
            signal = self._analyse_stock(ticker, df)
            if signal:
                signals.append(signal)

        # Sort by volume ratio descending — strongest moves first
        signals.sort(key=lambda s: s.volume_ratio, reverse=True)
        logger.info("Scan complete — %d signal(s) detected", len(signals))
        return signals

    def scan_intraday(
        self,
        tickers: list[str] = NIFTY50_TICKERS,
        interval: str = "15m",
    ) -> list[Signal]:
        """
        Run an intraday scan using shorter bar intervals (e.g. 5m, 15m).

        The fetcher is temporarily reconfigured to the requested interval.
        Uses a shorter period ('5d') as intraday data availability is limited.
        """
        original_period = self.fetcher.period
        original_interval = self.fetcher.interval

        self.fetcher.period = "5d"
        self.fetcher.interval = interval

        try:
            return self.scan(tickers)
        finally:
            # Always restore original settings
            self.fetcher.period = original_period
            self.fetcher.interval = original_interval
