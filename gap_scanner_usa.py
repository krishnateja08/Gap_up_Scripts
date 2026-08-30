"""
gap_scanner_usa.py
Single-file US (NYSE/Nasdaq) Daily Gap Up / Gap Down scanner with a
terminal-style dashboard report.

- Scans USA_WATCHLIST (below) using yfinance
- Gap % = ((Today Open - Yesterday Close) / Yesterday Close) * 100
- Gap Up  if gap_pct >= +threshold
- Gap Down if gap_pct <= -threshold
- Writes a styled gap_report_usa.html with:
    1. Market Overview bar (counts, sentiment meter, dual clocks, auto-refresh)
    2. Bullish (Gap Up) panel — sector tag, volume bar, momentum badge
    3. Bearish (Gap Down) panel — sector tag, volume bar, risk badge
    4. Indices & Sector ETFs panel — SPY/QQQ/DIA + the 11 sector SPDRs,
       shown separately
    5. Sector heatmap (avg gap % across ALL scanned stocks per GICS sector)
    6. Gap size distribution bar
- Sends a summary alert to Telegram (same config.json as the India scanner)

Reads credentials/threshold from config.json (same folder as this script).

Usage:
    python gap_scanner_usa.py                # full watchlist
    python gap_scanner_usa.py --sample        # quick 5-ticker test
    python gap_scanner_usa.py --no-telegram   # skip the Telegram alert

NOTE ON SCOPE: yfinance/Yahoo Finance does not provide real US pre-market
data, so this script does NOT fabricate a "pre-market volume" or
"pre-market trend" signal — anything shown here is derived from the daily
Open/Close/Volume series only.
"""

import argparse
import json
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import requests
import yfinance as yf

# Folder this script lives in — used so config.json / gap_report_usa.html are
# found next to the script even if you run it from a different directory.
SCRIPT_DIR = Path(__file__).resolve().parent

# ═══════════════════════════════════════════════════════════════════════════
# SECTOR MAP — key = plain US ticker, value = GICS sector SPDR code
# ═══════════════════════════════════════════════════════════════════════════

SECTOR_MAP = {
    **{s: "XLK" for s in [
        # Technology (17)
        "NVDA", "MSFT", "AAPL", "AVGO", "AMD", "ORCL", "ADBE", "PANW",
        "NOW", "SNPS", "CRM", "CSCO", "INTC", "QCOM", "AMAT", "LRCX",
        # Extras: SMCI
        "SMCI",
    ]},
    **{s: "XLC" for s in [
        # Communication Services (12)
        "GOOGL", "GOOG", "META", "NFLX", "CMCSA", "DIS",
        "TMUS", "VZ", "T", "CHTR", "SPOT", "RBLX",
    ]},
    **{s: "XLY" for s in [
        # Consumer Discretionary (13 — COST moved to Staples)
        "AMZN", "TSLA", "HD", "MCD", "TJX", "BKNG",
        "LOW", "SBUX", "NKE", "MAR", "ROST", "EBAY", "LULU",
    ]},
    **{s: "XLP" for s in [
        # Consumer Staples (10 — COST kept here as primary)
        "WMT", "PG", "KO", "PEP", "COST", "PM", "MO", "MDLZ", "CL", "MNST",
    ]},
    **{s: "XLV" for s in [
        # Health Care (16)
        "LLY", "UNH", "JNJ", "MRK", "ABBV", "TMO", "AMGN", "BMY",
        "GILD", "ISRG", "VRTX", "CVS", "CI", "MDT", "SYK", "REGN",
    ]},
    **{s: "XLF" for s in [
        # Financials (18)
        "JPM", "BAC", "MS", "GS", "V", "MA", "AXP", "BLK",
        "SPGI", "C", "WFC", "SCHW", "COF", "PGR", "CB", "MMC",
        # Extras: HOOD, SOFI
        "HOOD", "SOFI",
    ]},
    **{s: "XLI" for s in [
        # Industrials (15)
        "GE", "CAT", "UNP", "HON", "LMT", "UPS", "RTX", "DE",
        "FDX", "BA", "GEV", "ETN", "ADP", "FAST", "CTAS",
    ]},
    **{s: "XLE" for s in [
        # Energy (12 — NEE, SO, DUK, CEG, VST kept here as listed)
        "XOM", "CVX", "COP", "NEE", "SO", "DUK", "CEG", "VST",
        "SLB", "EOG", "KMI", "PSX",
    ]},
    **{s: "XLB" for s in [
        # Materials (8)
        "LIN", "FCX", "SHW", "NEM", "APD", "ECL", "NUE", "DOW",
    ]},
    **{s: "XLRE" for s in [
        # Real Estate (10)
        "PLD", "AMT", "EQIX", "DLR", "WELL", "SPG", "PSA", "O", "CBRE", "VTR",
    ]},
    **{s: "XLU" for s in [
        # Utilities (7 — SO, DUK, NEE already classified under XLE above)
        "EXC", "XEL", "AEP", "SRE", "D", "PEG", "WEC",
    ]},
}

# The full watchlist of individual stocks, derived from SECTOR_MAP.
USA_WATCHLIST = list(SECTOR_MAP.keys())

SAMPLE_TICKERS = ["AAPL", "MSFT", "AMZN", "GOOGL", "TSLA"]

# The broad-market/sector-ETF tickers themselves (SPY, QQQ, DIA + the 11
# sector SPDRs). These aren't in SECTOR_MAP — that's individual constituent
# stocks — but the dashboard's Indices & Sector ETFs panel needs quotes for
# these tickers too, so they're scanned alongside the watchlist.
INDEX_ETF_SYMBOLS = [
    "SPY", "QQQ", "DIA",
    "XLK", "XLC", "XLY", "XLP", "XLV", "XLF", "XLI", "XLE", "XLB", "XLRE", "XLU",
]

# The 3 broad-market ETFs get category "INDEX"; the 11 sector SPDRs get
# category "ETF". Both are shown together in the Indices & Sector ETFs panel.
BROAD_INDEX_SYMBOLS = {"SPY", "QQQ", "DIA"}
SECTOR_ETF_SYMBOLS = set(INDEX_ETF_SYMBOLS) - BROAD_INDEX_SYMBOLS

# Friendly display names for indices & sector ETFs
ETF_LABELS = {
    "SPY": "S&P 500 ETF",
    "QQQ": "Nasdaq 100 ETF",
    "DIA": "Dow Jones ETF",
    "XLK": "Technology",
    "XLC": "Communication Svcs",
    "XLY": "Consumer Discretionary",
    "XLP": "Consumer Staples",
    "XLV": "Health Care",
    "XLF": "Financials",
    "XLI": "Industrials",
    "XLE": "Energy",
    "XLB": "Materials",
    "XLRE": "Real Estate",
    "XLU": "Utilities",
}


# ═══════════════════════════════════════════════════════════════════════════
# CLASSIFICATION HELPERS
# ═══════════════════════════════════════════════════════════════════════════

def bare_symbol(ticker: str) -> str:
    return ticker


def get_category(ticker: str) -> str:
    """STOCK | ETF | INDEX"""
    if ticker in BROAD_INDEX_SYMBOLS:
        return "INDEX"
    if ticker in SECTOR_ETF_SYMBOLS:
        return "ETF"
    return "STOCK"


def get_sector(ticker: str) -> str:
    if ticker in BROAD_INDEX_SYMBOLS:
        return "INDEX"
    if ticker in SECTOR_ETF_SYMBOLS:
        return ticker  # the sector ETF's own code, e.g. "XLK"
    return SECTOR_MAP.get(ticker, "OTHER")


def get_display_name(ticker: str) -> str:
    return ETF_LABELS.get(ticker, ticker)


def classify_gap(gap_pct: float, threshold: float) -> str:
    if gap_pct >= threshold:
        return "GAP UP"
    if gap_pct <= -threshold:
        return "GAP DOWN"
    return "NEUTRAL"


def classify_momentum(gap_pct: float, volume_ratio: float) -> str:
    """Gap-up strength badge. 'High volume' = ratio >= 1.5x normal."""
    high_volume = volume_ratio >= 1.5
    if gap_pct >= 1.5 and high_volume:
        return "Strong"
    if gap_pct >= 0.8:
        return "Moderate"
    return "Weak"


def classify_risk(gap_pct: float) -> str:
    """Gap-down risk badge, based on the size of the drop."""
    drop = abs(gap_pct)
    if drop > 2.0:
        return "Heavy"
    if drop >= 1.0:
        return "Medium"
    return "Mild"


# ═══════════════════════════════════════════════════════════════════════════
# GAP DETECTION
# ═══════════════════════════════════════════════════════════════════════════

@dataclass
class GapResult:
    ticker: str
    display_name: str
    category: str        # STOCK | ETF | INDEX
    sector: str
    prev_close: float
    today_open: float
    gap_pct: float
    status: str           # GAP UP | GAP DOWN | NEUTRAL
    volume: int = 0
    avg_volume: float = 0.0
    volume_ratio: float = 1.0   # today's volume / recent average volume
    volume_shock: bool = False  # ratio >= 2.0x
    badge: str = ""              # momentum (up) or risk (down) label


def load_config(path: str = "config.json") -> dict:
    """Load config.json. If a bare relative filename is given (the default),
    resolve it next to this script rather than relative to the current
    working directory, so `python /some/path/gap_scanner_usa.py` works from
    anywhere. Uses the same config.json as the India gap_scanner.py — same
    Telegram bot/chat, just add gap_threshold_pct/refresh_interval_sec if
    you want different values for the US scan."""
    config_path = Path(path)
    if not config_path.is_absolute() and not config_path.exists():
        config_path = SCRIPT_DIR / path
    with open(config_path, "r") as f:
        return json.load(f)


def scan_ticker(ticker: str, threshold: float) -> GapResult | None:
    """Fetch recent daily history for a ticker and compute:
    - the gap between the most recent Open and the prior session's Close
    - a volume ratio vs. the average volume of the preceding sessions
    Returns None if there isn't enough data (bad/delisted ticker, holiday, etc.)
    """
    try:
        hist = yf.Ticker(ticker).history(period="10d", interval="1d")
        hist = hist.dropna(subset=["Open", "Close", "Volume"])
        if len(hist) < 2:
            return None

        prev_close = float(hist["Close"].iloc[-2])
        today_open = float(hist["Open"].iloc[-1])
        volume = int(hist["Volume"].iloc[-1])

        if prev_close == 0:
            return None

        # Average volume of prior sessions (excluding today) as a baseline
        prior_volumes = hist["Volume"].iloc[:-1]
        avg_volume = float(prior_volumes.mean()) if len(prior_volumes) else 0.0
        volume_ratio = (volume / avg_volume) if avg_volume > 0 else 1.0
        volume_shock = volume_ratio >= 2.0

        gap_pct = ((today_open - prev_close) / prev_close) * 100
        status = classify_gap(gap_pct, threshold)

        badge = ""
        if status == "GAP UP":
            badge = classify_momentum(gap_pct, volume_ratio)
        elif status == "GAP DOWN":
            badge = classify_risk(gap_pct)

        return GapResult(
            ticker=ticker,
            display_name=get_display_name(ticker),
            category=get_category(ticker),
            sector=get_sector(ticker),
            prev_close=round(prev_close, 2),
            today_open=round(today_open, 2),
            gap_pct=round(gap_pct, 2),
            status=status,
            volume=volume,
            avg_volume=round(avg_volume, 0),
            volume_ratio=round(volume_ratio, 2),
            volume_shock=volume_shock,
            badge=badge,
        )
    except Exception as exc:
        print(f"[WARN] Skipping {ticker}: {exc}")
        return None


def scan_tickers(tickers: list[str], threshold: float, pause: float = 0.0) -> list[GapResult]:
    """Scan every ticker and return ALL results (including NEUTRAL) — needed
    for the sector heatmap, which averages across the whole scan, not just
    the tickers that gapped."""
    results: list[GapResult] = []
    for ticker in tickers:
        result = scan_ticker(ticker, threshold)
        if result:
            results.append(result)
        if pause:
            time.sleep(pause)  # be gentle on the API for large watchlists
    return results


# ═══════════════════════════════════════════════════════════════════════════
# AGGREGATIONS — sector heatmap & gap-size distribution
# ═══════════════════════════════════════════════════════════════════════════

def build_sector_heatmap(stock_results: list[GapResult]) -> list[dict]:
    """Average gap % per sector across ALL scanned stocks (not just gappers)."""
    buckets: dict[str, list[float]] = {}
    for r in stock_results:
        buckets.setdefault(r.sector, []).append(r.gap_pct)

    heatmap = []
    for sector, gaps in buckets.items():
        avg_gap = sum(gaps) / len(gaps)
        heatmap.append({"sector": sector, "avg_gap": round(avg_gap, 2), "count": len(gaps)})

    heatmap.sort(key=lambda x: x["avg_gap"], reverse=True)
    return heatmap


def build_gap_distribution(gappers: list[GapResult]) -> dict:
    """Bucket gappers by size, split into up/down, for a histogram bar."""
    buckets = {
        "0.5–1%": {"up": 0, "down": 0},
        "1–2%":   {"up": 0, "down": 0},
        "2–5%":   {"up": 0, "down": 0},
        "5%+":    {"up": 0, "down": 0},
    }
    for r in gappers:
        mag = abs(r.gap_pct)
        if mag < 1:
            key = "0.5–1%"
        elif mag < 2:
            key = "1–2%"
        elif mag < 5:
            key = "2–5%"
        else:
            key = "5%+"
        buckets[key]["up" if r.status == "GAP UP" else "down"] += 1
    return buckets


# ═══════════════════════════════════════════════════════════════════════════
# HTML REPORT
# ═══════════════════════════════════════════════════════════════════════════

PAGE_HEAD = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>Gap Scanner USA — {report_date}</title>
<style>
  * {{ box-sizing: border-box; }}
  body {{
    font-family: -apple-system, Segoe UI, Roboto, Helvetica, Arial, sans-serif;
    background: #0b0d12;
    color: #e7e9ee;
    margin: 0;
    padding: 28px 16px 60px;
  }}
  .container {{ max-width: 1080px; margin: 0 auto; }}
  h1 {{ font-size: 22px; margin: 0 0 4px 0; }}
  h2 {{ font-size: 15px; margin: 0 0 14px 0; letter-spacing: 0.02em; }}
  .subtitle {{ color: #9aa1ac; font-size: 13px; margin-bottom: 4px; }}
  .clocks {{ color: #6b7280; font-size: 12px; margin-bottom: 22px; }}
  .clocks span {{ font-variant-numeric: tabular-nums; }}

  /* ── Overview bar ── */
  .overview {{
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));
    gap: 12px;
    margin-bottom: 28px;
  }}
  .stat-card {{
    background: #14161c;
    border: 1px solid #21242c;
    border-radius: 10px;
    padding: 14px 16px;
  }}
  .stat-label {{ font-size: 11px; color: #9aa1ac; text-transform: uppercase; letter-spacing: .04em; margin-bottom: 6px; }}
  .stat-value {{ font-size: 22px; font-weight: 700; }}
  .stat-value.up {{ color: #3ecf8e; }}
  .stat-value.down {{ color: #ff6b6b; }}
  .stat-sub {{ font-size: 11px; color: #6b7280; margin-top: 4px; }}
  .sentiment-bar {{ height: 6px; border-radius: 3px; background: #262a33; overflow: hidden; margin-top: 8px; }}
  .sentiment-fill {{ height: 100%; background: linear-gradient(90deg, #3ecf8e, #6ee7b7); }}

  /* ── Section wrapper ── */
  section {{ margin-bottom: 30px; }}
  .section-title {{ display: flex; align-items: center; gap: 8px; }}
  .count-pill {{
    font-size: 11px; padding: 2px 8px; border-radius: 999px; font-weight: 700;
    background: #1a1d24; border: 1px solid #262a33; color: #9aa1ac;
  }}

  /* ── Tables ── */
  table {{
    width: 100%; border-collapse: collapse; background: #14161c;
    border-radius: 10px; overflow: hidden; box-shadow: 0 4px 18px rgba(0,0,0,0.3);
  }}
  thead th {{
    text-align: left; font-size: 11px; text-transform: uppercase;
    letter-spacing: 0.04em; color: #9aa1ac; padding: 10px 14px;
    background: #1a1d24; border-bottom: 1px solid #262a33;
  }}
  tbody td {{ padding: 10px 14px; font-size: 13.5px; border-bottom: 1px solid #1c1f26; vertical-align: middle; }}
  tr.gap-up {{ background: rgba(62, 207, 142, 0.07); }}
  tr.gap-down {{ background: rgba(255, 107, 107, 0.07); }}
  tr:hover {{ filter: brightness(1.15); }}
  .ticker {{ font-weight: 700; }}
  .ticker-sub {{ font-size: 11px; color: #6b7280; font-weight: 400; }}
  .num {{ font-variant-numeric: tabular-nums; }}
  .gap-val {{ font-weight: 700; font-size: 15px; }}
  .gap-val.up {{ color: #3ecf8e; }}
  .gap-val.down {{ color: #ff6b6b; }}

  .tag {{
    display: inline-block; padding: 2px 8px; border-radius: 6px;
    font-size: 10.5px; font-weight: 600; background: #21242c; color: #9aa1ac;
  }}
  .badge {{
    display: inline-block; padding: 3px 10px; border-radius: 999px;
    font-size: 11px; font-weight: 700;
  }}
  .badge.strong {{ background: rgba(62, 207, 142, 0.20); color: #3ecf8e; }}
  .badge.moderate {{ background: rgba(250, 204, 21, 0.18); color: #facc15; }}
  .badge.weak {{ background: rgba(148, 163, 184, 0.18); color: #94a3b8; }}
  .badge.heavy {{ background: rgba(255, 107, 107, 0.20); color: #ff6b6b; }}
  .badge.medium {{ background: rgba(250, 204, 21, 0.18); color: #facc15; }}
  .badge.mild {{ background: rgba(148, 163, 184, 0.18); color: #94a3b8; }}
  .shock {{ color: #facc15; font-size: 11px; font-weight: 700; margin-left: 4px; }}

  .vol-bar-track {{ width: 70px; height: 6px; background: #21242c; border-radius: 3px; overflow: hidden; display: inline-block; vertical-align: middle; margin-right: 6px; }}
  .vol-bar-fill {{ height: 100%; }}
  .vol-bar-fill.up {{ background: #3ecf8e; }}
  .vol-bar-fill.down {{ background: #ff6b6b; }}
  .vol-bar-fill.neutral {{ background: #6b7280; }}

  .empty {{ padding: 30px; text-align: center; color: #9aa1ac; background: #14161c; border-radius: 10px; font-size: 13px; }}

  /* ── Sector heatmap ── */
  .heatmap {{ display: grid; grid-template-columns: repeat(auto-fill, minmax(130px, 1fr)); gap: 10px; }}
  .heat-cell {{ border-radius: 8px; padding: 10px 12px; border: 1px solid #21242c; }}
  .heat-sector {{ font-size: 11px; color: #cbd2db; font-weight: 700; margin-bottom: 4px; }}
  .heat-value {{ font-size: 15px; font-weight: 700; }}
  .heat-count {{ font-size: 10.5px; color: #6b7280; margin-top: 2px; }}

  /* ── Distribution bar ── */
  .dist-row {{ display: flex; align-items: center; gap: 10px; margin-bottom: 8px; font-size: 12px; }}
  .dist-label {{ width: 60px; color: #9aa1ac; flex-shrink: 0; }}
  .dist-track {{ flex: 1; display: flex; height: 16px; border-radius: 4px; overflow: hidden; background: #1a1d24; }}
  .dist-up {{ background: #3ecf8e; }}
  .dist-down {{ background: #ff6b6b; }}
  .dist-count {{ width: 70px; text-align: right; color: #9aa1ac; flex-shrink: 0; }}

  footer {{ margin-top: 10px; font-size: 11.5px; color: #4b5563; text-align: center; }}
</style>
</head>
<body>
<div class="container">
  <h1>📊 Gap Scanner — USA</h1>
  <div class="subtitle">Report generated {report_datetime} · Threshold: ±{threshold}%</div>
  <div class="clocks">🕐 ET: <span id="clock-et">--:--:--</span> &nbsp;|&nbsp; UTC: <span id="clock-utc">--:--:--</span> &nbsp;|&nbsp; Auto-refresh in <span id="refresh-countdown">--</span>s</div>
"""

OVERVIEW_BLOCK = """  <div class="overview">
    <div class="stat-card">
      <div class="stat-label">Scanned</div>
      <div class="stat-value">{scanned_count}</div>
      <div class="stat-sub">stocks + ETFs/indices</div>
    </div>
    <div class="stat-card">
      <div class="stat-label">Gap Up</div>
      <div class="stat-value up">{up_count} ▲</div>
      <div class="stat-sub">{up_pct:.0f}% of gappers</div>
    </div>
    <div class="stat-card">
      <div class="stat-label">Gap Down</div>
      <div class="stat-value down">{down_count} ▼</div>
      <div class="stat-sub">{down_pct:.0f}% of gappers</div>
    </div>
    <div class="stat-card">
      <div class="stat-label">Market Sentiment</div>
      <div class="stat-value" style="color:{sentiment_color}">{sentiment_label}</div>
      <div class="sentiment-bar"><div class="sentiment-fill" style="width:{up_pct:.0f}%"></div></div>
    </div>
  </div>
"""

ROW_STOCK_TEMPLATE = """    <tr class="{row_class}">
      <td>
        <div class="ticker">{ticker}</div>
        <div class="ticker-sub">{sector}</div>
      </td>
      <td class="gap-val {gap_class}">{gap_pct:+.2f}%</td>
      <td class="num">{prev_close}</td>
      <td class="num">{today_open}</td>
      <td>
        <span class="vol-bar-track"><span class="vol-bar-fill {gap_class}" style="width:{vol_bar_width:.0f}%"></span></span>
        <span class="num">{volume_ratio:.1f}x</span>{shock_html}
      </td>
      <td><span class="badge {badge_class}">{badge}</span></td>
    </tr>"""

INDEX_ROW_TEMPLATE = """    <tr class="{row_class}">
      <td><div class="ticker">{display_name}</div><div class="ticker-sub">{ticker_bare}</div></td>
      <td><span class="tag">{category}</span></td>
      <td class="gap-val {gap_class}">{gap_pct:+.2f}%</td>
      <td class="num">{volume_ratio:.1f}x</td>
      <td><span class="tag" style="color:{bias_color}">{bias}</span></td>
    </tr>"""


def volume_bar_width(ratio: float) -> float:
    # cap the visual bar at 3x so one outlier doesn't flatten the rest
    return min(ratio / 3.0, 1.0) * 100


def build_stock_table(results: list[GapResult], direction: str) -> str:
    """direction: 'up' or 'down'"""
    if not results:
        msg = "No gap ups beyond the threshold today." if direction == "up" else "No gap downs beyond the threshold today."
        return f'<div class="empty">{msg}</div>'

    badge_class_map = {"Strong": "strong", "Moderate": "moderate", "Weak": "weak",
                        "Heavy": "heavy", "Medium": "medium", "Mild": "mild"}

    rows = []
    for r in results:
        shock_html = ' <span class="shock">⚡ Vol Shock</span>' if r.volume_shock else ""
        rows.append(ROW_STOCK_TEMPLATE.format(
            row_class="gap-up" if direction == "up" else "gap-down",
            gap_class=direction,
            ticker=r.display_name,
            sector=r.sector,
            gap_pct=r.gap_pct,
            prev_close=f"{r.prev_close:.2f}",
            today_open=f"{r.today_open:.2f}",
            vol_bar_width=volume_bar_width(r.volume_ratio),
            volume_ratio=r.volume_ratio,
            shock_html=shock_html,
            badge_class=badge_class_map.get(r.badge, "weak"),
            badge=r.badge,
        ))

    badge_col = "Momentum" if direction == "up" else "Risk"
    return f"""  <table>
    <thead>
      <tr><th>Ticker</th><th>Gap %</th><th>Prev Close</th><th>Today Open</th><th>Volume vs Avg</th><th>{badge_col}</th></tr>
    </thead>
    <tbody>
{chr(10).join(rows)}
    </tbody>
  </table>"""


def build_index_etf_table(results: list[GapResult]) -> str:
    if not results:
        return '<div class="empty">No index/ETF data available.</div>'

    # sort: broad indices first, then sector ETFs, both by ticker
    results = sorted(results, key=lambda r: (r.category != "INDEX", r.ticker))

    rows = []
    for r in results:
        direction = "up" if r.gap_pct > 0 else ("down" if r.gap_pct < 0 else "neutral")
        row_class = "gap-up" if direction == "up" else ("gap-down" if direction == "down" else "")
        if abs(r.gap_pct) >= 0.8:
            strength = "Strong"
        elif abs(r.gap_pct) >= 0.3:
            strength = "Mild"
        else:
            strength = "Flat"
        bias_word = "Bullish" if direction == "up" else ("Bearish" if direction == "down" else "Neutral")
        bias = f"{strength} {bias_word}" if strength != "Flat" else "Neutral"
        bias_color = "#3ecf8e" if direction == "up" else ("#ff6b6b" if direction == "down" else "#9aa1ac")

        rows.append(INDEX_ROW_TEMPLATE.format(
            row_class=row_class,
            display_name=r.display_name,
            ticker_bare=r.ticker,
            category=r.category,
            gap_class=direction if direction != "neutral" else "",
            gap_pct=r.gap_pct,
            volume_ratio=r.volume_ratio,
            bias=bias,
            bias_color=bias_color,
        ))

    return f"""  <table>
    <thead>
      <tr><th>Name</th><th>Type</th><th>Gap %</th><th>Volume vs Avg</th><th>Market Bias</th></tr>
    </thead>
    <tbody>
{chr(10).join(rows)}
    </tbody>
  </table>"""


def build_sector_heatmap_html(heatmap: list[dict]) -> str:
    if not heatmap:
        return '<div class="empty">Not enough data for a sector heatmap.</div>'

    cells = []
    for h in heatmap:
        avg = h["avg_gap"]
        if avg > 0:
            intensity = min(abs(avg) / 1.5, 1.0)
            bg = f"rgba(62, 207, 142, {0.10 + 0.30 * intensity:.2f})"
            color = "#3ecf8e"
        elif avg < 0:
            intensity = min(abs(avg) / 1.5, 1.0)
            bg = f"rgba(255, 107, 107, {0.10 + 0.30 * intensity:.2f})"
            color = "#ff6b6b"
        else:
            bg = "rgba(148, 163, 184, 0.10)"
            color = "#94a3b8"

        sector_label = ETF_LABELS.get(h["sector"], h["sector"])
        cells.append(f"""    <div class="heat-cell" style="background:{bg}">
      <div class="heat-sector">{sector_label}</div>
      <div class="heat-value" style="color:{color}">{avg:+.2f}%</div>
      <div class="heat-count">{h['count']} stocks</div>
    </div>""")

    return f'  <div class="heatmap">\n{chr(10).join(cells)}\n  </div>'


def build_distribution_html(dist: dict, max_count: int) -> str:
    rows = []
    for label, counts in dist.items():
        up, down = counts["up"], counts["down"]
        total = up + down
        up_w = (up / max_count * 100) if max_count else 0
        down_w = (down / max_count * 100) if max_count else 0
        rows.append(f"""  <div class="dist-row">
    <div class="dist-label">{label}</div>
    <div class="dist-track">
      <div class="dist-up" style="width:{up_w:.0f}%"></div>
      <div class="dist-down" style="width:{down_w:.0f}%"></div>
    </div>
    <div class="dist-count">{up}▲ / {down}▼</div>
  </div>""")
    return "\n".join(rows)


PAGE_SCRIPT = """<script>
  const REFRESH_SECONDS = {refresh_seconds};
  let remaining = REFRESH_SECONDS;

  function updateClocks() {{
    const now = new Date();
    document.getElementById('clock-et').textContent =
      now.toLocaleTimeString('en-US', {{ timeZone: 'America/New_York', hour12: false }});
    document.getElementById('clock-utc').textContent =
      now.toLocaleTimeString('en-GB', {{ timeZone: 'UTC', hour12: false }});
  }}

  function tickRefresh() {{
    remaining -= 1;
    if (remaining <= 0) {{
      location.reload();
      return;
    }}
    document.getElementById('refresh-countdown').textContent = remaining;
  }}

  updateClocks();
  document.getElementById('refresh-countdown').textContent = remaining;
  setInterval(updateClocks, 1000);
  if (REFRESH_SECONDS > 0) {{
    setInterval(tickRefresh, 1000);
  }} else {{
    document.getElementById('refresh-countdown').textContent = 'off';
  }}
</script>
"""


def generate_html_report(all_results: list[GapResult], index_etf_results: list[GapResult],
                          threshold: float, scanned_count: int,
                          output_path: str = "gap_report_usa.html",
                          refresh_seconds: int = 300) -> str:
    stock_results = [r for r in all_results if r.category == "STOCK"]
    stock_gappers = [r for r in stock_results if r.status != "NEUTRAL"]

    up_results = sorted([r for r in stock_gappers if r.status == "GAP UP"], key=lambda r: r.gap_pct, reverse=True)
    down_results = sorted([r for r in stock_gappers if r.status == "GAP DOWN"], key=lambda r: r.gap_pct)

    up_count, down_count = len(up_results), len(down_results)
    total_gappers = max(up_count + down_count, 1)
    up_pct = up_count / total_gappers * 100
    down_pct = down_count / total_gappers * 100

    if up_pct >= 60:
        sentiment_label, sentiment_color = "Bullish", "#3ecf8e"
    elif up_pct <= 40:
        sentiment_label, sentiment_color = "Bearish", "#ff6b6b"
    else:
        sentiment_label, sentiment_color = "Neutral", "#facc15"

    heatmap = build_sector_heatmap(stock_results)
    dist = build_gap_distribution(stock_gappers)
    max_bucket = max((v["up"] + v["down"] for v in dist.values()), default=1) or 1

    html_parts = [PAGE_HEAD.format(
        report_date=datetime.now().strftime("%d %b %Y"),
        report_datetime=datetime.now().strftime("%d %b %Y, %I:%M %p"),
        threshold=threshold,
    )]

    html_parts.append(OVERVIEW_BLOCK.format(
        scanned_count=scanned_count,
        up_count=up_count, up_pct=up_pct,
        down_count=down_count, down_pct=down_pct,
        sentiment_label=sentiment_label, sentiment_color=sentiment_color,
    ))

    html_parts.append(f"""  <section>
    <div class="section-title"><h2>🟢 Bullish — Gap Up</h2><span class="count-pill">{up_count}</span></div>
{build_stock_table(up_results, "up")}
  </section>""")

    html_parts.append(f"""  <section>
    <div class="section-title"><h2>🔴 Bearish — Gap Down</h2><span class="count-pill">{down_count}</span></div>
{build_stock_table(down_results, "down")}
  </section>""")

    html_parts.append(f"""  <section>
    <div class="section-title"><h2>📈 Indices & Sector ETFs</h2><span class="count-pill">{len(index_etf_results)}</span></div>
{build_index_etf_table(index_etf_results)}
  </section>""")

    html_parts.append(f"""  <section>
    <h2>🗺️ Sector Heatmap <span style="color:#6b7280;font-weight:400">(avg gap % across all scanned stocks per sector)</span></h2>
{build_sector_heatmap_html(heatmap)}
  </section>""")

    html_parts.append(f"""  <section>
    <h2>📶 Gap Size Distribution</h2>
{build_distribution_html(dist, max_bucket)}
  </section>""")

    html_parts.append('  <footer>Generated automatically by gap_scanner_usa.py · Data via Yahoo Finance (yfinance) · No pre-market data used</footer>')
    html_parts.append('</div>')
    html_parts.append(PAGE_SCRIPT.format(refresh_seconds=refresh_seconds))
    html_parts.append('</body>\n</html>')

    html = "\n".join(html_parts)
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(html)

    return output_path


# ═══════════════════════════════════════════════════════════════════════════
# TELEGRAM ALERT
# ═══════════════════════════════════════════════════════════════════════════

TELEGRAM_API = "https://api.telegram.org/bot{token}/sendMessage"

# Fixed column widths for the monospace table (rendered inside a ``` block)
COL_TICKER, COL_SECTOR, COL_GAP, COL_LTP, COL_VOL, COL_TAG = 12, 7, 8, 8, 10, 6

STRENGTH_CODE = {"Strong": "STR", "Moderate": "MOD", "Weak": "WEAK"}
RISK_CODE = {"Heavy": "HVY", "Medium": "MED", "Mild": "MLD"}


def format_short_num(n: float) -> str:
    """Compact number format for table cells: 12045300 -> '12.0M'."""
    n = float(n)
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}K"
    return f"{n:.0f}"


def _table_row(ticker: str, sector: str, gap_pct: float, ltp: float, vol_ratio: float, tag: str = "") -> str:
    cells = [
        ticker[:COL_TICKER].ljust(COL_TICKER),
        sector[:COL_SECTOR].ljust(COL_SECTOR),
        f"{gap_pct:+.2f}%".ljust(COL_GAP),
        f"{ltp:.1f}".ljust(COL_LTP),
        f"{vol_ratio * 100:.0f}%".ljust(COL_VOL),
    ]
    if tag:
        cells.append(tag.ljust(COL_TAG))
    return "".join(cells).rstrip()


def _table_header(tag_label: str) -> str:
    cells = [
        "Ticker".ljust(COL_TICKER),
        "Sector".ljust(COL_SECTOR),
        "Gap%".ljust(COL_GAP),
        "LTP".ljust(COL_LTP),
        "Vol%".ljust(COL_VOL),
    ]
    if tag_label:
        cells.append(tag_label.ljust(COL_TAG))
    header = "".join(cells).rstrip()
    return header + "\n" + "-" * len(header)


def build_telegram_message(all_results: list[GapResult], index_etf_results: list[GapResult] | None = None) -> str:
    stock_gappers = [r for r in all_results if r.category == "STOCK" and r.status != "NEUTRAL"]
    # Gap ups: drop "Weak" badge — only Strong/Moderate are worth alerting on.
    ups = sorted(
        [r for r in stock_gappers if r.status == "GAP UP" and r.badge != "Weak"],
        key=lambda r: r.gap_pct, reverse=True,
    )
    # Gap downs: drop "Mild" badge — only Heavy/Medium risk are worth alerting on.
    downs = sorted(
        [r for r in stock_gappers if r.status == "GAP DOWN" and r.badge != "Mild"],
        key=lambda r: r.gap_pct,
    )

    total = max(len(ups) + len(downs), 1)
    up_ratio = len(ups) / total
    sentiment = "Bullish" if up_ratio >= 0.6 else ("Bearish" if up_ratio <= 0.4 else "Neutral")

    lines = [
        "📊 *Daily Gap Scanner — USA*",
        f"🕒 {datetime.now().strftime('%d %b %Y, %I:%M %p')}",
        f"📈 Sentiment: *{sentiment}*",
    ]

    # ── SECTORS IN PLAY ─────────────────────────────────────
    # Only the sectors actually represented among the filtered gappers above
    # (not the full 11-sector list) — code + full name, e.g. "XLK Technology".
    sectors_in_play = sorted({r.sector for r in ups + downs})
    if sectors_in_play:
        sector_lines = [f"{code} {ETF_LABELS.get(code, code)}" for code in sectors_in_play]
        lines.append("🏷 Sectors: " + ", ".join(sector_lines))
    lines.append("")

    # ── GAP UP ──────────────────────────────────────────────
    lines.append(f"🔵 *GAP UP — {len(ups)}*")
    if ups:
        table = [_table_header(tag_label="Str")]
        for r in ups:
            tag = STRENGTH_CODE.get(r.badge, "")
            if r.volume_shock:
                tag += "⚡"
            table.append(_table_row(r.display_name, r.sector, r.gap_pct, r.today_open, r.volume_ratio, tag))
        lines.append("```\n" + "\n".join(table) + "\n```")
    else:
        lines.append("_No gap-ups beyond threshold._")
    lines.append("")

    # ── GAP DOWN ────────────────────────────────────────────
    lines.append(f"🔴 *GAP DOWN — {len(downs)}*")
    if downs:
        table = [_table_header(tag_label="Risk")]
        for r in downs:
            tag = RISK_CODE.get(r.badge, "")
            if r.volume_shock:
                tag += "⚡"
            table.append(_table_row(r.display_name, r.sector, r.gap_pct, r.today_open, r.volume_ratio, tag))
        lines.append("```\n" + "\n".join(table) + "\n```")
    else:
        lines.append("_No gap-downs beyond threshold._")
    # NOTE: Indices & Sector ETFs are intentionally NOT included in the
    # Telegram message (they still appear in the HTML report). The
    # index_etf_results parameter is kept for backward-compatible call
    # signatures but is no longer used here.

    return "\n".join(lines)


def send_telegram_message(bot_token: str, chat_id: str, text: str) -> bool:
    if not bot_token or "YOUR_BOT_TOKEN" in bot_token:
        print("[WARN] Telegram bot token not configured — skipping alert.")
        return False

    url = TELEGRAM_API.format(token=bot_token)
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "Markdown",
        "disable_web_page_preview": True,
    }

    try:
        resp = requests.post(url, json=payload, timeout=15)
        resp.raise_for_status()
        return True
    except requests.RequestException as exc:
        print(f"[ERROR] Telegram send failed: {exc}")
        return False


def send_gap_alert(bot_token: str, chat_id: str, all_results: list[GapResult],
                    index_etf_results: list[GapResult] | None = None) -> bool:
    message = build_telegram_message(all_results, index_etf_results)
    return send_telegram_message(bot_token, chat_id, message)


# ═══════════════════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(description="US Daily Gap Up/Down Scanner")
    parser.add_argument("--sample", action="store_true",
                         help="Scan a small 5-ticker sample instead of the full watchlist")
    parser.add_argument("--config", default="config.json", help="Path to config.json")
    parser.add_argument("--output", default=str(SCRIPT_DIR / "gap_report_usa.html"),
                         help="Output HTML file path (default: gap_report_usa.html next to this script)")
    parser.add_argument("--no-telegram", action="store_true", help="Skip sending the Telegram alert")
    parser.add_argument("--no-indices", action="store_true", help="Skip the separate Indices & Sector ETFs panel")
    args = parser.parse_args()

    config = load_config(args.config)
    threshold = config.get("gap_threshold_pct", 0.5)
    refresh_seconds = config.get("refresh_interval_sec", 300)

    if args.sample:
        tickers = SAMPLE_TICKERS
    else:
        tickers = USA_WATCHLIST + ([] if args.no_indices else INDEX_ETF_SYMBOLS)

    print(f"Scanning {len(tickers)} tickers (threshold ±{threshold}%)...")
    scanned = scan_tickers(tickers, threshold, pause=0.15)

    all_results = [r for r in scanned if r.category == "STOCK"]
    index_etf_results = [r for r in scanned if r.category in ("INDEX", "ETF")]

    gapper_count = len([r for r in all_results if r.status != "NEUTRAL"])
    print(f"Found {gapper_count} gaps beyond threshold.")

    output_path = generate_html_report(
        all_results, index_etf_results, threshold, len(tickers),
        args.output, refresh_seconds,
    )
    print(f"HTML report written to: {output_path}")

    if not args.no_telegram:
        sent = send_gap_alert(
            config.get("telegram_bot_token", ""),
            config.get("telegram_chat_id", ""),
            all_results,
            index_etf_results,
        )
        print("Telegram alert sent." if sent else "Telegram alert not sent (see warnings above).")


if __name__ == "__main__":
    sys.exit(main())
