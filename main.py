"""
DENGE ARALIĞI TELEGRAM BOTU (Hibrit Model - Twelve Data + yfinance yedekli)
============================================================================
Clab'ın orijinal kodu korundu. Sadece XAGUSD gibi ücretsiz planda kapalı
semboller için yfinance yedek kaynak olarak eklendi.
BTCUSD, XAUUSD, EURUSD gibi çalışan semboller hâlâ Twelve Data'dan çekilir.
"""

import os
import logging
import statistics
from collections import Counter
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import requests
import yfinance as yf
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes

# ----------------------------------------------------------------------------
# AYARLAR
# ----------------------------------------------------------------------------

load_dotenv()

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TWELVE_DATA_API_KEY = os.getenv("TWELVE_DATA_API_KEY")

TWELVE_DATA_URL = "https://api.twelvedata.com/time_series"

CREDIT_BAR_UNIT = 21
MAX_SHORT_DAILY_BARS = 60
MAX_LONG_WEEKLY_BARS = 84
FOUR_HOUR_OUTPUTSIZE = 20

PERIOD_NAMES = ["4 Saatlik", "Günlük", "Haftalık", "Aylık", "6 Aylık", "Yıllık"]

TR_TZ = ZoneInfo("Europe/Istanbul")

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)


# ----------------------------------------------------------------------------
# SEMBOL NORMALİZASYON
# ----------------------------------------------------------------------------

def normalize_symbol(user_symbol: str) -> str:
    """'BTCUSD' -> 'BTC/USD', 'XAUUSD' -> 'XAU/USD' gibi Twelve Data formatına çevirir."""
    s = user_symbol.strip().upper().replace(" ", "")
    if "/" in s:
        return s
    if len(s) > 3:
        base, quote = s[:-3], s[-3:]
        return f"{base}/{quote}"
    return s


def _normalize_yfinance_symbol(user_symbol: str) -> str:
    """Kullanıcı girdisini yfinance formatına çevirir (yedek kaynak için)."""
    s = user_symbol.strip().upper().replace(" ", "")

    # DXY özel durumu
    if s in ("DXY", "DXYUSD"):
        return "DX-Y.NYB"

    # VIX özel durumu
    if s in ("VIX", "VIXUSD"):
        return "^VIX"

    # BIST endeksleri özel durumu
    if s in ("XU100", "BIST100"):
        return "^XU100"
    if s in ("XU030", "XU30", "BIST30"):
        return "^XU030"

    if "/" in s:
        base, quote = s.split("/")
    elif len(s) > 3:
        base, quote = s[:-3], s[-3:]
    else:
        return s

    # Emtialar (XAG, XAU, XPT, XPD) -> =X eki
    if base in ("XAU", "XAG", "XPT", "XPD"):
        return f"{base}{quote}=X"
    # Forex majörleri -> =X eki
    forex_bases = {"EUR", "GBP", "USD", "JPY", "CHF", "AUD", "NZD", "CAD"}
    forex_quotes = {"USD", "EUR", "GBP", "JPY", "CHF", "AUD", "NZD", "CAD"}
    if base in forex_bases and quote in forex_quotes:
        return f"{base}{quote}=X"
    # Kripto ve hisseler -> - formatı
    return f"{base}-{quote}"


# ----------------------------------------------------------------------------
# VERİ ÇEKME - ANA KAYNAK: Twelve Data
# ----------------------------------------------------------------------------

def fetch_bars_twelvedata(user_symbol: str, interval: str, outputsize: int):
    """Twelve Data'dan belirtilen aralıkta son `outputsize` mumu çeker."""
    if not TWELVE_DATA_API_KEY:
        raise ValueError("TWELVE_DATA_API_KEY tanımlı değil. Ortam değişkenlerini kontrol edin.")

    symbol = normalize_symbol(user_symbol)
    params = {
        "symbol": symbol,
        "interval": interval,
        "outputsize": outputsize,
        "apikey": TWELVE_DATA_API_KEY,
    }

    resp = requests.get(TWELVE_DATA_URL, params=params, timeout=15)
    data = resp.json()

    if isinstance(data, dict) and data.get("status") == "error":
        msg = data.get("message", "")
        # "plan" / "Grow" / "Venture" / "available starting" geçiyorsa
        # -> bu sembol ücretsiz planda kapalı, yedek kaynağa geçilecek
        if any(word in msg.lower() for word in ["plan", "grow", "venture", "available starting"]):
            raise ValueError("UPGRADE_REQUIRED")
        raise ValueError(msg)

    if "values" not in data:
        raise ValueError(f"'{symbol}' için veri bulunamadı. Sembolü kontrol edin (örn: BTCUSD, XAUUSD, EURUSD).")

    return data["values"]


# ----------------------------------------------------------------------------
# VERİ ÇEKME - YEDEK KAYNAK: yfinance (sadece Twelve Data başarısız olursa)
# ----------------------------------------------------------------------------

def fetch_bars_yfinance(user_symbol: str, interval: str, outputsize: int):
    """
    Yedek veri kaynağı. Sadece Twelve Data 'UPGRADE_REQUIRED' hatası
    verdiğinde (ücretsiz planda kapalı semboller için) çağrılır.
    """
    symbol = _normalize_yfinance_symbol(user_symbol)

    if interval == "4h":
        return _fetch_4h_yfinance(symbol, outputsize)
    elif interval == "1day":
        return _fetch_daily_yfinance(symbol, outputsize)
    elif interval == "1week":
        return _fetch_weekly_yfinance(symbol, outputsize)
    else:
        raise ValueError(f"yfinance desteklenmeyen interval: {interval}")


def _fetch_daily_yfinance(symbol: str, outputsize: int):
    ticker = yf.Ticker(symbol)
    end = datetime.now(TR_TZ)
    start = end - timedelta(days=outputsize + 10)
    df = ticker.history(start=start, end=end, interval="1d")

    if df.empty:
        raise ValueError(f"yfinance: '{symbol}' için günlük veri bulunamadı")

    return [
        {"datetime": idx.strftime("%Y-%m-%d"), "high": float(row["High"]), "low": float(row["Low"])}
        for idx, row in df.tail(outputsize).iterrows()
    ]


def _fetch_weekly_yfinance(symbol: str, outputsize: int):
    ticker = yf.Ticker(symbol)
    end = datetime.now(TR_TZ)
    start = end - timedelta(weeks=outputsize + 5)
    df = ticker.history(start=start, end=end, interval="1wk")

    if df.empty:
        raise ValueError(f"yfinance: '{symbol}' için haftalık veri bulunamadı")

    return [
        {"datetime": idx.strftime("%Y-%m-%d"), "high": float(row["High"]), "low": float(row["Low"])}
        for idx, row in df.tail(outputsize).iterrows()
    ]


def _fetch_4h_yfinance(symbol: str, outputsize: int):
    ticker = yf.Ticker(symbol)
    end = datetime.now(TR_TZ)
    start = end - timedelta(hours=(outputsize + 1) * 4 + 10)
    df = ticker.history(start=start, end=end, interval="1h")

    if df.empty:
        raise ValueError(f"yfinance: '{symbol}' için 4 saatlik veri bulunamadı")

    df_4h = df.resample("4h").agg({
        "Open": "first", "High": "max", "Low": "min", "Close": "last"
    }).dropna()

    return [
        {"datetime": idx.strftime("%Y-%m-%d %H:%M:%S"), "high": float(row["High"]), "low": float(row["Low"])}
        for idx, row in df_4h.tail(outputsize).iterrows()
    ]


# ----------------------------------------------------------------------------
# ANA fetch_bars FONKSİYONU (Önce Twelve Data, başarısızsa yfinance)
# ----------------------------------------------------------------------------

# Twelve Data'nın hiç taşımadığı (BIST endeksleri gibi) semboller.
# Bunlar için Twelve Data'yı denemeden direkt yfinance'e gidilir.
YFINANCE_ONLY_SYMBOLS = {"XU100", "BIST100", "XU030", "XU30", "BIST30"}

# 1 ons = 31.1034768 gram. XAUTRYG (Gram Altın/TL) hiçbir sağlayıcıda tek bir
# sembol olarak bulunmuyor; (XAU/USD / 31.1034768) * USD/TRY formülüyle
# TÜRETİLİR.
GRAMS_PER_TROY_OUNCE = 31.1034768


def fetch_bars(user_symbol: str, interval: str, outputsize: int):
    """
    Önce Twelve Data'yı dener.
    Eğer 'UPGRADE_REQUIRED' hatası alırsa (ücretsiz planda kapalı sembol),
    otomatik olarak yfinance yedek kaynağına geçer.
    XU100/XU030 gibi Twelve Data'da hiç bulunmayan semboller için
    Twelve Data hiç denenmeden direkt yfinance kullanılır.
    XAUTRYG (Gram Altın/TL) ise XAU/TRY üzerinden TÜRETİLİR (gerçek bir
    sembol değil, ons fiyatı 31.1034768'e bölünerek gram fiyatı elde edilir).
    """
    normalized_input = user_symbol.strip().upper().replace(" ", "")

    if normalized_input in ("XAUTRYG", "GRAMALTIN"):
        # Formül: (XAUUSD / 31.1034768) * USDTRY
        xau_bars = fetch_bars("XAUUSD", interval, outputsize)
        usdtry_bars = fetch_bars("USDTRY", interval, outputsize)
        usdtry_by_date = {b["datetime"]: b for b in usdtry_bars}

        result = []
        for xb in xau_bars:
            ub = usdtry_by_date.get(xb["datetime"])
            if ub is None:
                continue  # o tarihte eşleşen USDTRY verisi yoksa bu günü atla
            result.append({
                "datetime": xb["datetime"],
                "high": (float(xb["high"]) / GRAMS_PER_TROY_OUNCE) * float(ub["high"]),
                "low": (float(xb["low"]) / GRAMS_PER_TROY_OUNCE) * float(ub["low"]),
            })

        if not result:
            raise ValueError("XAUTRYG için XAUUSD ve USDTRY tarihleri eşleştirilemedi.")
        return result

    if normalized_input in YFINANCE_ONLY_SYMBOLS:
        return fetch_bars_yfinance(user_symbol, interval, outputsize)

    try:
        return fetch_bars_twelvedata(user_symbol, interval, outputsize)
    except ValueError as e:
        if str(e) == "UPGRADE_REQUIRED":
            logger.info(f"🔄 '{user_symbol}' Twelve Data ücretsiz planda kapalı, yfinance deneniyor...")
            return fetch_bars_yfinance(user_symbol, interval, outputsize)
        raise


# ----------------------------------------------------------------------------
# DÖNEM SINIRLARI (son TAMAMLANMIŞ hafta / ay / yarıyıl / yıl)
# ----------------------------------------------------------------------------

def get_last_completed_week_range(today: date, is_crypto: bool = False):
    weekday = today.weekday()

    if is_crypto:
        this_monday = today - timedelta(days=weekday)
        last_monday = this_monday - timedelta(days=7)
        last_sunday = last_monday + timedelta(days=6)
        return last_monday, last_sunday

    if weekday >= 5:
        week_monday = today - timedelta(days=weekday)
    else:
        this_monday = today - timedelta(days=weekday)
        week_monday = this_monday - timedelta(days=7)

    week_friday = week_monday + timedelta(days=4)
    return week_monday, week_friday


def get_last_completed_month_range(today: date):
    first_of_this_month = today.replace(day=1)
    last_day_prev_month = first_of_this_month - timedelta(days=1)
    first_day_prev_month = last_day_prev_month.replace(day=1)
    return first_day_prev_month, last_day_prev_month


def get_last_completed_half_year_range(today: date):
    year = today.year
    if today.month <= 6:
        return date(year - 1, 7, 1), date(year - 1, 12, 31)
    return date(year, 1, 1), date(year, 6, 30)


def get_last_completed_year_range(today: date):
    last_year = today.year - 1
    return date(last_year, 1, 1), date(last_year, 12, 31)


# ----------------------------------------------------------------------------
# HESAPLAMA
# ----------------------------------------------------------------------------

def _parse_bar_date(bar: dict) -> date:
    return datetime.strptime(bar["datetime"][:10], "%Y-%m-%d").date()


def _parse_bar_datetime(bar: dict) -> datetime:
    raw = bar["datetime"]
    if len(raw) > 10:
        return datetime.strptime(raw, "%Y-%m-%d %H:%M:%S")
    return datetime.strptime(raw, "%Y-%m-%d")


def _filter_by_range(bars, start: date, end: date):
    return [b for b in bars if start <= _parse_bar_date(b) <= end]


CRYPTO_BASES = {
    "BTC", "ETH", "XRP", "LTC", "BCH", "ADA", "SOL", "DOGE", "DOT", "MATIC",
    "BNB", "AVAX", "LINK", "TRX", "SHIB", "ATOM", "UNI", "XLM", "ETC", "FIL",
    "APT", "ARB", "OP", "NEAR", "ICP", "AAVE", "SAND", "MANA", "ALGO", "VET",
}


def _is_crypto_symbol(user_symbol: str) -> bool:
    normalized = normalize_symbol(user_symbol)
    base = normalized.split("/")[0].upper()
    return base in CRYPTO_BASES


def _filter_weekend_bars_if_not_crypto(bars, user_symbol: str):
    if _is_crypto_symbol(user_symbol):
        return bars
    return [b for b in bars if _parse_bar_date(b).weekday() < 5]


def _last_completed_day_bars(bars, today: date):
    completed = [b for b in bars if _parse_bar_date(b) < today]
    if not completed:
        return []
    latest = max(_parse_bar_date(b) for b in completed)
    return [b for b in completed if _parse_bar_date(b) == latest]


def _levels_from_bars(bars, birim: str = "gün") -> dict:
    if not bars:
        raise ValueError("bu dönem henüz tamamlanmamış veya veri yok")

    values = []
    for bar in bars:
        values.append(round(float(bar["high"]), 4))
        values.append(round(float(bar["low"]), 4))

    denge = statistics.median(values)
    ortalama = statistics.mean(values)
    range_ = max(values) - min(values)
    half_range = range_ * 0.5

    counts = Counter(values)
    mods = sorted([v for v, c in counts.items() if c >= 2])
    dates = sorted({_parse_bar_date(b) for b in bars})

    destek1 = denge - half_range
    destek2 = denge - range_

    uyari = None
    if destek2 < 0:
        uyari = "⚠️ Bu dönem çok oynak; Destek 2 matematiksel olarak negatif çıktı (fiyatta gerçekleşemez)."
    elif destek1 < 0:
        uyari = "⚠️ Bu dönem çok oynak; Destek 1 matematiksel olarak negatif çıktı (fiyatta gerçekleşemez)."

    return {
        "denge": denge,
        "ortalama": ortalama,
        "direnc1": denge + half_range,
        "direnc2": denge + range_,
        "destek1": destek1,
        "destek2": destek2,
        "range": range_,
        "mod": mods,
        "adet": len(bars),
        "birim": birim,
        "baslangic": dates[0].isoformat(),
        "bitis": dates[-1].isoformat(),
        "uyari": uyari,
    }


def _compute_short_daily_outputsize(today: date) -> int:
    month_start, _ = get_last_completed_month_range(today)
    days_needed = (today - month_start).days + 7
    return max(15, min(days_needed, MAX_SHORT_DAILY_BARS))


def _compute_long_weekly_outputsize(today: date) -> int:
    year_start, _ = get_last_completed_year_range(today)
    days_needed = (today - year_start).days
    weeks_needed = (days_needed // 7) + 3
    return max(30, min(weeks_needed, MAX_LONG_WEEKLY_BARS))


def calculate_all_periods(user_symbol: str) -> dict:
    today = datetime.now(TR_TZ).date()
    results = {}

    # --- 4 Saatlik ---
    try:
        four_hour_bars = fetch_bars(user_symbol, "4h", FOUR_HOUR_OUTPUTSIZE)
        four_hour_bars = _filter_weekend_bars_if_not_crypto(four_hour_bars, user_symbol)
        last_bar = max(four_hour_bars, key=_parse_bar_datetime) if four_hour_bars else None
        if last_bar is None:
            raise ValueError("Yeterli 4 saatlik veri bulunamadı.")
        results["4 Saatlik"] = _levels_from_bars([last_bar], birim="adet 4 saatlik mum")
    except Exception as e:
        results["4 Saatlik"] = {"hata": str(e)}

    # --- Günlük, Haftalık, Aylık (günlük mumlar) ---
    try:
        short_size = _compute_short_daily_outputsize(today)
        daily_bars = fetch_bars(user_symbol, "1day", short_size)
        daily_bars = _filter_weekend_bars_if_not_crypto(daily_bars, user_symbol)
    except Exception as e:
        error = {"hata": str(e)}
        results["Günlük"] = error
        results["Haftalık"] = error
        results["Aylık"] = error
        daily_bars = None

    if daily_bars is not None:
        try:
            bars = _last_completed_day_bars(daily_bars, today)
            results["Günlük"] = _levels_from_bars(bars, birim="gün")
        except Exception as e:
            results["Günlük"] = {"hata": str(e)}

        try:
            is_crypto = _is_crypto_symbol(user_symbol)
            start, end = get_last_completed_week_range(today, is_crypto=is_crypto)
            results["Haftalık"] = _levels_from_bars(_filter_by_range(daily_bars, start, end), birim="gün")
        except Exception as e:
            results["Haftalık"] = {"hata": str(e)}

        try:
            start, end = get_last_completed_month_range(today)
            results["Aylık"] = _levels_from_bars(_filter_by_range(daily_bars, start, end), birim="gün")
        except Exception as e:
            results["Aylık"] = {"hata": str(e)}

    # --- 6 Aylık, Yıllık (haftalık mumlar) ---
    try:
        long_size = _compute_long_weekly_outputsize(today)
        weekly_bars = fetch_bars(user_symbol, "1week", long_size)
    except Exception as e:
        error = {"hata": str(e)}
        results["6 Aylık"] = error
        results["Yıllık"] = error
        weekly_bars = None

    if weekly_bars is not None:
        try:
            start, end = get_last_completed_half_year_range(today)
            results["6 Aylık"] = _levels_from_bars(_filter_by_range(weekly_bars, start, end), birim="hafta")
        except Exception as e:
            results["6 Aylık"] = {"hata": str(e)}

        try:
            start, end = get_last_completed_year_range(today)
            results["Yıllık"] = _levels_from_bars(_filter_by_range(weekly_bars, start, end), birim="hafta")
        except Exception as e:
            results["Yıllık"] = {"hata": str(e)}

    return results


# ----------------------------------------------------------------------------
# TELEGRAM BOTU
# ----------------------------------------------------------------------------

PERIOD_ICONS = {
    "4 Saatlik": "🕓",
    "Günlük": "🕐",
    "Haftalık": "📅",
    "Aylık": "🗓️",
    "6 Aylık": "📈",
    "Yıllık": "🏆",
}

CONFIRMATION_NOTES = {
    "4 Saatlik": "2 adet 30 dakikalık kapanış",
    "Günlük": "2 adet 1 saatlik kapanış",
    "Haftalık": "2 adet 4 saatlik kapanış",
    "Aylık": "2 adet günlük kapanış",
    "6 Aylık": "2 adet aylık kapanış",
    "Yıllık": "2 adet 6 aylık kapanış",
}


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "✨ *Denge Aralığı Botu* ✨\n\n"
        "Bana bir enstrüman kodu gönder (örn: *BTCUSD*, *XAUUSD*, *XAGUSD*, *XPTUSD*, "
        "*XPDUSD*, *EURUSD*, *DXY*, *VIX*, *XU100*, *XU030*, *XAUTRYG*).\n\n"
        "🕐 Günlük  📅 Haftalık  🗓️ Aylık  📈 6 Aylık  🏆 Yıllık\n"
        "için Denge (Medyan), Aritmetik Ortalama, Direnç 1/2 ve Destek 1/2 "
        "seviyelerini hesaplayayım.\n\n"
        "_Yalnızca TAMAMLANMIŞ (kapanmış) son periyot kullanılır._\n"
        "_6 Aylık ve Yıllık, API kredi limiti nedeniyle haftalık mumlarla hesaplanır._",
        parse_mode="Markdown",
    )


def _format_tr_date(iso_date: str) -> str:
    d = datetime.strptime(iso_date, "%Y-%m-%d").date()
    return d.strftime("%d.%m.%Y")


def format_period_block(period_name: str, result: dict) -> str:
    icon = PERIOD_ICONS.get(period_name, "•")

    if "hata" in result:
        return f"{icon} *{period_name}*\n⚠️ _{result['hata']}_"

    birim_etiketi = f"{result['adet']} {result['birim']}"
    tarih_araligi = f"{_format_tr_date(result['baslangic'])} → {_format_tr_date(result['bitis'])}"

    def row(label: str, value: float, emoji: str = "") -> str:
        full_label = f"{emoji} {label}" if emoji else label
        return f"{full_label:<11}{value:>11,.2f}"

    table = "\n".join([
        row("Direnç 2", result["direnc2"], "🔴"),
        row("Direnç 1", result["direnc1"], "🔴"),
        "─" * 22,
        row("Denge", result["denge"], "🟣"),
        row("Ortalama", result["ortalama"], "🟢"),
        "─" * 22,
        row("Destek 1", result["destek1"], "🔵"),
        row("Destek 2", result["destek2"], "🔵"),
    ])

    lines = [
        f"{icon} *{period_name}*  _({birim_etiketi} · {tarih_araligi})_",
        f"```\n{table}\n```",
    ]

    if result["mod"]:
        mod_str = ", ".join(f"{v:,.2f}" for v in result["mod"])
        lines.append(f"🔁 _Mod (tekrarlayan seviye): {mod_str}_")

    if result.get("uyari"):
        lines.append(f"_{result['uyari']}_")

    confirmation_note = CONFIRMATION_NOTES.get(period_name)
    if confirmation_note:
        lines.append(f"📌 _Onay: {confirmation_note}, Denge'nin üstünde ya da altında kapanmalı_")

    return "\n".join(lines)


async def handle_symbol(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_symbol = update.message.text.strip()
    processing_msg = await update.message.reply_text(f"⏳ {user_symbol.upper()} hesaplanıyor...")

    results = calculate_all_periods(user_symbol)

    separator = "━" * 24
    blocks = [f"💰 *{user_symbol.upper()}*", separator, ""]
    for i, period_name in enumerate(PERIOD_NAMES):
        blocks.append(format_period_block(period_name, results.get(period_name, {"hata": "sonuç yok"})))
        if i < len(PERIOD_NAMES) - 1:
            blocks.append("")

    message = "\n".join(blocks).strip()

    if len(message) <= 4000:
        await processing_msg.edit_text(message, parse_mode="Markdown")
    else:
        await processing_msg.delete()
        await update.message.reply_text(f"💰 *{user_symbol.upper()}*\n{separator}", parse_mode="Markdown")
        for period_name in PERIOD_NAMES:
            block = format_period_block(period_name, results.get(period_name, {"hata": "sonuç yok"}))
            await update.message.reply_text(block, parse_mode="Markdown")


def main():
    if not TELEGRAM_BOT_TOKEN:
        raise RuntimeError("TELEGRAM_BOT_TOKEN tanımlı değil. Ortam değişkenlerini kontrol edin.")

    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_symbol))

    logger.info("Bot başlatıldı, mesajlar bekleniyor...")
    app.run_polling()


if __name__ == "__main__":
    main()
