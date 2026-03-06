"""
alerts.py - Telegram Alert System for MIOS

Formats and sends trade alerts to a Telegram chat / channel via the
Bot API.  No third-party Telegram library is required — we use plain
`requests` HTTP calls so the dependency footprint stays minimal.

Setup
-----
1. Create a bot via @BotFather and copy the token.
2. Add the bot to your channel / group.
3. Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID in the .env file
   (or as environment variables).

Alert types
-----------
  send_market_report()  — Morning / midday / closing market summary.
  send_trade_alert()    — Single high-probability trade setup.
  send_bulk_alerts()    — Multiple trade alerts in one session.
  send_error_alert()    — Notify on unexpected system errors.
"""

import logging
import os
from typing import Optional

import requests

from mios.risk_manager import TradeSetup
from mios.data_fetcher import DataFetcher

logger = logging.getLogger(__name__)

# ── Telegram API base URL ─────────────────────────────────────────────────────
TELEGRAM_API = "https://api.telegram.org/bot{token}/{method}"

# ── Emoji shortcuts for message decoration ────────────────────────────────────
BULL = "🐂"
BEAR = "🐻"
FIRE = "🔥"
WARN = "⚠️"
CHART = "📊"
CLOCK = "🕐"
CHECK = "✅"
CROSS = "❌"
ROCKET = "🚀"
STOP = "🛑"


class TelegramAlerter:
    """
    Sends formatted Markdown messages to a Telegram chat via the Bot API.

    All methods return True on success, False on failure (errors are logged
    but never raised, so a Telegram outage cannot crash the scanner).
    """

    def __init__(
        self,
        bot_token: Optional[str] = None,
        chat_id: Optional[str] = None,
        min_quality_score: int = 70,
    ):
        """
        Args:
            bot_token        : Telegram bot token (falls back to env var
                               TELEGRAM_BOT_TOKEN if not provided).
            chat_id          : Target chat / channel ID (falls back to env var
                               TELEGRAM_CHAT_ID if not provided).
            min_quality_score: Setups below this score are not alerted.
        """
        self.bot_token = bot_token or os.getenv("TELEGRAM_BOT_TOKEN", "")
        self.chat_id = chat_id or os.getenv("TELEGRAM_CHAT_ID", "")
        self.min_quality_score = min_quality_score

        if not self.bot_token or not self.chat_id:
            logger.warning(
                "Telegram credentials not set — alerts will be printed to stdout only."
            )

    # ── Low-level send ────────────────────────────────────────────────────────

    def _send(self, text: str) -> bool:
        """
        Internal helper: POST a message to the Telegram sendMessage endpoint.

        Uses MarkdownV2 parse mode. Returns True if the request succeeds.
        """
        if not self.bot_token or not self.chat_id:
            # Fallback: print to stdout so we can still see alerts locally
            print("\n" + "=" * 60)
            print(text)
            print("=" * 60 + "\n")
            return True

        url = TELEGRAM_API.format(token=self.bot_token, method="sendMessage")
        payload = {
            "chat_id": self.chat_id,
            "text": text,
            "parse_mode": "Markdown",   # simpler than MarkdownV2
            "disable_web_page_preview": True,
        }

        try:
            resp = requests.post(url, json=payload, timeout=10)
            resp.raise_for_status()
            logger.debug("Telegram message sent (chat=%s)", self.chat_id)
            return True
        except requests.RequestException as exc:
            logger.error("Failed to send Telegram alert: %s", exc)
            return False

    # ── Message formatters ────────────────────────────────────────────────────

    def _format_trade_alert(self, setup: TradeSetup) -> str:
        """
        Build a rich Markdown trade alert message.

        Example output:
        ┌─────────────────────────────────────────┐
        │ 🔥 MIOS TRADE ALERT — BREAKOUT LONG     │
        │ Stock   : RELIANCE.NS                   │
        │ Entry   : ₹2,850.00                     │
        │ Stop    : ₹2,800.00  (−1.75%)           │
        │ Target1 : ₹2,900.00  (+1.75%)  RR 1:1  │
        │ Target2 : ₹2,950.00  (+3.49%)  RR 1:2  │
        │ Volume  : 2.8× average                  │
        │ Score   : 85 / 100                      │
        └─────────────────────────────────────────┘
        """
        sig = setup.signal
        direction_emoji = ROCKET if "LONG" in setup.signal_type else BEAR
        direction_label = "BREAKOUT LONG" if "LONG" in setup.signal_type else "BREAKOUT SHORT"

        sl_pct = ((setup.stop_loss - setup.entry) / setup.entry) * 100
        t1_pct = ((setup.target1 - setup.entry) / setup.entry) * 100
        t2_pct = ((setup.target2 - setup.entry) / setup.entry) * 100

        # Notes from scanner (why the signal fired)
        notes_text = "\n".join(f"  • {n}" for n in sig.notes) if sig.notes else "  • N/A"

        msg = (
            f"{FIRE} *MIOS TRADE ALERT — {direction_label}* {direction_emoji}\n"
            f"{'─' * 36}\n"
            f"{CHART} *Stock*    : `{sig.ticker}`\n"
            f"💰 *Entry*    : ₹{setup.entry:,.2f}\n"
            f"{STOP} *Stop Loss* : ₹{setup.stop_loss:,.2f}  ({sl_pct:+.1f}%)\n"
            f"{CHECK} *Target 1* : ₹{setup.target1:,.2f}  ({t1_pct:+.1f}%)  | RR 1:{setup.rr_ratio_t1:.1f}\n"
            f"{ROCKET} *Target 2* : ₹{setup.target2:,.2f}  ({t2_pct:+.1f}%)  | RR 1:{setup.rr_ratio_t2:.1f}\n"
            f"{'─' * 36}\n"
            f"📈 *Change*  : {sig.change_pct:+.2f}%\n"
            f"🔊 *Volume*  : {sig.volume_ratio:.1f}× average\n"
            f"⚡ *Score*   : {setup.quality_score} / 100\n"
            f"{'─' * 36}\n"
            f"📝 *Why it fired:*\n{notes_text}\n"
            f"{'─' * 36}\n"
            f"⚠️ _MIOS signals are for educational purposes only._\n"
            f"_Always do your own research before trading._"
        )
        return msg

    def _format_market_report(
        self,
        report_type: str,
        market_summary: dict,
        gainers: list[dict],
        losers: list[dict],
        num_signals: int = 0,
    ) -> str:
        """Format a periodic market report (pre-market / midday / closing)."""
        mood = market_summary.get("market_mood", "Unknown")
        mood_emoji = BULL if mood == "Bullish" else (BEAR if mood == "Bearish" else CHART)

        nifty_last = market_summary.get("nifty_last")
        nifty_pct = market_summary.get("nifty_pct")
        vix = market_summary.get("vix")

        nifty_line = (
            f"  Nifty 50  : {nifty_last:,.2f}  ({nifty_pct:+.2f}%)"
            if nifty_last and nifty_pct is not None
            else "  Nifty 50  : N/A"
        )
        vix_line = f"  India VIX : {vix:.2f}" if vix else "  India VIX : N/A"

        gainers_text = "\n".join(
            f"  {CHECK} {g['ticker']:<18} {g['change_pct']:+.2f}%  ₹{g['close']:,.2f}"
            for g in gainers
        ) or "  No data"

        losers_text = "\n".join(
            f"  {CROSS} {l['ticker']:<18} {l['change_pct']:+.2f}%  ₹{l['close']:,.2f}"
            for l in losers
        ) or "  No data"

        msg = (
            f"{mood_emoji} *MIOS {report_type.upper()} REPORT* {mood_emoji}\n"
            f"{'─' * 36}\n"
            f"*Market Mood* : {mood}\n"
            f"{nifty_line}\n"
            f"{vix_line}\n"
            f"{'─' * 36}\n"
            f"📈 *Top Gainers:*\n{gainers_text}\n"
            f"{'─' * 36}\n"
            f"📉 *Top Losers:*\n{losers_text}\n"
            f"{'─' * 36}\n"
            f"🔍 *Signals found this session* : {num_signals}\n"
        )
        return msg

    # ── Public send methods ───────────────────────────────────────────────────

    def send_trade_alert(self, setup: TradeSetup) -> bool:
        """
        Send a trade alert for a single setup.
        Skips setups that fall below the minimum quality threshold.
        """
        if setup.quality_score < self.min_quality_score:
            logger.info(
                "Skipping alert for %s — score %d below threshold %d",
                setup.ticker, setup.quality_score, self.min_quality_score,
            )
            return False

        if not setup.is_valid:
            logger.info("Skipping invalid setup for %s", setup.ticker)
            return False

        msg = self._format_trade_alert(setup)
        return self._send(msg)

    def send_bulk_alerts(self, setups: list[TradeSetup]) -> int:
        """
        Send alerts for multiple setups.
        Returns the count of successfully sent messages.
        """
        sent = 0
        # Only alert high-quality, valid setups
        eligible = [
            s for s in setups
            if s.is_valid and s.quality_score >= self.min_quality_score
        ]

        if not eligible:
            self._send(
                f"{WARN} *MIOS* — Scan complete. "
                f"No high-probability setups found this session."
            )
            return 0

        for setup in eligible:
            if self.send_trade_alert(setup):
                sent += 1

        logger.info("Sent %d/%d eligible alerts", sent, len(eligible))
        return sent

    def send_market_report(
        self,
        report_type: str,
        market_summary: dict,
        gainers: list[dict],
        losers: list[dict],
        num_signals: int = 0,
    ) -> bool:
        """
        Send a market summary report.

        Args:
            report_type    : Label, e.g. 'Pre-Market', 'Midday', 'Closing'.
            market_summary : Dict from DataFetcher.get_market_summary().
            gainers        : Top gainers list from DataFetcher.get_top_gainers_losers().
            losers         : Top losers list.
            num_signals    : Total signals detected in this session.
        """
        msg = self._format_market_report(
            report_type, market_summary, gainers, losers, num_signals
        )
        return self._send(msg)

    def send_error_alert(self, error_msg: str) -> bool:
        """Send a system error notification."""
        msg = f"{WARN} *MIOS SYSTEM ERROR*\n\n`{error_msg}`"
        return self._send(msg)

    def send_startup_message(self) -> bool:
        """Send a startup confirmation when MIOS boots."""
        msg = (
            f"{CLOCK} *MIOS 2.0 — Started*\n"
            f"Market Intelligence & Opportunity System is now active.\n"
            f"Scheduled scans: 08:45 | 09:20 | 12:30 | 15:35 IST"
        )
        return self._send(msg)
