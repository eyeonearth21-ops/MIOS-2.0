"""
scheduler.py - Task Scheduler for MIOS

Runs four scheduled jobs every trading day (IST = UTC+5:30):

  08:45 — Pre-market report:  market mood, VIX, overnight events.
  09:20 — Opening scan:       breakout scan shortly after NSE/BSE open at 09:15.
  12:30 — Midday scan:        mid-session opportunities check.
  15:35 — Closing report:     day summary, top gainers/losers, final signals.

The scheduler also checks whether the current date is a trading day
(Monday–Friday, skipping Indian public holidays defined in HOLIDAYS_IST).

Dependencies
------------
  schedule  — lightweight in-process job scheduler
  pytz      — timezone handling for IST
"""

import logging
import time
from datetime import date, datetime
from typing import Callable, Optional

import pytz
import schedule

logger = logging.getLogger(__name__)

# ── IST timezone ──────────────────────────────────────────────────────────────
IST = pytz.timezone("Asia/Kolkata")

# ── Indian public / exchange holidays (NSE / BSE) for current year ───────────
# Update this set each year from the official NSE holiday list.
# Format: date(YYYY, M, D)
HOLIDAYS_IST: set[date] = {
    # 2025 NSE holidays (sample — update annually)
    date(2025, 1, 26),   # Republic Day
    date(2025, 2, 26),   # Mahashivratri
    date(2025, 3, 14),   # Holi
    date(2025, 4, 14),   # Dr. Ambedkar Jayanti / Good Friday
    date(2025, 4, 18),   # Good Friday
    date(2025, 5, 1),    # Maharashtra Day
    date(2025, 8, 15),   # Independence Day
    date(2025, 8, 27),   # Ganesh Chaturthi
    date(2025, 10, 2),   # Gandhi Jayanti
    date(2025, 10, 20),  # Diwali (Laxmi Pujan)
    date(2025, 10, 21),  # Diwali (Balipratipada)
    date(2025, 11, 5),   # Prakash Gurpurb
    date(2025, 12, 25),  # Christmas
    # 2026 holidays (add as released by NSE)
    date(2026, 1, 26),   # Republic Day
}

# ── Scheduled scan times (HH:MM in IST) ──────────────────────────────────────
SCHEDULE_TIMES = {
    "pre_market": "08:45",
    "opening_scan": "09:20",
    "midday_scan": "12:30",
    "closing_report": "15:35",
}


# ── Holiday / trading day helpers ─────────────────────────────────────────────

def is_trading_day(check_date: Optional[date] = None) -> bool:
    """
    Return True if check_date (default = today IST) is a valid NSE/BSE
    trading day (Mon–Fri and not in HOLIDAYS_IST).
    """
    if check_date is None:
        check_date = datetime.now(IST).date()

    # Weekends (Saturday=5, Sunday=6)
    if check_date.weekday() >= 5:
        logger.debug("%s is a weekend — market closed", check_date)
        return False

    if check_date in HOLIDAYS_IST:
        logger.debug("%s is a declared holiday — market closed", check_date)
        return False

    return True


def current_ist_time() -> datetime:
    """Return current datetime in IST."""
    return datetime.now(IST)


# ── Wrapper that guards every job with a trading-day check ────────────────────

def _trading_day_guard(job_name: str, func: Callable) -> Callable:
    """
    Wrap a scheduled callback so it is silently skipped on non-trading days.
    """
    def guarded():
        if not is_trading_day():
            logger.info("Skipping '%s' — not a trading day", job_name)
            return
        logger.info("Running scheduled job: %s", job_name)
        try:
            func()
        except Exception as exc:
            logger.error("Job '%s' raised an exception: %s", job_name, exc, exc_info=True)
    return guarded


# ── MIOSScheduler class ───────────────────────────────────────────────────────

class MIOSScheduler:
    """
    Registers and runs the four MIOS daily jobs using the `schedule` library.

    Usage::

        scheduler = MIOSScheduler(
            pre_market_fn=pre_market_job,
            opening_scan_fn=opening_scan_job,
            midday_scan_fn=midday_scan_job,
            closing_report_fn=closing_report_job,
        )
        scheduler.start()   # blocks forever; Ctrl-C to stop
    """

    def __init__(
        self,
        pre_market_fn: Optional[Callable] = None,
        opening_scan_fn: Optional[Callable] = None,
        midday_scan_fn: Optional[Callable] = None,
        closing_report_fn: Optional[Callable] = None,
        poll_interval_seconds: int = 30,
    ):
        """
        Args:
            pre_market_fn       : Callable for 08:45 pre-market job.
            opening_scan_fn     : Callable for 09:20 opening scan job.
            midday_scan_fn      : Callable for 12:30 midday scan job.
            closing_report_fn   : Callable for 15:35 closing report job.
            poll_interval_seconds: How often the scheduler checks for pending
                                   jobs (default 30 s).
        """
        self.pre_market_fn = pre_market_fn or (lambda: logger.info("pre_market_fn not set"))
        self.opening_scan_fn = opening_scan_fn or (lambda: logger.info("opening_scan_fn not set"))
        self.midday_scan_fn = midday_scan_fn or (lambda: logger.info("midday_scan_fn not set"))
        self.closing_report_fn = closing_report_fn or (lambda: logger.info("closing_report_fn not set"))
        self.poll_interval = poll_interval_seconds

    def _register_jobs(self) -> None:
        """Register all four daily jobs with the `schedule` library."""
        schedule.clear()  # Remove any previously registered jobs

        jobs = [
            ("Pre-Market Report",  SCHEDULE_TIMES["pre_market"],   self.pre_market_fn),
            ("Opening Scan",       SCHEDULE_TIMES["opening_scan"],  self.opening_scan_fn),
            ("Midday Scan",        SCHEDULE_TIMES["midday_scan"],   self.midday_scan_fn),
            ("Closing Report",     SCHEDULE_TIMES["closing_report"],self.closing_report_fn),
        ]

        for name, time_str, fn in jobs:
            guarded = _trading_day_guard(name, fn)
            schedule.every().day.at(time_str).do(guarded)
            logger.info("Scheduled '%s' at %s IST", name, time_str)

    def run_job_now(self, job: str) -> None:
        """
        Manually trigger a specific job immediately (useful for testing).

        Args:
            job: One of 'pre_market', 'opening_scan', 'midday_scan', 'closing_report'.
        """
        job_map = {
            "pre_market":    self.pre_market_fn,
            "opening_scan":  self.opening_scan_fn,
            "midday_scan":   self.midday_scan_fn,
            "closing_report":self.closing_report_fn,
        }
        fn = job_map.get(job)
        if fn is None:
            logger.error("Unknown job '%s'. Valid: %s", job, list(job_map))
            return
        logger.info("Manually running job: %s", job)
        fn()

    def start(self, run_missed: bool = False) -> None:
        """
        Register jobs and enter the blocking run-loop.

        Args:
            run_missed: If True, immediately execute any job whose scheduled
                        time has already passed today (useful for late starts).
        """
        self._register_jobs()

        if run_missed:
            self._run_missed_jobs()

        now_ist = current_ist_time()
        logger.info("MIOS Scheduler active — current IST time: %s", now_ist.strftime("%H:%M:%S"))
        logger.info("Press Ctrl-C to stop.\n")

        try:
            while True:
                schedule.run_pending()
                time.sleep(self.poll_interval)
        except KeyboardInterrupt:
            logger.info("Scheduler stopped by user (KeyboardInterrupt).")

    def _run_missed_jobs(self) -> None:
        """Run any jobs that were scheduled for earlier today but missed."""
        now_str = current_ist_time().strftime("%H:%M")

        missed = [
            (name, t, fn)
            for name, t, fn in [
                ("Pre-Market Report",  SCHEDULE_TIMES["pre_market"],   self.pre_market_fn),
                ("Opening Scan",       SCHEDULE_TIMES["opening_scan"],  self.opening_scan_fn),
                ("Midday Scan",        SCHEDULE_TIMES["midday_scan"],   self.midday_scan_fn),
                ("Closing Report",     SCHEDULE_TIMES["closing_report"],self.closing_report_fn),
            ]
            if t < now_str
        ]

        if missed:
            logger.info("Running %d missed job(s) …", len(missed))
            for name, t, fn in missed:
                logger.info("  → Running missed job '%s' (was scheduled at %s)", name, t)
                try:
                    fn()
                except Exception as exc:
                    logger.error("Missed job '%s' failed: %s", name, exc)
