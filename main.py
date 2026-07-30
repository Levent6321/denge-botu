"""
DENGE ARALIĞI TELEGRAM BOTU (3 Kaynaklı Hibrit Model)
============================================================================
1) Twelve Data      -> BTCUSD, XAUUSD, EURUSD gibi standart forex/kripto/emtia
2) isyatirimhisse    -> XU100, XU030, XU500 gibi BIST endeksleri (İş Yatırım)
3) Yahoo Finance     -> XAGUSD, XPTUSD, XPDUSD, VIX, DXY (YEDEK KAYNAK)
XAUTRYG (Gram Altın/TL) ise XAUUSD ve USDTRY üzerinden TÜRETİLİR.
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

try:
    import isyatirimhisse
except ImportError:
    isyatirimhisse = None

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


def _normalize_yahoo_symbol(user_symbol: str) -> str:
    """Kullanıcı girdisini Yahoo Finance formatına çevirir."""
    s = user_symbol.strip().upper().replace(" ", "")
    
    yahoo_map = {
        "XAGUSD": "SI=F",
        "XPTUSD": "PL=F",
        "XPDUSD": "PA=F",
        "VIX": "^VIX",
        "VIXUSD": "^VIX",
        "DXY": "DX-Y.NYB",
        "DXYUSD": "DX-Y.NYB",
    }
    
    return yahoo_map.get(s, s)


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
# VERİ ÇEKME - YAHOO FINANCE (XAGUSD, XPTUSD, XPDUSD, VIX, DXY İÇİN)
# ----------------------------------------------------------------------------

def fetch_bars_yahoo(user_symbol: str, interval: str, outputsize: int):
    """Yahoo Finance'den veri çeker (XAGUSD, XPTUSD, XPDUSD, VIX, DXY için)."""
    yahoo_symbol = _normalize_yahoo_symbol(user_symbol)
    
    interval_map = {
        "4h": "60m",
        "1day": "1d",
        "1week": "1wk",
    }
    
    yf_interval = interval_map.get(interval, "1d")
    
    if interval == "4h":
        period = "5d"
    else:
        period = f"{outputsize * 2}d" if interval == "1day" else f"{outputsize * 2}wk"
    
    try:
        ticker = yf.Ticker(yahoo_symbol)
        df = ticker.history(period=period, interval=yf_interval)
        
        if df.empty:
            raise ValueError(f"Yahoo Finance: {user_symbol} için veri yok")
        
        bars = []
        for idx, row in df.iterrows():
            bars.append({
                "datetime": idx.strftime("%Y-%m-%d %H:%M:%S"),
                "high": float(row['High']),
                "low": float(row['Low']),
            })
        
        result = bars[-outputsize:] if len(bars) > outputsize else bars
        
        if not result:
            raise ValueError("Veri boş geldi")
            
        return result
        
    except Exception as e:
        raise ValueError(f"Yahoo Finance hatası: {e}")


# ----------------------------------------------------------------------------
# VERİ ÇEKME - BIST ENDEKSLERİ: isyatirimhisse (İş Yatırım)
# ----------------------------------------------------------------------------

# XU100/XU030/XU500 gibi BIST endeksleri Twelve Data'da hiç bulunmuyor.
# Bunlar için doğrudan İş Yatırım'ın verisi (isyatirimhisse) kullanılır.
BIST_INDEX_ALIASES = {
    "XU100": "XU100", "BIST100": "XU100",
    "XU030": "XU030", "XU30": "XU030", "BIST30": "XU030",
    "XU500": "XU500", "BIST500": "XU500",
}


def _normalize_date_str(raw) -> str:
    """isyatirimhisse'den gelen tarihi 'YYYY-MM-DD' formatına çevirir
    (kaynak GG-AA-YYYY, GG.AA.YYYY ya da zaten ISO olabilir)."""
    raw = str(raw).strip()
    if len(raw) >= 10 and raw[4] == "-" and raw[7] == "-":
        return raw[:10]
    for sep in ("-", ".", "/"):
        parts = raw.split(sep)
        if len(parts) == 3:
            if len(parts[2]) == 4:  # GG-AA-YYYY
                gg, aa, yyyy = parts
                return f"{yyyy}-{aa.zfill(2)}-{gg.zfill(2)}"
            if len(parts[0]) == 4:  # YYYY-AA-GG
                yyyy, aa, gg = parts
                return f"{yyyy}-{aa.zfill(2)}-{gg.zfill(2)}"
    raise ValueError(f"Tarih formatı tanınamadı: {raw}")


def fetch_bars_bist_index(user_symbol: str, interval: str, outputsize: int):
    """BIST endeksleri (XU100/XU030/XU500) için İş Yatırım'dan veri çeker.
    Bu kaynakta gün-içi (4 saatlik) veri YOKTUR."""
    if interval == "4h":
        raise ValueError("İş Yatırım kaynağında 4 saatlik veri yok.")

    if isyatirimhisse is None:
        raise ValueError("isyatirimhisse kütüphanesi kurulu değil. Lütfen 'pip install isyatirimhisse' yapın.")

    index_code = BIST_INDEX_ALIASES[user_symbol.strip().upper().replace(" ", "")]

    today = datetime.now(TR_TZ).date()
    start = today - timedelta(days=800)  # ~2.2 yıl geriye, yıllık ihtiyacı karşılar

    df = isyatirimhisse.fetch_index_data(
        indices=index_code,
        start_date=start.strftime("%d-%m-%Y"),
        end_date=today.strftime("%d-%m-%Y"),
    )

    if df is None or df.empty:
        raise ValueError(f"İş Yatırım'dan '{user_symbol}' için veri alınamadı.")

    date_col = next((c for c in df.columns if "tarih" in c.lower() or "date" in c.lower()), None)
    high_col = next((c for c in df.columns if "yuksek" in c.lower() or "high" in c.lower()), None)
    low_col = next((c for c in df.columns if "dusuk" in c.lower() or "low" in c.lower()), None)
    close_col = next(
        (c for c in df.columns if any(k in c.lower() for k in ("kapanis", "close", "deger", "value", index_code.lower()))),
        None,
    )

    if date_col is None or (close_col is None and (high_col is None or low_col is None)):
        raise ValueError(f"İş Yatırım verisi beklenmeyen formatta (sütunlar: {list(df.columns)}).")

    bars = []
    for _, row in df.iterrows():
        try:
            dt_str = _normalize_date_str(row[date_col])
            close_val = float(row[close_col]) if close_col else None
            high_val = float(row[high_col]) if high_col else close_val
            low_val = float(row[low_col]) if low_col else close_val
            if high_val is None or low_val is None:
                continue
            bars.append({"datetime": dt_str, "high": high_val, "low": low_val})
        except Exception:
            continue

    if not bars:
        raise ValueError(f"'{user_symbol}' için ayrıştırılabilir veri bulunamadı.")

    bars.sort(key=lambda b: b["datetime"])
    return bars[-outputsize:] if len(bars) > outputsize else bars


# ----------------------------------------------------------------------------
# ANA fetch_bars FONKSİYONU
# ----------------------------------------------------------------------------

# 1 ons = 31.1034768 gram. XAUTRYG (Gram Altın/TL) hiçbir sağlayıcıda tek bir
# sembol olarak bulunmuyor; (XAU/USD / 31.1034768) * USD/TRY formülüyle
# TÜRETİLİR.
GRAMS_PER_TROY_OUNCE = 31.1034768


def fetch_bars(user_symbol: str, interval: str, outputsize: int):
    """
    Sıralama:
      1) XAUTRYG -> XAUUSD ve USDTRY üzerinden TÜRETİLİR.
      2) XU100/XU030/XU500 -> doğrudan isyatirimhisse (İş Yatırım).
      3) XAGUSD, XPTUSD, XPDUSD, VIX, DXY -> Yahoo Finance (öncelikli)
      4) Diğer her şey -> önce Twelve Data; 'UPGRADE_REQUIRED' hatası
         alırsa Yahoo Finance denenir.
    """
    normalized_input = user_symbol.strip().upper().replace(" ", "")

    # 1) XAUTRYG (Gram Altın) türetme
    if normalized_input in ("XAUTRYG", "GRAMALTIN"):
        xau_bars = fetch_bars("XAUUSD", interval, outputsize)
        usdtry_bars = fetch_bars("USDTRY", interval, outputsize)
        usdtry_by_date = {b["datetime"]: b for b in usdtry_bars}

        result = []
        for xb in xau_bars:
            ub = usdtry_by_date.get(xb["datetime"])
            if ub is None:
                continue
            result.append({
                "datetime": xb["datetime"],
                "high": (float(xb["high"]) / GRAMS_PER_TROY_OUNCE) * float(ub["high"]),
                "low": (float(xb["low"]) / GRAMS_PER_TROY_OUNCE) * float(ub["low"]),
            })

        if not result:
            raise ValueError("XAUTRYG için XAUUSD ve USDTRY tarihleri eşleştirilemedi.")
        return result

    # 2) BIST endeksleri
    if normalized_input in BIST_INDEX_ALIASES:
        return fetch_bars_bist_index(user_symbol, interval, outputsize)

    # 3) Yahoo Finance öncelikli semboller (XAGUSD, XPTUSD, XPDUSD, VIX, DXY)
    if normalized_input in ["XAGUSD", "XPTUSD", "XPDUSD", "VIX", "DXY"]:
        return fetch_bars_yahoo(user_symbol, interval, outputsize)

    # 4) Twelve Data + yedek
    try:
        return fetch_bars_twelvedata(user_symbol, interval, outputsize)
    except ValueError as e:
        if str(e) == "UPGRADE_REQUIRED":
            logger.info(f"🔄 '{user_symbol}' Twelve Data ücretsiz planda kapalı, Yahoo Finance deneniyor...")
            try:
                return fetch_bars_yahoo(user_symbol, interval, outputsize)
            except Exception as yahoo_err:
                raise ValueError(
                    f"Twelve Data ücretsiz planda kapalı; Yahoo Finance de başarısız oldu: {yahoo_err}"
                )
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
        "*XPDUSD*, *EURUSD*, *DXY*, *VIX*, *XU100*, *XU030*, *XU500*, *XAUTRYG*).\n\n"
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
