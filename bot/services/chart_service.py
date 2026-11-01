# bot/services/chart_service.py
import io
import asyncio
import logging
import numpy as np
import pandas as pd
import mplfinance as mpf
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt

from .coingecko import get_json, get_json_url

TIMEFRAME_CONFIG = {
    "15m": {"binance_iv": "15m", "limit": 96, "cg_days": "1", "label": "15m"},
    "1h":  {"binance_iv": "1h",  "limit": 96, "cg_days": "7", "label": "1H"},
    "4h":  {"binance_iv": "4h",  "limit": 90, "cg_days": "14", "label": "4H"},
    "1":   {"binance_iv": "15m", "limit": 96, "cg_days": "1", "label": "24H (15m)"},
    "1d":  {"binance_iv": "15m", "limit": 96, "cg_days": "1", "label": "24H (15m)"},
    "7":   {"binance_iv": "2h",  "limit": 84, "cg_days": "7", "label": "7D (2H)"},
    "7d":  {"binance_iv": "2h",  "limit": 84, "cg_days": "7", "label": "7D (2H)"},
    "30":  {"binance_iv": "1d",  "limit": 60, "cg_days": "30", "label": "30D (1D)"},
    "30d": {"binance_iv": "1d",  "limit": 60, "cg_days": "30", "label": "30D (1D)"},
}


def plt_close():
    try:
        plt.close('all')
    except Exception:
        pass


def _compute_rsi(series: pd.Series, period: int = 14) -> pd.Series:
    delta = series.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    rsi = 100 - (100 / (1 + rs))
    return rsi.fillna(50.0)


def _fmt_chart_price(val: float) -> str:
    if val >= 1000:
        return f"${val:,.2f}"
    if val >= 1:
        return f"${val:,.4f}"
    return f"${val:,.6f}"


def _create_chart_image(symbol: str, df: pd.DataFrame, tf_label: str, has_volume: bool = False):
    fig = None
    try:
        last_close = float(df['Close'].iloc[-1])
        first_open = float(df['Open'].iloc[0])
        high_max = float(df['High'].max())
        low_min = float(df['Low'].min())
        pct_change = ((last_close - first_open) / first_open * 100.0) if first_open else 0.0
        sign = "+" if pct_change >= 0 else ""

        mc = mpf.make_marketcolors(
            up='#14F195',
            down='#F43F5E',
            wick={'up': '#14F195', 'down': '#F43F5E'},
            edge={'up': '#14F195', 'down': '#F43F5E'},
            volume={'up': '#14F19566', 'down': '#F43F5E66'}
        )
        style = mpf.make_mpf_style(
            base_mpf_style='nightclouds',
            marketcolors=mc,
            facecolor='#0B0F17',
            figcolor='#0B0F17',
            edgecolor='#1E293B',
            gridcolor='#1E293B',
            gridstyle='--',
            y_on_right=True,
            rc={
                'axes.labelcolor': '#94A3B8',
                'xtick.color': '#94A3B8',
                'ytick.color': '#94A3B8',
                'font.size': 9,
            }
        )

        addplots = []
        if len(df) >= 20:
            ema20 = df['Close'].ewm(span=20, adjust=False).mean()
            addplots.append(mpf.make_addplot(ema20, color='#00D4FF', width=1.2, panel=0))
        if len(df) >= 50:
            ema50 = df['Close'].ewm(span=50, adjust=False).mean()
            addplots.append(mpf.make_addplot(ema50, color='#F59E0B', width=1.2, panel=0))

        rsi_panel = 2 if has_volume else 1
        panel_ratios = (6, 1.6, 1.8) if has_volume else (6, 1.8)
        if len(df) >= 16:
            rsi = _compute_rsi(df['Close'], 14)
            addplots.append(mpf.make_addplot(rsi, panel=rsi_panel, color='#A855F7', ylabel='RSI(14)', width=1.2, ylim=(15, 85)))

        title_text = (
            f"\n{symbol.upper()}/USD  •  {_fmt_chart_price(last_close)} ({sign}{pct_change:.2f}%)  •  [{tf_label}]\n"
            f"High: {_fmt_chart_price(high_max)}   |   Low: {_fmt_chart_price(low_min)}   |   EMA 20 (Cyan) • EMA 50 (Gold)"
        )

        plot_kwargs = dict(
            type='candle',
            style=style,
            title=title_text,
            ylabel='Price (USD)',
            volume=has_volume,
            figsize=(11, 6.8),
            tight_layout=True,
            returnfig=True,
        )
        if addplots:
            plot_kwargs['addplot'] = addplots
            if len(df) >= 16:
                plot_kwargs['panel_ratios'] = panel_ratios

        fig, axlist = mpf.plot(df, **plot_kwargs)

        # Current price horizontal dashed reference line
        main_ax = axlist[0]
        line_color = '#14F195' if pct_change >= 0 else '#F43F5E'
        main_ax.axhline(last_close, color=line_color, linestyle='--', linewidth=0.9, alpha=0.75)

        # RSI 30/70 reference lines
        if len(df) >= 16 and len(axlist) > rsi_panel * 2:
            rsi_ax = axlist[rsi_panel * 2]
            rsi_ax.axhline(70, color='#F43F5E', linestyle=':', linewidth=0.7, alpha=0.5)
            rsi_ax.axhline(30, color='#14F195', linestyle=':', linewidth=0.7, alpha=0.5)

        # Subtle watermark
        main_ax.text(
            0.5, 0.5,
            'CEKREGO',
            transform=main_ax.transAxes,
            fontsize=34,
            color='#FFFFFF',
            alpha=0.05,
            ha='center',
            va='center',
            weight='bold',
            zorder=0
        )

        buf = io.BytesIO()
        fig.savefig(buf, format='png', dpi=135, bbox_inches='tight', facecolor='#0B0F17')
        buf.seek(0)
        plt.close(fig)
        return buf
    except Exception as e:
        logging.error(f"[CHART] Render error: {e}")
        if fig is not None:
            try:
                plt.close(fig)
            except Exception:
                pass
        plt_close()
        return None


async def _fetch_binance_ohlcv(symbol: str, interval: str, limit: int = 96):
    sym_clean = (symbol or "").upper().strip()
    if sym_clean in ("USDT", "USDC", "DAI"):
        return None
    pair = f"{sym_clean}USDT"
    url = "https://data-api.binance.vision/api/v3/klines"
    data = await get_json_url(url, params={"symbol": pair, "interval": interval, "limit": limit})
    if not data or not isinstance(data, list) or len(data) < 10:
        return None
    try:
        rows = []
        for k in data:
            rows.append([
                int(k[0]),
                float(k[1]),
                float(k[2]),
                float(k[3]),
                float(k[4]),
                float(k[5]),
            ])
        df = pd.DataFrame(rows, columns=['Date', 'Open', 'High', 'Low', 'Close', 'Volume'])
        df['Date'] = pd.to_datetime(df['Date'], unit='ms')
        df.set_index('Date', inplace=True)
        return df
    except Exception:
        return None


async def generate_chart(coin_id: str, symbol: str, days: str = "1"):
    tf_key = str(days or "1").lower().strip()
    cfg = TIMEFRAME_CONFIG.get(tf_key, TIMEFRAME_CONFIG["1"])

    try:
        # 1. Try high-resolution Binance OHLCV first (includes Volume + 96 candles)
        df = await _fetch_binance_ohlcv(symbol, cfg["binance_iv"], cfg["limit"])
        has_vol = True

        # 2. Fallback to CoinGecko OHLC for non-Binance coins
        if df is None:
            has_vol = False
            cg_data = await get_json(f"coins/{coin_id}/ohlc", {"vs_currency": "usd", "days": cfg["cg_days"]})
            if not cg_data or not isinstance(cg_data, list):
                return None
            df = pd.DataFrame(cg_data, columns=['Date', 'Open', 'High', 'Low', 'Close'])
            df['Date'] = pd.to_datetime(df['Date'], unit='ms')
            df.set_index('Date', inplace=True)

        loop = asyncio.get_running_loop()
        buf = await loop.run_in_executor(None, _create_chart_image, symbol, df, cfg["label"], has_vol)
        return buf
    except Exception as e:
        logging.error(f"[CHART] generate_chart err: {e}")
        return None
