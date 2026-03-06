"""
alerts.py - WhatsApp Alert System for MIOS

Formats and sends trade alerts to a WhatsApp number via the Twilio
Messaging API.  No third-party Twilio SDK is required — we use plain
`requests` HTTP calls so the dependency footprint stays minimal.

Setup
-----
1. Sign up at https://www.twilio.com and create a project.
2. Enable the WhatsApp sandbox (or use a verified WhatsApp Business number).
3. Set the four env vars below in the .env file:
   TWILIO_ACCOUNT_SID   — from the Twilio Console dashboard
   TWILIO_AUTH_TOKEN    — from the Twilio Console dashboard
   WHATSAPP_FROM        — your Twilio WhatsApp number, e.g. +14155238886
   WHATSAPP_TO          — the recipient number, e.g. +919876543210

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
from requests.auth import HTTPBasicAuth

from mios.risk_manager import TradeSetup
from mios.data_fetcher import DataFetcher

logger = logging.getLogger(__name__)

# ── Twilio API endpoint ───────────────────────────────────────────────────────
TWILIO_API = "https://api.twilio.com/2010-04-01/Accounts/{account_sid}/Messages.json"

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


class WhatsAppAlerter:
    """
    Sends formatted messages to a WhatsApp number via the Twilio API.

    All methods return True on success, False on failure (errors are logged
    but never raised, so a Twilio outage cannot crash the scanner).

    WhatsApp supports *bold*, _italic_, and `monospace` natively, so the
    existing Markdown-style formatting renders correctly in the app.
    """

    def __init__(
        self,
        account_sid: Optional[str] = None,
        auth_token: Optional[str] = None,
        from_number: Optional[str] = None,
        to_number: Optional[str] = None,
        min_quality_score: int = 70,
    ):
        """
        Args:
            account_sid      : Twilio Account SID (falls back to TWILIO_ACCOUNT_SID).
            auth_token       : Twilio Auth Token (falls back to TWILIO_AUTH_TOKEN).
            from_number      : WhatsApp-enabled Twilio number, e.g. +14155238886
                               (falls back to WHATSAPP_FROM).
            to_number        : Recipient WhatsApp number, e.g. +919876543210
                               (falls back to WHATSAPP_TO).
            min_quality_score: Setups below this score are not alerted.
        """
        self.account_sid = account_sid or os.getenv("TWILIO_ACCOUNT_SID", "")
        self.auth_token = auth_token or os.getenv("TWILIO_AUTH_TOKEN", "")
        self.from_number = from_number or os.getenv("WHATSAPP_FROM", "")
        self.to_number = to_number or os.getenv("WHATSAPP_TO", "")
        self.min_quality_score = min_quality_score

        if not all([self.account_sid, self.auth_token, self.from_number, self.to_number]):
            logger.warning(
                "WhatsApp/Twilio credentials not set — alerts will be printed to stdout only."
            )

    # ── Low-level send ────────────────────────────────────────────────────────

    def _send(self, text: str) -> bool:
        """
        Internal helper: POST a message via the Twilio Messages API.

        Returns True if the request succeeds (HTTP 201 Created).
        """
        if not all([self.account_sid, self.auth_token, self.from_number, self.to_number]):
            # Fallback: print to stdout so we can still see alerts locally
            print("\n" + "=" * 60)
            print(text)
            print("=" * 60 + "\n")
            return True

        url = TWILIO_API.format(account_sid=self.account_sid)
        payload = {
            "From": f"whatsapp:{self.from_number}",
            "To": f"whatsapp:{self.to_number}",
            "Body": text,
        }

        try:
            resp = requests.post(
                url,
                data=payload,
                auth=HTTPBasicAuth(self.account_sid, self.auth_token),
                timeout=10,
            )
            resp.raise_for_status()
            logger.debug("WhatsApp message sent (to=%s)", self.to_number)
            return True
        except requests.RequestException as exc:
            logger.error("Failed to send WhatsApp alert: %s", exc)
            return False

    # ── Message formatters ────────────────────────────────────────────────────

    def _format_trade_alert(self, setup: TradeSetup) -> str:
        """
        Build a rich trade alert message.

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
