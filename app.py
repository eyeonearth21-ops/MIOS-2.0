"""
app.py — MIOS 2.0 Streamlit Web App

Run locally:   streamlit run app.py
Deploy:        See Dockerfile.web and deploy-webapp-gcp.sh
"""

import os
import streamlit as st
import pandas as pd
from dotenv import load_dotenv

load_dotenv()

from mios.data_fetcher import NIFTY50_TICKERS, DataFetcher
from mios.scanner import Scanner
from mios.risk_manager import RiskManager, TradeSetup

# ── Page config ───────────────────────────────────────────────────────────────
st.set_page_config(
    page_title="MIOS 2.0 — Stock Scanner",
    page_icon="📈",
    layout="wide",
)

# ── Sidebar — controls ────────────────────────────────────────────────────────
with st.sidebar:
    st.title("📈 MIOS 2.0")
    st.caption("Market Intelligence & Opportunity System")
    st.divider()

    # Stock selection
    st.subheader("Select Stocks")
    select_all = st.checkbox("Select all Nifty 50", value=True)

    # Clean display names (strip .NS suffix)
    name_map = {t: t.replace(".NS", "").replace(".BO", "") for t in NIFTY50_TICKERS}

    if select_all:
        selected_tickers = NIFTY50_TICKERS
        st.caption(f"{len(NIFTY50_TICKERS)} stocks selected")
    else:
        display_names = list(name_map.values())
        chosen = st.multiselect(
            "Choose stocks",
            options=display_names,
            default=display_names[:10],
        )
        # Map back to full ticker symbols
        reverse_map = {v: k for k, v in name_map.items()}
        selected_tickers = [reverse_map[n] for n in chosen if n in reverse_map]

    st.divider()

    # Scanner parameters
    st.subheader("Scanner Settings")
    lookback = st.slider("Lookback days", 5, 60, 20)
    vol_spike = st.slider("Volume spike ×", 1.0, 5.0, 1.5, step=0.1)
    min_chg = st.slider("Min price change %", 0.1, 5.0, 0.5, step=0.1)

    st.divider()

    # Risk parameters
    st.subheader("Risk Settings")
    sl_atr = st.slider("SL ATR multiplier", 0.5, 3.0, 1.5, step=0.1)
    max_sl = st.slider("Max SL %", 1, 10, 3)
    min_score = st.slider("Min quality score", 0, 100, 70)

    st.divider()
    run_btn = st.button("🔍 Run Scan", type="primary", use_container_width=True)

# ── Main area ─────────────────────────────────────────────────────────────────
st.title("MIOS 2.0 — Breakout Scanner")

# Market snapshot at the top
col1, col2, col3 = st.columns(3)

@st.cache_data(ttl=300)   # cache for 5 min
def fetch_market_summary():
    fetcher = DataFetcher(period="2d", interval="1d")
    return fetcher.get_market_summary()

try:
    summary = fetch_market_summary()
    col1.metric("Nifty 50", f"₹{summary.get('nifty_close', 'N/A'):,.2f}",
                f"{summary.get('nifty_change_pct', 0):+.2f}%")
    col2.metric("India VIX", f"{summary.get('vix_close', 'N/A'):.2f}",
                f"{summary.get('vix_change_pct', 0):+.2f}%")
    col3.metric("Sensex", f"₹{summary.get('sensex_close', 'N/A'):,.2f}",
                f"{summary.get('sensex_change_pct', 0):+.2f}%")
except Exception:
    col1.metric("Nifty 50", "—")
    col2.metric("India VIX", "—")
    col3.metric("Sensex", "—")

st.divider()

# ── Scan results ──────────────────────────────────────────────────────────────
if run_btn:
    if not selected_tickers:
        st.warning("Select at least one stock to scan.")
        st.stop()

    with st.spinner(f"Scanning {len(selected_tickers)} stocks …"):
        scanner = Scanner(
            lookback_days=lookback,
            volume_spike_threshold=vol_spike,
            min_price_change_pct=min_chg,
        )
        risk_mgr = RiskManager(
            sl_atr_mult=sl_atr,
            max_sl_pct=max_sl / 100,
            t2_rr_multiple=2.0,
        )

        signals = scanner.scan(selected_tickers)
        approved, rejected = risk_mgr.evaluate_all(signals)

    # ── Summary metrics ───────────────────────────────────────────────────────
    m1, m2, m3, m4 = st.columns(4)
    m1.metric("Stocks scanned", len(selected_tickers))
    m2.metric("Signals found", len(signals))
    m3.metric("Approved setups", len(approved))
    m4.metric("Rejected (RR)", len(rejected))

    st.divider()

    # ── Approved setups table ─────────────────────────────────────────────────
    if approved:
        st.subheader(f"✅ Approved Setups ({len(approved)})")

        rows = []
        for setup in approved:
            sig = setup.signal
            direction = "🟢 LONG" if sig.signal_type == "BREAKOUT_LONG" else "🔴 SHORT"
            rows.append({
                "Stock":        sig.ticker.replace(".NS", ""),
                "Direction":    direction,
                "Close ₹":      sig.close,
                "Entry ₹":      setup.entry,
                "Stop Loss ₹":  setup.stop_loss,
                "Target 1 ₹":   setup.target1,
                "Target 2 ₹":   setup.target2,
                "RR (T2)":      f"1:{setup.rr_ratio_t2:.1f}",
                "Vol ×avg":     f"{sig.volume_ratio:.1f}×",
                "Chg %":        f"{sig.change_pct:+.2f}%",
                "Quality":      setup.quality_score,
            })

        df = pd.DataFrame(rows)
        st.dataframe(
            df,
            use_container_width=True,
            hide_index=True,
            column_config={
                "Quality": st.column_config.ProgressColumn(
                    "Quality", min_value=0, max_value=100, format="%d"
                ),
            },
        )

        # Detail expander for each setup
        st.subheader("Setup Details")
        for setup in approved:
            sig = setup.signal
            label = f"{sig.ticker.replace('.NS','')}  —  {'LONG 🟢' if sig.signal_type == 'BREAKOUT_LONG' else 'SHORT 🔴'}"
            with st.expander(label):
                c1, c2, c3 = st.columns(3)
                c1.metric("Entry",      f"₹{setup.entry:.2f}")
                c1.metric("Stop Loss",  f"₹{setup.stop_loss:.2f}")
                c2.metric("Target 1",   f"₹{setup.target1:.2f}")
                c2.metric("Target 2",   f"₹{setup.target2:.2f}")
                c3.metric("RR at T2",   f"1:{setup.rr_ratio_t2:.1f}")
                c3.metric("Quality",    f"{setup.quality_score}/100")
                st.caption(" · ".join(sig.notes))
    else:
        st.info("No setups passed the risk filter for this scan. Try lowering the Min quality score or volume spike threshold.")

    # ── Rejected signals (collapsed) ──────────────────────────────────────────
    if rejected:
        with st.expander(f"Rejected signals ({len(rejected)}) — failed RR filter"):
            rej_rows = []
            for setup in rejected:
                sig = setup.signal
                rej_rows.append({
                    "Stock":   sig.ticker.replace(".NS", ""),
                    "Type":    sig.signal_type,
                    "Close ₹": sig.close,
                    "RR":      f"1:{setup.rr_ratio_t2:.1f}",
                    "Reason":  setup.rejection_reason or "RR too low",
                })
            st.dataframe(pd.DataFrame(rej_rows), use_container_width=True, hide_index=True)

elif not run_btn:
    st.info("Configure your stock selection and parameters in the sidebar, then click **Run Scan**.")
