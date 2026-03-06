"""
data_fetcher.py - Market Data Fetcher for MIOS

Responsible for fetching:
  - Nifty 50 index data
  - India VIX (volatility index)
  - Top gainers and losers from NSE/BSE
  - Individual stock OHLCV (Open, High, Low, Close, Volume) data

Data source: yfinance (Yahoo Finance) which covers NSE (.NS) and BSE (.BO) tickers.
"""

import logging
from datetime import datetime, timedelta
from typing import Optional

import pandas as pd
import yfinance as yf

logger = logging.getLogger(__name__)

# ── Index symbols on Yahoo Finance ──────────────────────────────────────────
NIFTY_50_SYMBOL = "^NSEI"          # Nifty 50 index
INDIA_VIX_SYMBOL = "^INDIAVIX"     # India VIX (market fear gauge)
SENSEX_SYMBOL = "^BSESN"           # BSE Sensex

# ── Nifty 50 constituent tickers (NSE suffix ".NS") ─────────────────────────
NIFTY50_TICKERS = [
    "RELIANCE.NS", "TCS.NS", "HDFCBANK.NS", "INFY.NS", "ICICIBANK.NS",
    "HINDUNILVR.NS", "ITC.NS", "SBIN.NS", "BHARTIARTL.NS", "KOTAKBANK.NS",
    "LT.NS", "AXISBANK.NS", "ASIANPAINT.NS", "MARUTI.NS", "SUNPHARMA.NS",
    "TITAN.NS", "BAJFINANCE.NS", "ULTRACEMCO.NS", "NESTLEIND.NS", "WIPRO.NS",
    "POWERGRID.NS", "NTPC.NS", "TECHM.NS", "HCLTECH.NS", "ONGC.NS",
    "JSWSTEEL.NS", "TATAPOWER.NS", "TATASTEEL.NS", "ADANIENT.NS", "ADANIPORTS.NS",
    "BPCL.NS", "COALINDIA.NS", "DIVISLAB.NS", "DRREDDY.NS", "EICHERMOT.NS",
    "GRASIM.NS", "HDFCLIFE.NS", "HEROMOTOCO.NS", "HINDALCO.NS", "INDUSINDBK.NS",
    "CIPLA.NS", "M&M.NS", "SBILIFE.NS", "SHRIRAMFIN.NS", "BAJAJFINSV.NS",
    "BAJAJ-AUTO.NS", "APOLLOHOSP.NS", "BRITANNIA.NS", "TATACONSUM.NS", "UPL.NS",
]


class DataFetcher:
    """
    Fetches real-time and historical market data for Indian stocks.

    All price data is sourced via yfinance which proxies NSE/BSE data
    through Yahoo Finance. Tickers follow the convention:
      NSE stocks  →  SYMBOL.NS  (e.g. RELIANCE.NS)
      BSE stocks  →  SYMBOL.BO  (e.g. 500325.BO)
    """

    def __init__(self, period: str = "5d", interval: str = "1d"):
        """
        Args:
            period:   Look-back window for historical data (default 5 trading days).
            interval: OHLCV bar size — '1m', '5m', '15m', '1h', '1d', etc.
        """
        self.period = period
        self.interval = interval

    # ── Index helpers ────────────────────────────────────────────────────────

    def get_index_data(self, symbol: str) -> Optional[pd.DataFrame]:
        """
        Download OHLCV bars for an index symbol.

        Returns a DataFrame with columns [Open, High, Low, Close, Volume]
        indexed by datetime, or None on failure.
        """
        try:
            ticker = yf.Ticker(symbol)
            df = ticker.history(period=self.period, interval=self.interval)
            if df.empty:
                logger.warning("No data returned for index %s", symbol)
                return None
            logger.info("Fetched %d bars for %s", len(df), symbol)
            return df
        except Exception as exc:
            logger.error("Failed to fetch index data for %s: %s", symbol, exc)
            return None

    def get_nifty50(self) -> Optional[pd.DataFrame]:
        """Return Nifty 50 OHLCV data."""
        return self.get_index_data(NIFTY_50_SYMBOL)

    def get_india_vix(self) -> Optional[float]:
        """
        Return the latest India VIX reading (closing value of the most
        recent available bar).  Higher VIX → higher implied volatility.
        """
        df = self.get_index_data(INDIA_VIX_SYMBOL)
        if df is None or df.empty:
            return None
        latest_vix = float(df["Close"].iloc[-1])
        logger.info("India VIX: %.2f", latest_vix)
        return latest_vix

    def get_sensex(self) -> Optional[pd.DataFrame]:
        """Return BSE Sensex OHLCV data."""
        return self.get_index_data(SENSEX_SYMBOL)

    # ── Stock helpers ────────────────────────────────────────────────────────

    def get_stock_data(self, ticker: str) -> Optional[pd.DataFrame]:
        """
        Download OHLCV bars for a single stock ticker.

        Args:
            ticker: Yahoo Finance ticker, e.g. 'RELIANCE.NS'.
        Returns:
            DataFrame with OHLCV columns or None on failure.
        """
        try:
            t = yf.Ticker(ticker)
            df = t.history(period=self.period, interval=self.interval)
            if df.empty:
                logger.debug("No data for ticker %s", ticker)
                return None
            return df
        except Exception as exc:
            logger.error("Error fetching %s: %s", ticker, exc)
            return None

    def get_multiple_stocks(self, tickers: list[str]) -> dict[str, pd.DataFrame]:
        """
        Bulk-download OHLCV data for a list of tickers via a single API call.

        Returns a dict  {ticker: DataFrame}.  Tickers with no data are excluded.
        """
        try:
            # yf.download returns a multi-level DataFrame when multiple tickers
            # are requested; we split it back into per-ticker DataFrames.
            raw = yf.download(
                tickers,
                period=self.period,
                interval=self.interval,
                group_by="ticker",
                auto_adjust=True,
                threads=True,
                progress=False,
            )

            result: dict[str, pd.DataFrame] = {}

            if len(tickers) == 1:
                # Single-ticker download doesn't add a ticker level
                df = raw.dropna(how="all")
                if not df.empty:
                    result[tickers[0]] = df
            else:
                for ticker in tickers:
                    try:
                        df = raw[ticker].dropna(how="all")
                        if not df.empty:
                            result[ticker] = df
                    except KeyError:
                        logger.debug("Ticker %s not found in bulk download", ticker)

            logger.info("Bulk-fetched data for %d/%d tickers", len(result), len(tickers))
            return result

        except Exception as exc:
            logger.error("Bulk download failed: %s", exc)
            return {}

    # ── Market summary ───────────────────────────────────────────────────────

    def get_market_summary(self) -> dict:
        """
        Build a snapshot dict with:
          - nifty_last  : latest Nifty 50 close
          - nifty_change: point change vs previous close
          - nifty_pct   : percentage change
          - vix         : India VIX value
          - market_mood : 'Bullish' / 'Bearish' / 'Neutral' (heuristic)
        """
        summary: dict = {}

        # Nifty 50
        nifty_df = self.get_nifty50()
        if nifty_df is not None and len(nifty_df) >= 2:
            prev_close = float(nifty_df["Close"].iloc[-2])
            last_close = float(nifty_df["Close"].iloc[-1])
            change = last_close - prev_close
            pct = (change / prev_close) * 100
            summary.update(
                nifty_last=round(last_close, 2),
                nifty_change=round(change, 2),
                nifty_pct=round(pct, 2),
            )
        else:
            summary.update(nifty_last=None, nifty_change=None, nifty_pct=None)

        # India VIX
        summary["vix"] = self.get_india_vix()

        # Simple mood heuristic
        pct = summary.get("nifty_pct")
        vix = summary.get("vix")
        if pct is not None and vix is not None:
            if pct > 0.5 and vix < 20:
                summary["market_mood"] = "Bullish"
            elif pct < -0.5 or vix > 25:
                summary["market_mood"] = "Bearish"
            else:
                summary["market_mood"] = "Neutral"
        else:
            summary["market_mood"] = "Unknown"

        return summary

    # ── Gainers / Losers ─────────────────────────────────────────────────────

    def get_top_gainers_losers(
        self,
        tickers: list[str] = NIFTY50_TICKERS,
        top_n: int = 5,
    ) -> dict[str, list[dict]]:
        """
        Compute daily price change for each ticker and return the top N
        gainers and top N losers.

        Args:
            tickers: List of Yahoo Finance tickers to evaluate.
            top_n:   Number of gainers/losers to return.

        Returns:
            {
              'gainers': [{'ticker': ..., 'change_pct': ..., 'close': ...}, ...],
              'losers':  [{'ticker': ..., 'change_pct': ..., 'close': ...}, ...],
            }
        """
        stocks = self.get_multiple_stocks(tickers)
        performance: list[dict] = []

        for ticker, df in stocks.items():
            if len(df) < 2:
                continue
            prev_close = float(df["Close"].iloc[-2])
            last_close = float(df["Close"].iloc[-1])
            if prev_close == 0:
                continue
            change_pct = ((last_close - prev_close) / prev_close) * 100
            performance.append(
                {
                    "ticker": ticker,
                    "close": round(last_close, 2),
                    "change_pct": round(change_pct, 2),
                }
            )

        # Sort descending for gainers, ascending for losers
        performance.sort(key=lambda x: x["change_pct"], reverse=True)
        gainers = performance[:top_n]
        losers = performance[-top_n:][::-1]  # worst performers first

        logger.info(
            "Top %d gainers: %s | Top %d losers: %s",
            top_n,
            [g["ticker"] for g in gainers],
            top_n,
            [l["ticker"] for l in losers],
        )

        return {"gainers": gainers, "losers": losers}
