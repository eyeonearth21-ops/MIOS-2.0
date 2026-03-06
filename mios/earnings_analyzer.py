"""
earnings_analyzer.py - Earnings Momentum Analyzer for MIOS

Identifies Nifty 50 stocks with upcoming quarterly results and computes
historical post-result rally probabilities using yfinance data.

Signal generation criteria (MIOS spec, Section 7 — Earnings Momentum):
  - Upcoming result within DAYS_AHEAD calendar days
  - Historical post-result rally (>RALLY_PCT% within DAYS_AFTER sessions)
    probability >= MIN_PROBABILITY (default 65%)
  - Minimum R:R >= 1:2 at Target 2

Signal anatomy
--------------
  Entry   = current close (accumulate before result)
  SL      = Entry − (ATR × SL_ATR_MULT), hard-capped at MAX_SL_PCT of entry
  Target1 = Entry + risk × 1.0  (conservative partial exit, 1:1 R:R)
  Target2 = max(Entry + risk × 2.0, historical avg winning rally)

Data source
-----------
  yfinance — earnings_dates index for scheduled result windows,
             history() for post-result price analysis.
"""

import logging
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Optional

import pandas as pd
import yfinance as yf

logger = logging.getLogger(__name__)

# ── Module-level defaults ─────────────────────────────────────────────────────
DAYS_AHEAD: int = 7            # Calendar days forward to check for results
MIN_PROBABILITY: float = 0.65  # 65% historical frequency required
RALLY_PCT: float = 2.0         # % gain threshold to count as a "post-result rally"
DAYS_AFTER: int = 3            # Trading sessions after result day to measure move
MIN_QUARTERS: int = 6          # Minimum quarters needed for reliable stats
ATR_PERIOD: int = 14
SL_ATR_MULT: float = 1.5
MAX_SL_PCT: float = 0.03       # 3% hard cap on stop distance
T2_RR_MULT: float = 2.0        # Minimum 1:2 R:R at Target 2


# ── EarningsSignal dataclass ──────────────────────────────────────────────────

@dataclass
class EarningsSignal:
    """
    A pre-earnings setup validated against historical post-result behaviour.

    Attributes:
        ticker               : Yahoo Finance ticker (e.g. 'HDFCBANK.NS').
        next_result_date     : Expected result date (ISO string YYYY-MM-DD).
        days_to_result       : Calendar days from today until result.
        close                : Latest closing price.
        atr                  : Average True Range used for risk sizing.
        historical_probability: Fraction of past quarters with post-result rally.
        quarters_analyzed    : Number of past quarters used in calculation.
        avg_post_rally_pct   : Average % gain on winning post-result windows.
        entry                : Recommended entry price (equals close).
        stop_loss            : ATR-based stop loss.
        target1              : First target — 1:1 R:R.
        target2              : Final target — max(2× risk, avg historical rally).
        risk_points          : entry − stop_loss.
        rr_ratio             : Reward:Risk ratio at Target 2.
        quality_score        : 0–100 composite score.
        signal_type          : Always 'EARNINGS_MOMENTUM'.
        notes                : Human-readable reasoning bullets.
    """
    ticker: str
    next_result_date: str
    days_to_result: int
    close: float
    atr: float
    historical_probability: float
    quarters_analyzed: int
    avg_post_rally_pct: float
    entry: float
    stop_loss: float
    target1: float
    target2: float
    risk_points: float
    rr_ratio: float
    quality_score: int
    signal_type: str = "EARNINGS_MOMENTUM"
    notes: list[str] = field(default_factory=list)


# ── EarningsAnalyzer class ────────────────────────────────────────────────────

class EarningsAnalyzer:
    """
    Scans a watchlist for stocks with upcoming quarterly results and
    validates each against historical post-result price behaviour.

    Usage::

        from mios.data_fetcher import NIFTY50_TICKERS
        analyzer = EarningsAnalyzer()
        signals = analyzer.scan(NIFTY50_TICKERS)
    """

    def __init__(
        self,
        days_ahead: int = DAYS_AHEAD,
        min_probability: float = MIN_PROBABILITY,
        rally_pct: float = RALLY_PCT,
        days_after: int = DAYS_AFTER,
        min_quarters: int = MIN_QUARTERS,
        sl_atr_mult: float = SL_ATR_MULT,
        max_sl_pct: float = MAX_SL_PCT,
        history_period: str = "5y",
    ):
        self.days_ahead = days_ahead
        self.min_probability = min_probability
        self.rally_pct = rally_pct
        self.days_after = days_after
        self.min_quarters = min_quarters
        self.sl_atr_mult = sl_atr_mult
        self.max_sl_pct = max_sl_pct
        self.history_period = history_period

    # ── Earnings date detection ───────────────────────────────────────────────

    def _get_upcoming_result_date(self, ticker: str) -> Optional[date]:
        """
        Return the next scheduled result date if it falls within
        self.days_ahead calendar days, otherwise None.

        Tries two yfinance sources:
          1. ticker.calendar  — contains the formally announced next date.
          2. ticker.earnings_dates — forward-looking dates from estimates.
        """
        today = date.today()
        horizon = today + timedelta(days=self.days_ahead)

        try:
            t = yf.Ticker(ticker)

            # Source 1: calendar
            cal = t.calendar
            if cal is not None:
                ed = self._extract_date_from_calendar(cal)
                if ed is not None and today <= ed <= horizon:
                    return ed

            # Source 2: earnings_dates index (includes estimated future dates)
            e_dates = t.earnings_dates
            if e_dates is not None and not e_dates.empty:
                future = [d.date() for d in e_dates.index if d.date() >= today]
                if future:
                    closest = min(future)
                    if closest <= horizon:
                        return closest

        except Exception as exc:
            logger.debug("%s: earnings date lookup failed — %s", ticker, exc)

        return None

    @staticmethod
    def _extract_date_from_calendar(cal) -> Optional[date]:
        """Parse the Earnings Date field from a yfinance calendar object."""
        try:
            if isinstance(cal, dict):
                val = cal.get("Earnings Date")
                if val is None:
                    return None
                if isinstance(val, (list, pd.DatetimeIndex)) and len(val):
                    val = val[0]
                return pd.Timestamp(val).date()

            if isinstance(cal, pd.DataFrame) and "Earnings Date" in cal.index:
                val = cal.loc["Earnings Date"].iloc[0]
                return pd.Timestamp(val).date()

        except Exception:
            pass
        return None

    # ── ATR ───────────────────────────────────────────────────────────────────

    def _compute_atr(self, df: pd.DataFrame) -> float:
        """Compute ATR_PERIOD-day Average True Range."""
        high = df["High"]
        low = df["Low"]
        prev_close = df["Close"].shift(1)
        tr = pd.concat(
            [(high - low), (high - prev_close).abs(), (low - prev_close).abs()],
            axis=1,
        ).max(axis=1)
        atr = tr.rolling(ATR_PERIOD).mean().iloc[-1]
        return float(atr) if pd.notna(atr) else float(high.iloc[-1] - low.iloc[-1])

    # ── Historical pattern analysis ───────────────────────────────────────────

    def _compute_historical_stats(
        self, ticker: str, hist: pd.DataFrame
    ) -> tuple[float, int, float]:
        """
        Measure post-result rally frequency over past earnings dates.

        For each past earnings date:
          - Record the closing price on the result day (entry_close).
          - Check the maximum close in the next DAYS_AFTER sessions.
          - Count as a WIN if max_close >= entry_close × (1 + RALLY_PCT / 100).

        Returns:
            (probability, quarters_analyzed, avg_post_rally_pct_on_wins)
        """
        try:
            e_dates = yf.Ticker(ticker).earnings_dates
            if e_dates is None or e_dates.empty:
                return 0.0, 0, 0.0

            today = date.today()
            past_dates = sorted(
                [d.date() for d in e_dates.index if d.date() < today],
                reverse=True,
            )

            if len(past_dates) < self.min_quarters:
                logger.debug(
                    "%s: only %d past earnings dates (need %d)",
                    ticker, len(past_dates), self.min_quarters,
                )
                return 0.0, 0, 0.0

            hist_idx = pd.to_datetime(hist.index).normalize()
            wins, total = 0, 0
            rally_pcts: list[float] = []

            for ed in past_dates:
                ed_ts = pd.Timestamp(ed)
                positions = (hist_idx >= ed_ts).to_numpy().nonzero()[0]
                if not len(positions):
                    continue
                pos = positions[0]
                if pos + self.days_after >= len(hist):
                    continue

                entry_close = float(hist["Close"].iloc[pos])
                post_closes = hist["Close"].iloc[pos + 1: pos + 1 + self.days_after]
                pct_gain = (float(post_closes.max()) / entry_close - 1) * 100

                total += 1
                if pct_gain >= self.rally_pct:
                    wins += 1
                    rally_pcts.append(pct_gain)

            if total < self.min_quarters:
                return 0.0, 0, 0.0

            probability = wins / total
            avg_rally = round(sum(rally_pcts) / len(rally_pcts), 2) if rally_pcts else 0.0
            return round(probability, 3), total, avg_rally

        except Exception as exc:
            logger.debug("%s: historical stats failed — %s", ticker, exc)
            return 0.0, 0, 0.0

    # ── Signal construction ───────────────────────────────────────────────────

    def _build_signal(
        self,
        ticker: str,
        result_date: date,
        hist: pd.DataFrame,
        probability: float,
        quarters: int,
        avg_rally_pct: float,
    ) -> EarningsSignal:
        """Compute trade levels and assemble an EarningsSignal."""
        today = date.today()
        days_to = (result_date - today).days

        close = round(float(hist["Close"].iloc[-1]), 2)
        atr = self._compute_atr(hist)

        # Stop loss — ATR-based with hard cap
        sl_dist = min(atr * self.sl_atr_mult, close * self.max_sl_pct)
        stop_loss = round(close - sl_dist, 2)
        risk = round(close - stop_loss, 2)

        target1 = round(close + risk, 2)                          # 1:1 R:R

        # Target 2 — maximum of 2× risk and historical avg rally
        hist_target = round(close * (1 + avg_rally_pct / 100), 2) if avg_rally_pct > 0 else 0.0
        target2 = max(round(close + risk * T2_RR_MULT, 2), hist_target)

        rr = round((target2 - close) / risk, 2) if risk > 0 else 0.0

        # Quality score (0–100):
        #   probability contribution: 65%→25pts, 75%→50pts (scaled from 60% baseline)
        #   urgency: closer result = higher score (max 20 pts)
        #   depth: more historical quarters = higher confidence (max 30 pts)
        prob_score = min(50, max(0, int((probability - 0.60) * 500)))
        urgency_score = max(0, min(20, int((self.days_ahead - days_to + 1) * 20 / self.days_ahead)))
        depth_score = min(30, quarters * 2)
        quality = min(100, prob_score + urgency_score + depth_score)

        notes = [
            f"Results due {result_date.strftime('%d %b %Y')} ({days_to} day{'s' if days_to != 1 else ''} away)",
            f"Post-result rally (>{self.rally_pct:.0f}%) in {int(probability * 100)}% of last {quarters} quarters",
        ]
        if avg_rally_pct > 0:
            notes.append(f"Average winning post-result move: +{avg_rally_pct:.1f}%")

        return EarningsSignal(
            ticker=ticker,
            next_result_date=result_date.isoformat(),
            days_to_result=days_to,
            close=close,
            atr=round(atr, 2),
            historical_probability=probability,
            quarters_analyzed=quarters,
            avg_post_rally_pct=avg_rally_pct,
            entry=close,
            stop_loss=stop_loss,
            target1=target1,
            target2=target2,
            risk_points=risk,
            rr_ratio=rr,
            quality_score=quality,
            notes=notes,
        )

    # ── Public API ────────────────────────────────────────────────────────────

    def scan(
        self,
        tickers: list[str],
        min_probability: Optional[float] = None,
    ) -> list[EarningsSignal]:
        """
        Scan tickers for upcoming earnings with high historical rally probability.

        For each ticker:
          1. Check for a result within self.days_ahead days.
          2. Download 5 years of OHLCV.
          3. Compute historical post-result stats.
          4. Reject if probability < threshold or R:R < 1:2.
          5. Build and return the EarningsSignal.

        Args:
            tickers         : Yahoo Finance tickers to scan.
            min_probability : Override the instance-level probability threshold.

        Returns:
            EarningsSignal list sorted by historical_probability descending.
        """
        threshold = min_probability if min_probability is not None else self.min_probability
        signals: list[EarningsSignal] = []

        logger.info(
            "Earnings scan: %d tickers | results in next %d days | min prob %.0f%%",
            len(tickers), self.days_ahead, threshold * 100,
        )

        for ticker in tickers:
            # Step 1 — upcoming result date
            result_date = self._get_upcoming_result_date(ticker)
            if result_date is None:
                continue
            logger.info("%s: result expected on %s", ticker, result_date)

            # Step 2 — historical OHLCV
            try:
                hist = yf.Ticker(ticker).history(period=self.history_period)
                if hist.empty or len(hist) < 60:
                    logger.debug("%s: insufficient history (%d bars)", ticker, len(hist))
                    continue
            except Exception as exc:
                logger.debug("%s: history fetch failed — %s", ticker, exc)
                continue

            # Step 3 — historical post-result stats
            probability, quarters, avg_rally = self._compute_historical_stats(ticker, hist)
            if quarters < self.min_quarters:
                logger.debug("%s: not enough historical quarters (%d)", ticker, quarters)
                continue

            # Step 4 — probability filter
            if probability < threshold:
                logger.info(
                    "%s: probability %.0f%% below %.0f%% threshold — skipped",
                    ticker, probability * 100, threshold * 100,
                )
                continue

            # Step 5 — build signal
            signal = self._build_signal(ticker, result_date, hist, probability, quarters, avg_rally)

            # Minimum 1:2 R:R at Target 2
            if signal.rr_ratio < T2_RR_MULT:
                logger.info(
                    "%s: R:R 1:%.1f below minimum 1:%.0f — skipped",
                    ticker, signal.rr_ratio, T2_RR_MULT,
                )
                continue

            signals.append(signal)
            logger.info(
                "EARNINGS SIGNAL ▶ %s | Prob=%d%% | Entry=%.2f | SL=%.2f | T2=%.2f | RR=1:%.1f",
                ticker, int(probability * 100), signal.entry,
                signal.stop_loss, signal.target2, signal.rr_ratio,
            )

        signals.sort(key=lambda s: s.historical_probability, reverse=True)
        logger.info("Earnings scan complete — %d signal(s) found", len(signals))
        return signals
