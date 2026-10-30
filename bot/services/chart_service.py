# bot/services/chart_service.py
import io
import asyncio
import logging
import pandas as pd
import mplfinance as mpf
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt

from .coingecko import get_json

def plt_close():
    try:
        plt.close('all')
    except:
        pass

def _create_chart_image(symbol, ohlc_data, days, interval):
    fig = None
    try:
        df = pd.DataFrame(ohlc_data, columns=['Date', 'Open', 'High', 'Low', 'Close'])
        df['Date'] = pd.to_datetime(df['Date'], unit='ms')
        df.set_index('Date', inplace=True)

        mc = mpf.make_marketcolors(up='#6C2DC7', down='#DC143C', wick='inherit', edge='inherit', volume='inherit')
        s = mpf.make_mpf_style(base_mpf_style='nightclouds', marketcolors=mc, gridstyle='--', facecolor='#0d1117')

        fig, axlist = mpf.plot(
            df,
            type='candle',
            style=s,
            title=f"\n{symbol.upper()} ({days}d - {interval})",
            ylabel='Price (USD)',
            mav=(9, 21, 50),
            returnfig=True
        )

        # Watermark samar di latar belakang: "CEKREGO BOT"
        ax = axlist[0]
        ax.text(
            0.5, 0.5,
            'CEKREGO BOT',
            transform=ax.transAxes,
            fontsize=36,
            color='#ffffff',
            alpha=0.07,
            ha='center',
            va='center',
            rotation=20,
            weight='bold',
            zorder=0
        )

        buf = io.BytesIO()
        fig.savefig(buf, format='png', dpi=105, bbox_inches='tight')
        buf.seek(0)
        plt.close(fig)
        return buf
    except Exception as e:
        logging.error(f"[CHART] Error: {e}")
        if fig is not None:
            try:
                plt.close(fig)
            except Exception:
                pass
        plt_close()
        return None


async def generate_chart(coin_id, symbol, days):
    try:
        data = await get_json(f"/coins/{coin_id}/ohlc", {"vs_currency": "usd", "days": days})
        if not data:
            return None
        loop = asyncio.get_running_loop()
        buf = await loop.run_in_executor(None, _create_chart_image, symbol, data, days, "OHLC")
        return buf
    except Exception as e:
        logging.error(f"[CHART] err: {e}")
        return None

# --- 🧠 QUANTITATIVE ANALYSIS CORE ---
