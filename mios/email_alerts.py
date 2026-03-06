"""
email_alerts.py - Gmail Email Alert System for MIOS

Formats and sends trade alerts as HTML emails via SMTP.
No third-party libraries required — uses Python's built-in smtplib and email
modules, so the dependency footprint stays minimal.

Setup
-----
Set the following environment variables (or add to .env):
  EMAIL_ADDRESS    — sender Gmail address, e.g. alerts@gmail.com
  EMAIL_PASSWORD   — Gmail App Password (not your account password)
  EMAIL_RECEIVER   — recipient address, e.g. trader@example.com
  SMTP_SERVER      — SMTP host (default: smtp.gmail.com)
  SMTP_PORT        — SMTP port (default: 587, uses STARTTLS)

Gmail setup
-----------
1. Enable 2-Step Verification on your Google account.
2. Generate an App Password at https://myaccount.google.com/apppasswords
3. Use the App Password as EMAIL_PASSWORD.

Alert types
-----------
  send_trade_alert()    — Single high-probability trade setup.
  send_bulk_alerts()    — Multiple trade alerts in one session.
  send_market_report()  — Morning / midday / closing market summary.
  send_error_alert()    — Notify on unexpected system errors.
  send_startup_message()— Confirmation when MIOS boots.
"""

import logging
import os
import smtplib
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import TYPE_CHECKING, Optional

from mios.risk_manager import TradeSetup

if TYPE_CHECKING:
    from mios.earnings_analyzer import EarningsSignal

logger = logging.getLogger(__name__)


class GmailAlerter:
    """
    Sends formatted HTML trade-alert emails via SMTP (Gmail by default).

    All public methods return True on success, False on failure.  Errors are
    logged but never raised so an SMTP outage cannot crash the scanner.
    """

    def __init__(
        self,
        email_address: Optional[str] = None,
        email_password: Optional[str] = None,
        email_receiver: Optional[str] = None,
        smtp_server: Optional[str] = None,
        smtp_port: Optional[int] = None,
        min_quality_score: int = 70,
    ):
        """
        Args:
            email_address    : Sender address (falls back to EMAIL_ADDRESS).
            email_password   : SMTP / App Password (falls back to EMAIL_PASSWORD).
            email_receiver   : Recipient address (falls back to EMAIL_RECEIVER).
            smtp_server      : SMTP host (falls back to SMTP_SERVER, default smtp.gmail.com).
            smtp_port        : SMTP port (falls back to SMTP_PORT, default 587).
            min_quality_score: Setups below this score are not alerted.
        """
        self.email_address = email_address or os.getenv("EMAIL_ADDRESS", "")
        self.email_password = email_password or os.getenv("EMAIL_PASSWORD", "")
        self.email_receiver = email_receiver or os.getenv("EMAIL_RECEIVER", "")
        self.smtp_server = smtp_server or os.getenv("SMTP_SERVER", "smtp.gmail.com")
        self.smtp_port = smtp_port or int(os.getenv("SMTP_PORT", "587"))
        self.min_quality_score = min_quality_score

        if not all([self.email_address, self.email_password, self.email_receiver]):
            logger.warning(
                "Email credentials not fully set — alerts will be printed to stdout only."
            )

    # ── Low-level send ────────────────────────────────────────────────────────

    def _send(self, subject: str, html_body: str) -> bool:
        """
        Internal helper: send an HTML email via SMTP with STARTTLS.

        Falls back to stdout if credentials are missing.
        Returns True on success.
        """
        if not all([self.email_address, self.email_password, self.email_receiver]):
            print("\n" + "=" * 60)
            print(f"SUBJECT: {subject}")
            print(html_body)
            print("=" * 60 + "\n")
            return True

        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"] = self.email_address
        msg["To"] = self.email_receiver
        msg.attach(MIMEText(html_body, "html"))

        try:
            with smtplib.SMTP(self.smtp_server, self.smtp_port, timeout=10) as server:
                server.ehlo()
                server.starttls()
                server.login(self.email_address, self.email_password)
                server.sendmail(self.email_address, self.email_receiver, msg.as_string())
            logger.debug("Email sent to %s | %s", self.email_receiver, subject)
            return True
        except Exception as exc:
            logger.error("Failed to send email alert: %s", exc)
            return False

    # ── HTML helpers ──────────────────────────────────────────────────────────

    @staticmethod
    def _html_wrap(title: str, content: str) -> str:
        """Wrap content in a minimal, readable HTML email template."""
        return f"""<!DOCTYPE html>
<html>
<head>
  <meta charset="UTF-8">
  <style>
    body {{ font-family: Arial, sans-serif; background: #f4f4f4; margin: 0; padding: 20px; }}
    .card {{ background: #ffffff; border-radius: 8px; padding: 24px;
             max-width: 560px; margin: 0 auto; box-shadow: 0 2px 8px rgba(0,0,0,0.1); }}
    h2 {{ margin-top: 0; color: #1a1a2e; }}
    table {{ width: 100%; border-collapse: collapse; margin: 16px 0; }}
    td {{ padding: 8px 12px; border-bottom: 1px solid #e8e8e8; }}
    td:first-child {{ color: #555; width: 40%; }}
    td:last-child {{ font-weight: bold; color: #1a1a2e; }}
    .badge-long {{ background: #d4edda; color: #155724; padding: 3px 10px;
                   border-radius: 12px; font-size: 13px; }}
    .badge-short {{ background: #f8d7da; color: #721c24; padding: 3px 10px;
                    border-radius: 12px; font-size: 13px; }}
    .section-title {{ font-size: 13px; font-weight: bold; color: #888;
                      text-transform: uppercase; letter-spacing: 1px;
                      margin: 20px 0 4px; }}
    ul {{ margin: 4px 0 0 0; padding-left: 20px; color: #333; }}
    .footer {{ font-size: 11px; color: #aaa; margin-top: 20px; border-top: 1px solid #eee;
               padding-top: 12px; }}
  </style>
</head>
<body>
  <div class="card">
    <h2>{title}</h2>
    {content}
    <div class="footer">
      MIOS signals are for educational purposes only.
      Always do your own research before trading.
      &nbsp;&middot;&nbsp; Generated {datetime.now().strftime("%Y-%m-%d %H:%M IST")}
    </div>
  </div>
</body>
</html>"""

    # ── Message formatters ────────────────────────────────────────────────────

    def _format_trade_alert(self, setup: TradeSetup) -> tuple[str, str]:
        """
        Build a trade alert email.

        Returns:
            (subject, html_body)
        """
        sig = setup.signal
        is_long = "LONG" in setup.signal_type
        direction_label = "BREAKOUT LONG" if is_long else "BREAKOUT SHORT"
        badge_class = "badge-long" if is_long else "badge-short"

        sl_pct = ((setup.stop_loss - setup.entry) / setup.entry) * 100
        t1_pct = ((setup.target1 - setup.entry) / setup.entry) * 100
        t2_pct = ((setup.target2 - setup.entry) / setup.entry) * 100

        # Signal confidence as percentage label
        confidence = setup.quality_score

        # Key risk factors from scanner notes
        risk_factors = sig.notes if sig.notes else ["No specific risk factors noted"]
        risk_items = "".join(f"<li>{note}</li>" for note in risk_factors)

        content = f"""
    <p><span class="{badge_class}">{direction_label}</span></p>

    <div class="section-title">Trade Details</div>
    <table>
      <tr><td>Stock</td>          <td>{sig.ticker}</td></tr>
      <tr><td>Entry</td>          <td>&#8377;{setup.entry:,.2f}</td></tr>
      <tr><td>Stop Loss</td>      <td>&#8377;{setup.stop_loss:,.2f}&nbsp;
                                      ({sl_pct:+.1f}%)</td></tr>
      <tr><td>Target 1</td>       <td>&#8377;{setup.target1:,.2f}&nbsp;
                                      ({t1_pct:+.1f}%)</td></tr>
      <tr><td>Target 2</td>       <td>&#8377;{setup.target2:,.2f}&nbsp;
                                      ({t2_pct:+.1f}%)</td></tr>
      <tr><td>Risk Reward</td>    <td>1 : {setup.rr_ratio_t2:.1f}
                                      (T1&nbsp;1:{setup.rr_ratio_t1:.1f})</td></tr>
      <tr><td>Signal Confidence</td><td>{confidence} / 100</td></tr>
    </table>

    <div class="section-title">Key Risk Factors</div>
    <ul>{risk_items}</ul>
"""
        subject = f"MIOS Alert | {sig.ticker} — {direction_label} | Score {confidence}/100"
        return subject, self._html_wrap(f"MIOS Trade Alert — {sig.ticker}", content)

    def _format_market_report(
        self,
        report_type: str,
        market_summary: dict,
        gainers: list[dict],
        losers: list[dict],
        num_signals: int = 0,
    ) -> tuple[str, str]:
        """Format a periodic market report. Returns (subject, html_body)."""
        mood = market_summary.get("market_mood", "Unknown")
        nifty_last = market_summary.get("nifty_last")
        nifty_pct = market_summary.get("nifty_pct")
        vix = market_summary.get("vix")

        nifty_line = (
            f"&#8377;{nifty_last:,.2f} ({nifty_pct:+.2f}%)"
            if nifty_last and nifty_pct is not None
            else "N/A"
        )
        vix_line = f"{vix:.2f}" if vix else "N/A"

        def _movers_rows(items: list[dict], positive: bool) -> str:
            if not items:
                return "<tr><td colspan='3'>No data</td></tr>"
            color = "#155724" if positive else "#721c24"
            return "".join(
                f"<tr>"
                f"<td>{m['ticker']}</td>"
                f"<td style='color:{color};font-weight:bold'>{m['change_pct']:+.2f}%</td>"
                f"<td>&#8377;{m['close']:,.2f}</td>"
                f"</tr>"
                for m in items
            )

        content = f"""
    <div class="section-title">Market Overview</div>
    <table>
      <tr><td>Market Mood</td><td>{mood}</td></tr>
      <tr><td>Nifty 50</td>  <td>{nifty_line}</td></tr>
      <tr><td>India VIX</td> <td>{vix_line}</td></tr>
      <tr><td>Signals Found</td><td>{num_signals}</td></tr>
    </table>

    <div class="section-title">Top Gainers</div>
    <table>
      <tr style="color:#888;font-size:12px">
        <td>Ticker</td><td>Change</td><td>Close</td>
      </tr>
      {_movers_rows(gainers, True)}
    </table>

    <div class="section-title">Top Losers</div>
    <table>
      <tr style="color:#888;font-size:12px">
        <td>Ticker</td><td>Change</td><td>Close</td>
      </tr>
      {_movers_rows(losers, False)}
    </table>
"""
        subject = f"MIOS {report_type} Report | Mood: {mood} | {datetime.now().strftime('%Y-%m-%d')}"
        return subject, self._html_wrap(f"MIOS {report_type} Report", content)

    # ── Public send methods ───────────────────────────────────────────────────

    def send_trade_alert(self, setup: TradeSetup) -> bool:
        """
        Send a trade alert email for a single setup.
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

        subject, body = self._format_trade_alert(setup)
        return self._send(subject, body)

    def send_bulk_alerts(self, setups: list[TradeSetup]) -> int:
        """
        Send alert emails for multiple setups.
        Returns the count of successfully sent messages.
        """
        eligible = [
            s for s in setups
            if s.is_valid and s.quality_score >= self.min_quality_score
        ]

        if not eligible:
            self._send(
                "MIOS — No High-Probability Setups Found",
                self._html_wrap(
                    "MIOS Scan Complete",
                    "<p>No high-probability trade setups were found this session.</p>",
                ),
            )
            return 0

        sent = 0
        for setup in eligible:
            if self.send_trade_alert(setup):
                sent += 1

        logger.info("Sent %d/%d eligible alert emails", sent, len(eligible))
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
        Send a market summary report email.

        Args:
            report_type    : Label, e.g. 'Pre-Market', 'Midday', 'Closing'.
            market_summary : Dict from DataFetcher.get_market_summary().
            gainers        : Top gainers list from DataFetcher.get_top_gainers_losers().
            losers         : Top losers list.
            num_signals    : Total signals detected in this session.
        """
        subject, body = self._format_market_report(
            report_type, market_summary, gainers, losers, num_signals
        )
        return self._send(subject, body)

    def send_error_alert(self, error_msg: str) -> bool:
        """Send a system error notification email."""
        subject = "MIOS System Error"
        body = self._html_wrap(
            "MIOS System Error",
            f"<p style='color:#721c24;'><strong>An error occurred:</strong></p>"
            f"<pre style='background:#f8d7da;padding:12px;border-radius:4px'>{error_msg}</pre>",
        )
        return self._send(subject, body)

    def send_earnings_alerts(self, signals: "list[EarningsSignal]") -> bool:
        """
        Send a single email summarising all upcoming earnings momentum setups.

        Each signal block shows: result date, entry, SL, targets, probability,
        R:R ratio, and the historical reasoning bullets.

        Args:
            signals: EarningsSignal list from EarningsAnalyzer.scan().
        Returns:
            True if the email was sent (or printed) successfully.
        """
        if not signals:
            return False

        def _signal_block(sig: "EarningsSignal") -> str:
            sl_pct = ((sig.stop_loss - sig.entry) / sig.entry) * 100
            t1_pct = ((sig.target1 - sig.entry) / sig.entry) * 100
            t2_pct = ((sig.target2 - sig.entry) / sig.entry) * 100
            note_items = "".join(f"<li>{n}</li>" for n in sig.notes)
            return f"""
    <div style="margin-bottom:20px;padding:16px;background:#f8f9fa;border-radius:6px;
                border-left:4px solid #2196F3;">
      <strong style="font-size:15px;color:#1a1a2e;">{sig.ticker}</strong>
      &nbsp;<span style="background:#d0e8ff;color:#0d47a1;padding:2px 8px;
                         border-radius:10px;font-size:12px;">EARNINGS MOMENTUM</span>
      <table style="width:100%;border-collapse:collapse;margin:10px 0;">
        <tr><td style="color:#555;padding:4px 0;width:45%">Result Date</td>
            <td style="font-weight:bold;color:#1a1a2e;">{sig.next_result_date}
                &nbsp;({sig.days_to_result}d away)</td></tr>
        <tr><td style="color:#555;padding:4px 0">Entry</td>
            <td style="font-weight:bold;">&#8377;{sig.entry:,.2f}</td></tr>
        <tr><td style="color:#555;padding:4px 0">Stop Loss</td>
            <td style="font-weight:bold;color:#721c24;">&#8377;{sig.stop_loss:,.2f}
                &nbsp;({sl_pct:+.1f}%)</td></tr>
        <tr><td style="color:#555;padding:4px 0">Target 1</td>
            <td style="font-weight:bold;color:#155724;">&#8377;{sig.target1:,.2f}
                &nbsp;({t1_pct:+.1f}%)</td></tr>
        <tr><td style="color:#555;padding:4px 0">Target 2</td>
            <td style="font-weight:bold;color:#155724;">&#8377;{sig.target2:,.2f}
                &nbsp;({t2_pct:+.1f}%)</td></tr>
        <tr><td style="color:#555;padding:4px 0">Risk : Reward</td>
            <td style="font-weight:bold;">1 : {sig.rr_ratio:.1f}</td></tr>
        <tr><td style="color:#555;padding:4px 0">Historical Probability</td>
            <td style="font-weight:bold;">{int(sig.historical_probability * 100)}%
                &nbsp;({sig.quarters_analyzed} quarters)</td></tr>
        <tr><td style="color:#555;padding:4px 0">Signal Score</td>
            <td style="font-weight:bold;">{sig.quality_score} / 100</td></tr>
      </table>
      <ul style="margin:4px 0;padding-left:18px;color:#333;font-size:13px;">
        {note_items}
      </ul>
    </div>"""

        blocks = "".join(_signal_block(s) for s in signals)
        count = len(signals)
        content = f"""
    <p style="color:#555;">{count} stock{'s' if count != 1 else ''} with
    upcoming results and high historical rally probability:</p>
    {blocks}"""

        subject = (
            f"MIOS Earnings Momentum | {count} Setup{'s' if count != 1 else ''} "
            f"| {datetime.now().strftime('%Y-%m-%d')}"
        )
        return self._send(subject, self._html_wrap("MIOS Earnings Momentum Setups", content))

    def send_startup_message(self) -> bool:
        """Send a startup confirmation email when MIOS boots."""
        subject = "MIOS 2.0 — Started"
        body = self._html_wrap(
            "MIOS 2.0 — Market Intelligence &amp; Opportunity System",
            "<p>MIOS is now active and monitoring markets.</p>"
            "<p><strong>Scheduled scans:</strong> 08:45 | 09:20 | 12:30 | 15:35 IST</p>",
        )
        return self._send(subject, body)
