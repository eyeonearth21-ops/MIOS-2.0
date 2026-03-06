"""
risk_manager.py - Trade Setup Generator & Risk Filter for MIOS

For every raw scanner signal this module:
  1. Calculates Entry, Stop Loss (SL), Target 1, and Target 2 using ATR-based
     or fixed-percentage sizing rules.
  2. Computes the Risk:Reward (RR) ratio.
  3. REJECTS any setup where RR < MIN_REWARD_RATIO (default 1:2).
  4. Returns a TradeSetup dataclass ready for alerting.

Risk rules
----------
BREAKOUT LONG
  Entry   = current close (market order on confirmed breakout)
  SL      = Entry - (ATR × sl_atr_mult)         [floor: Entry × (1 - max_sl_pct)]
  Target1 = Entry + (risk × t1_rr_multiple)      [RR 1:1 as conservative exit]
  Target2 = Entry + (risk × t2_rr_multiple)      [full target, RR ≥ 1:2]

BREAKOUT SHORT (for shorting or puts)
  Entry   = current close
  SL      = Entry + (ATR × sl_atr_mult)
  Target1 = Entry - (risk × t1_rr_multiple)
  Target2 = Entry - (risk × t2_rr_multiple)
"""

import logging
from dataclasses import dataclass
from typing import Optional

from mios.scanner import Signal

logger = logging.getLogger(__name__)

# ── Minimum RR to accept a trade ────────────────────────────────────────────
MIN_REWARD_RATIO: float = 2.0   # 1:2  (reward must be ≥ 2× the risk)


# ── TradeSetup dataclass ─────────────────────────────────────────────────────

@dataclass
class TradeSetup:
    """
    A validated trade setup with full position sizing details.

    Attributes:
        signal        : The originating scanner Signal.
        entry         : Recommended entry price.
        stop_loss     : Stop-loss level (invalidates the trade if breached).
        target1       : First partial profit target.
        target2       : Final profit target (full exit).
        risk_points   : Absolute risk per share (entry − stop_loss for longs).
        reward_t1     : Reward at Target 1 per share.
        reward_t2     : Reward at Target 2 per share.
        rr_ratio_t1   : Risk:Reward at Target 1 (e.g. 1.0 means 1:1).
        rr_ratio_t2   : Risk:Reward at Target 2 (e.g. 2.0 means 1:2).
        quality_score : 0–100 composite score for signal strength.
        is_valid      : False if RR is below the minimum threshold.
        rejection_reason: Populated when is_valid=False.
    """
    signal: Signal
    entry: float
    stop_loss: float
    target1: float
    target2: float
    risk_points: float
    reward_t1: float
    reward_t2: float
    rr_ratio_t1: float
    rr_ratio_t2: float
    quality_score: int
    is_valid: bool = True
    rejection_reason: Optional[str] = None

    # ── Convenience properties ────────────────────────────────────────────────

    @property
    def ticker(self) -> str:
        return self.signal.ticker

    @property
    def signal_type(self) -> str:
        return self.signal.signal_type

    @property
    def rr_label(self) -> str:
        """Human-readable RR label, e.g. '1:2.5'."""
        return f"1:{self.rr_ratio_t2:.1f}"

    def summary(self) -> str:
        """One-line trade summary for logging / quick display."""
        direction = "LONG" if "LONG" in self.signal_type else "SHORT"
        return (
            f"{self.ticker} [{direction}] | "
            f"Entry={self.entry:.2f}  SL={self.stop_loss:.2f}  "
            f"T1={self.target1:.2f}  T2={self.target2:.2f}  "
            f"RR={self.rr_label}  Score={self.quality_score}"
        )


# ── RiskManager class ─────────────────────────────────────────────────────────

class RiskManager:
    """
    Converts raw scanner signals into fully specified, risk-filtered trade setups.
    """

    def __init__(
        self,
        sl_atr_mult: float = 1.5,
        max_sl_pct: float = 0.03,
        t1_rr_multiple: float = 1.0,
        t2_rr_multiple: float = 2.0,
        min_reward_ratio: float = MIN_REWARD_RATIO,
    ):
        """
        Args:
            sl_atr_mult     : ATR multiplier for SL distance (default 1.5 ATR).
            max_sl_pct      : Maximum SL distance as % of entry (hard cap at 3%).
            t1_rr_multiple  : Risk multiple for Target 1 (default 1.0 → 1:1 RR).
            t2_rr_multiple  : Risk multiple for Target 2 (default 2.0 → 1:2 RR).
            min_reward_ratio: Minimum RR at Target 2 to approve the trade.
        """
        self.sl_atr_mult = sl_atr_mult
        self.max_sl_pct = max_sl_pct
        self.t1_rr_multiple = t1_rr_multiple
        self.t2_rr_multiple = t2_rr_multiple
        self.min_reward_ratio = min_reward_ratio

    # ── Quality scoring ───────────────────────────────────────────────────────

    def _quality_score(self, signal: Signal) -> int:
        """
        Assign a 0–100 quality score based on:
          - Volume ratio     (higher volume = stronger conviction)
          - Price change     (larger move = stronger breakout)
          - ATR availability (proper sizing possible)

        Score bands:
          80–100  : High probability — send alert
          60–79   : Medium probability — log only
          <60     : Low probability — discard
        """
        score = 0

        # Volume factor (max 50 pts) — 2× avg → 25 pts, 4× avg → 50 pts
        vol_score = min(50, int(signal.volume_ratio * 12.5))
        score += vol_score

        # Price change factor (max 30 pts) — 1% → 10 pts, 3% → 30 pts
        price_score = min(30, int(abs(signal.change_pct) * 10))
        score += price_score

        # ATR available bonus (max 20 pts)
        if signal.atr and signal.atr > 0:
            score += 20

        return min(100, score)

    # ── Setup calculation ─────────────────────────────────────────────────────

    def _build_long_setup(self, signal: Signal) -> TradeSetup:
        """Build a BREAKOUT_LONG trade setup."""
        entry = signal.close

        # Stop loss: ATR-based, but never wider than max_sl_pct of entry
        atr = signal.atr or (signal.high - signal.low)
        sl_distance = atr * self.sl_atr_mult
        max_sl_distance = entry * self.max_sl_pct
        sl_distance = min(sl_distance, max_sl_distance)   # take the tighter SL
        stop_loss = round(entry - sl_distance, 2)

        # Ensure stop is below today's low (logical floor)
        stop_loss = min(stop_loss, round(signal.low - 0.01, 2))

        risk_points = round(entry - stop_loss, 2)

        # Targets based on risk multiples
        target1 = round(entry + risk_points * self.t1_rr_multiple, 2)
        target2 = round(entry + risk_points * self.t2_rr_multiple, 2)

        reward_t1 = round(target1 - entry, 2)
        reward_t2 = round(target2 - entry, 2)

        rr_t1 = round(reward_t1 / risk_points, 2) if risk_points else 0.0
        rr_t2 = round(reward_t2 / risk_points, 2) if risk_points else 0.0

        quality = self._quality_score(signal)

        # Validate minimum RR at Target 2
        is_valid = rr_t2 >= self.min_reward_ratio
        rejection = None if is_valid else (
            f"RR at T2 = 1:{rr_t2:.1f} is below the minimum 1:{self.min_reward_ratio:.0f}"
        )

        return TradeSetup(
            signal=signal,
            entry=entry,
            stop_loss=stop_loss,
            target1=target1,
            target2=target2,
            risk_points=risk_points,
            reward_t1=reward_t1,
            reward_t2=reward_t2,
            rr_ratio_t1=rr_t1,
            rr_ratio_t2=rr_t2,
            quality_score=quality,
            is_valid=is_valid,
            rejection_reason=rejection,
        )

    def _build_short_setup(self, signal: Signal) -> TradeSetup:
        """Build a BREAKOUT_SHORT trade setup (short selling / put option)."""
        entry = signal.close

        # Stop loss above entry for shorts
        atr = signal.atr or (signal.high - signal.low)
        sl_distance = atr * self.sl_atr_mult
        max_sl_distance = entry * self.max_sl_pct
        sl_distance = min(sl_distance, max_sl_distance)
        stop_loss = round(entry + sl_distance, 2)

        # Ensure stop is above today's high (logical ceiling)
        stop_loss = max(stop_loss, round(signal.high + 0.01, 2))

        risk_points = round(stop_loss - entry, 2)

        target1 = round(entry - risk_points * self.t1_rr_multiple, 2)
        target2 = round(entry - risk_points * self.t2_rr_multiple, 2)

        reward_t1 = round(entry - target1, 2)
        reward_t2 = round(entry - target2, 2)

        rr_t1 = round(reward_t1 / risk_points, 2) if risk_points else 0.0
        rr_t2 = round(reward_t2 / risk_points, 2) if risk_points else 0.0

        quality = self._quality_score(signal)

        is_valid = rr_t2 >= self.min_reward_ratio
        rejection = None if is_valid else (
            f"RR at T2 = 1:{rr_t2:.1f} is below the minimum 1:{self.min_reward_ratio:.0f}"
        )

        return TradeSetup(
            signal=signal,
            entry=entry,
            stop_loss=stop_loss,
            target1=target1,
            target2=target2,
            risk_points=risk_points,
            reward_t1=reward_t1,
            reward_t2=reward_t2,
            rr_ratio_t1=rr_t1,
            rr_ratio_t2=rr_t2,
            quality_score=quality,
            is_valid=is_valid,
            rejection_reason=rejection,
        )

    # ── Public API ────────────────────────────────────────────────────────────

    def evaluate(self, signal: Signal) -> TradeSetup:
        """
        Evaluate a single signal and return a TradeSetup (valid or rejected).
        """
        if "LONG" in signal.signal_type:
            setup = self._build_long_setup(signal)
        else:
            setup = self._build_short_setup(signal)

        if setup.is_valid:
            logger.info("APPROVED  %s", setup.summary())
        else:
            logger.info("REJECTED  %s → %s", setup.ticker, setup.rejection_reason)

        return setup

    def evaluate_all(self, signals: list[Signal]) -> tuple[list[TradeSetup], list[TradeSetup]]:
        """
        Evaluate a list of signals and split into approved / rejected setups.

        Args:
            signals: Raw Signal objects from the Scanner.
        Returns:
            (approved_setups, rejected_setups)
        """
        approved: list[TradeSetup] = []
        rejected: list[TradeSetup] = []

        for signal in signals:
            setup = self.evaluate(signal)
            (approved if setup.is_valid else rejected).append(setup)

        logger.info(
            "Risk evaluation complete — %d approved, %d rejected",
            len(approved), len(rejected),
        )
        return approved, rejected
