"""
main.py - MIOS Entry Point

Wires all components together and starts the scheduler.

Component flow
──────────────
DataFetcher ──► Scanner ──► [Signal, ...]
                               │
                          RiskManager
                               │
                      [TradeSetup (valid/rejected)]
                               │
                         WhatsAppAlerter
                               │
                      WhatsApp number / stdout

Scheduled jobs
──────────────
  08:45  pre_market_job()    — index snapshot + VIX + top movers
  09:20  opening_scan_job()  — full breakout scan post-open
  12:30  midday_scan_job()   — mid-session opportunity check
  15:35  closing_report_job()— end-of-day summary + top gainers/losers

CLI usage
─────────
  # Start the scheduled daemon
  python -m mios.main

  # Run a specific job immediately (useful for testing without waiting)
  python -m mios.main --run pre_market
  python -m mios.main --run opening_scan
  python -m mios.main --run midday_scan
  python -m mios.main --run closing_report

  # Run all four jobs once and exit (smoke test)
  python -m mios.main --test
"""

import argparse
import logging
import os
import sys
from dotenv import load_dotenv

# ── Load .env before anything else so env vars are available ─────────────────
load_dotenv()

from mios.data_fetcher import DataFetcher, NIFTY50_TICKERS
from mios.scanner import Scanner
from mios.risk_manager import RiskManager
from mios.alerts import WhatsAppAlerter
from mios.scheduler import MIOSScheduler

# ── Logging configuration ────────────────────────────────────────────────────
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()

logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s  %(levelname)-8s  %(name)s — %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.StreamHandler(sys.stdout),
    ],
)
logger = logging.getLogger("mios.main")


# ── Global component instances ────────────────────────────────────────────────
# These are shared across all scheduled jobs to avoid re-creating objects.

fetcher = DataFetcher()
scanner = Scanner(
    lookback_days=int(os.getenv("SCANNER_LOOKBACK_DAYS", 20)),
    volume_spike_threshold=float(os.getenv("SCANNER_VOLUME_SPIKE", 1.5)),
    min_price_change_pct=float(os.getenv("SCANNER_MIN_PRICE_CHG", 0.5)),
)
risk_manager = RiskManager(
    sl_atr_mult=float(os.getenv("RISK_SL_ATR_MULT", 1.5)),
    max_sl_pct=float(os.getenv("RISK_MAX_SL_PCT", 0.03)),
    t2_rr_multiple=float(os.getenv("RISK_T2_RR_MULTIPLE", 2.0)),
)
alerter = WhatsAppAlerter(
    min_quality_score=int(os.getenv("ALERT_MIN_SCORE", 70)),
)


# ── Shared helper: full scan pipeline ────────────────────────────────────────

def _run_scan_pipeline(session_label: str) -> int:
    """
    Execute the complete scan → evaluate → alert pipeline.

    Returns the number of valid setups found.
    """
    logger.info("─── %s: Starting scan pipeline ───", session_label)

    # 1. Run the scanner to detect breakout signals
    signals = scanner.scan(NIFTY50_TICKERS)

    if not signals:
        logger.info("%s: No signals detected — nothing to evaluate.", session_label)
        alerter._send(f"🔍 *MIOS {session_label}* — Scan complete. No signals this session.")
        return 0

    # 2. Evaluate each signal through the risk manager
    approved, rejected = risk_manager.evaluate_all(signals)
    logger.info(
        "%s: %d signal(s) → %d approved, %d rejected (RR filter)",
        session_label, len(signals), len(approved), len(rejected),
    )

    # 3. Send Telegram alerts for approved setups
    alerter.send_bulk_alerts(approved)

    return len(approved)


# ── Individual scheduled jobs ─────────────────────────────────────────────────

def pre_market_job() -> None:
    """
    08:45 IST — Pre-market report.

    Sends a market mood snapshot before the opening bell:
      - Nifty 50 last close and % change
      - India VIX level
      - Overnight top gainers and losers
    """
    logger.info("═══ PRE-MARKET REPORT (08:45) ═══")

    try:
        summary = fetcher.get_market_summary()
        movers = fetcher.get_top_gainers_losers(top_n=5)
        alerter.send_market_report(
            report_type="Pre-Market",
            market_summary=summary,
            gainers=movers["gainers"],
            losers=movers["losers"],
            num_signals=0,
        )
        logger.info("Pre-market report sent successfully.")
    except Exception as exc:
        logger.error("pre_market_job failed: %s", exc, exc_info=True)
        alerter.send_error_alert(f"pre_market_job error: {exc}")


def opening_scan_job() -> None:
    """
    09:20 IST — Opening breakout scan.

    Runs 5 minutes after the NSE/BSE opening bell (09:15) to catch
    stocks already showing strong directional moves with volume confirmation.
    """
    logger.info("═══ OPENING SCAN (09:20) ═══")
    try:
        _run_scan_pipeline("Opening Scan")
    except Exception as exc:
        logger.error("opening_scan_job failed: %s", exc, exc_info=True)
        alerter.send_error_alert(f"opening_scan_job error: {exc}")


def midday_scan_job() -> None:
    """
    12:30 IST — Midday scan.

    Checks for new breakouts that develop after the initial morning
    volatility settles — often produces cleaner, more sustained moves.
    """
    logger.info("═══ MIDDAY SCAN (12:30) ═══")
    try:
        _run_scan_pipeline("Midday Scan")
    except Exception as exc:
        logger.error("midday_scan_job failed: %s", exc, exc_info=True)
        alerter.send_error_alert(f"midday_scan_job error: {exc}")


def closing_report_job() -> None:
    """
    15:35 IST — Closing report (markets close at 15:30).

    Summarises the day:
      - Final Nifty 50 and VIX values
      - Day's top 5 gainers and losers
      - Any late-day signals / setups
    """
    logger.info("═══ CLOSING REPORT (15:35) ═══")
    try:
        summary = fetcher.get_market_summary()
        movers = fetcher.get_top_gainers_losers(top_n=5)

        # Run one final scan to catch any end-of-day breakouts
        signals = scanner.scan(NIFTY50_TICKERS)
        approved, _ = risk_manager.evaluate_all(signals)

        alerter.send_market_report(
            report_type="Closing",
            market_summary=summary,
            gainers=movers["gainers"],
            losers=movers["losers"],
            num_signals=len(approved),
        )

        # Alert any final setups
        if approved:
            alerter.send_bulk_alerts(approved)

        logger.info("Closing report sent successfully.")
    except Exception as exc:
        logger.error("closing_report_job failed: %s", exc, exc_info=True)
        alerter.send_error_alert(f"closing_report_job error: {exc}")


# ── CLI argument parsing ──────────────────────────────────────────────────────

def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="MIOS — Market Intelligence & Opportunity System",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python -m mios.main                   # Start scheduled daemon
  python -m mios.main --run pre_market  # Run pre-market job now
  python -m mios.main --test            # Run all jobs once and exit
        """,
    )
    parser.add_argument(
        "--run",
        choices=["pre_market", "opening_scan", "midday_scan", "closing_report"],
        help="Run a specific job immediately and exit.",
    )
    parser.add_argument(
        "--test",
        action="store_true",
        help="Run all four jobs once in sequence and exit (smoke test).",
    )
    parser.add_argument(
        "--run-missed",
        action="store_true",
        help="On startup, immediately run any jobs whose time has already passed today.",
    )
    return parser.parse_args()


# ── Main entry point ──────────────────────────────────────────────────────────

def main() -> None:
    args = _parse_args()

    logger.info("━━━ MIOS 2.0 — Market Intelligence & Opportunity System ━━━")
    logger.info("Configured tickers : %d (Nifty 50)", len(NIFTY50_TICKERS))
    logger.info(
        "Scanner settings   : lookback=%d days, volume_spike=%.1f×, min_change=%.1f%%",
        scanner.lookback_days,
        scanner.volume_spike_threshold,
        scanner.min_price_change_pct,
    )
    logger.info(
        "Risk settings      : SL mult=%.1f ATR, max SL=%.0f%%, min RR=1:%.0f",
        risk_manager.sl_atr_mult,
        risk_manager.max_sl_pct * 100,
        risk_manager.min_reward_ratio,
    )

    # ── Immediate single-job run ──────────────────────────────────────────────
    if args.run:
        job_map = {
            "pre_market":    pre_market_job,
            "opening_scan":  opening_scan_job,
            "midday_scan":   midday_scan_job,
            "closing_report":closing_report_job,
        }
        logger.info("Running job '%s' immediately …", args.run)
        job_map[args.run]()
        return

    # ── Smoke test: run all four jobs sequentially ────────────────────────────
    if args.test:
        logger.info("Running all jobs in test mode …")
        pre_market_job()
        opening_scan_job()
        midday_scan_job()
        closing_report_job()
        logger.info("Test run complete.")
        return

    # ── Daemon mode: start scheduler ──────────────────────────────────────────
    alerter.send_startup_message()

    scheduler = MIOSScheduler(
        pre_market_fn=pre_market_job,
        opening_scan_fn=opening_scan_job,
        midday_scan_fn=midday_scan_job,
        closing_report_fn=closing_report_job,
        poll_interval_seconds=int(os.getenv("SCHEDULER_POLL_SECONDS", 30)),
    )
    scheduler.start(run_missed=args.run_missed)


if __name__ == "__main__":
    main()
