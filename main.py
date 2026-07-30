"""
DENGE ARALIĞI TELEGRAM BOTU (4 Kaynaklı Hibrit Model)
============================================================================
1) Twelve Data      -> BTCUSD, XAUUSD, EURUSD gibi standart forex/kripto/emtia
2) isyatirimhisse    -> XU100, XU030, XU500 gibi BIST endeksleri
3) Yahoo Finance     -> XAGUSD, XPTUSD, XPDUSD, VIX, DXY (ANA YEDEK)
4) Stooq (son çare)  -> Twelve Data ve Yahoo başarısız olursa
"""

import os
import logging
import statistics
from collections import Counter
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import requests
import yfinance as yf
import pandas as pd
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


def _normalize_stooq_symbol(user_symbol: str) -> str:
    """Kullanıcı girdisini Stooq formatına çevirir."""
    s = user_symbol.strip().upper().replace(" ", "")
    
    # Özel durumlar
    special = {
        "VIX": "^vix",
        "VIXUSD": "^vix",
        "DXY": "usdx",
        "DXYUSD": "usdx",
        "XAGUSD": "xagusd",
        "XPTUSD": "xptusd",
        "XPDUSD": "xpdusd",
    }
    
    if s in special:
        return special[s]
    
    return s.replace("/", "").lower()


def _normalize_yahoo_symbol(user_symbol: str) -> str:
    """Kullanıcı girdisini Yahoo Finance formatına çevirir."""
    s = user_symbol.strip().upper().replace(" ", "")
    
    # Yahoo Finance sembol eşleştirmeleri
    yahoo_map = {
        "XAGUSD": "SI=F",      # Gümüş vadeli
        "XPTUSD": "PL=F",      # Platin vadeli
        "XPDUSD": "PA=F",      # Paladyum vadeli
        "VIX": "^VIX",         # VIX endeksi
        "VIXUSD": "^VIX",
        "DXY": "DX-Y.NYB",     # Dolar endeksi
        "DXYUSD": "DX-Y.NYB",
        "BTCUSD": "BTC-USD",
        "ETHUSD": "ETH-USD",
        "EURUSD": "EURUSD=X",
        "GBPUSD": "GBPUSD=X",
        "USDTRY": "TRY=X",
        "XAUUSD": "GC=F",      # Altın vadeli
    }
    
    if s in yahoo_map:
        return yahoo_map[s]
    
    # Genel format: sembolü olduğu gibi dene
    return s


# ----------------------------------------------------------------------------
# VERİ ÇEKME - ANA KAYNAK: Twelve Data
# ----------------------------------------------------------------------------

def fetch_bars_twelvedata(user_symbol: str, interval: str, outputsize: int):
    """Twelve Data'dan belirtilen aralıkta son `outputsize` mumu çeker."""
    if not TWELVE_DATA_API_KEY:
        raise ValueError("TWELVE_DATA_API_KEY tanımlı değil.")

    symbol = normalize_symbol(user_symbol)
    params = {
        "symbol": symbol,
        "interval": interval,
        "outputsize": outputsize,
        "apikey": TWELVE_DATA_API_KEY,
    }

    logger.info(f"📊 Twelve Data çağrısı: {symbol} {interval}")
    
    try:
        resp = requests.get(TWELVE_DATA_URL, params=params, timeout=15)
        data = resp.json()
    except Exception as e:
        raise ValueError(f"Twelve Data bağlantı hatası: {e}")

    if isinstance(data, dict) and data.get("status") == "error":
        msg = data.get("message", "")
        if any(word in msg.lower() for word in ["plan", "grow", "venture", "available starting"]):
            raise ValueError("UPGRADE_REQUIRED")
        raise ValueError(msg)

    if "values" not in data:
        raise ValueError(f"'{symbol}' için veri bulunamadı.")

    logger.info(f"✅ Twelve Data'dan {len(data['values'])} veri alındı")
    return data["values"]


# ----------------------------------------------------------------------------
# VERİ ÇEKME - YAHOO FINANCE (ANA YEDEK)
# ----------------------------------------------------------------------------

def fetch_bars_yahoo(user_symbol: str, interval: str, outputsize: int):
    """
    Yahoo Finance'den veri çeker.
    Özellikle XAGUSD, XPTUSD, XPDUSD, VIX, DXY için kullanılır.
    """
    yahoo_symbol = _normalize_yahoo_symbol(user_symbol)
    
    # Interval dönüşümü
    interval_map = {
        "4h": "60m",      # 4 saat -> 60 dakika
        "1day": "1d",
        "1week": "1wk",
    }
    
    yf_interval = interval_map.get(interval, "1d")
    
    # 4 saatlik için yeterli veri al
    if interval == "4h":
        period = "5d"  # Son 5 gün
    else:
        # outputsize kadar gün/ hafta
        period = f"{outputsize * 2}d" if interval == "1day" else f"{outputsize * 2}wk"
    
    logger.info(f"📊 Yahoo Finance çağrısı: {yahoo_symbol} {yf_interval}")
    
    try:
        ticker = yf.Ticker(yahoo_symbol)
        df = ticker.history(period=period, interval=yf_interval)
        
        if df.empty:
            raise ValueError(f"Yahoo Finance: {user_symbol} için veri yok")
        
        # Veriyi formatla
        bars = []
        for idx, row in df.iterrows():
            bars.append({
                "datetime": idx.strftime("%Y-%m-%d %H:%M:%S"),
                "high": float(row['High']),
                "low": float(row['Low']),
            })
        
        # Son outputsize kadarını al
        result = bars[-outputsize:] if len(bars) > outputsize else bars
        
        if not result:
            raise ValueError("Veri boş geldi")
            
        logger.info(f"✅ Yahoo Finance'dan {len(result)} veri alındı")
        return result
        
    except Exception as e:
        raise ValueError(f"Yahoo Finance hatası: {e}")


# ----------------------------------------------------------------------------
# VERİ ÇEKME - YEDEK KAYNAK: Stooq (SON ÇARE)
# ----------------------------------------------------------------------------

def fetch_bars_stooq(user_symbol: str, interval: str, outputsize: int):
    """
    Son çare yedek kaynak. Yahoo Finance de çalışmazsa dene.
    """
    if interval == "4h":
        raise ValueError("Stooq kaynağında 4 saatlik veri yok.")

    stooq_interval = "w" if interval == "1week" else "d"
    symbol = _normalize_stooq_symbol(user_symbol)
    
    # Stooq URL'leri
    url_templates = [
        f"https://stooq.com/q/d/l/?s={{symbol}}&i={{interval}}",
        f"https://stooq.com/q/d/l/?s={{symbol}}.US&i={{interval}}",
    ]
    
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
    }
    
    for url_template in url_templates:
        try:
            url = url_template.format(symbol=symbol, interval=stooq_interval)
            logger.info(f"🔄 Stooq deneniyor: {url}")
            
            resp = requests.get(url, headers=headers, timeout=15)
            
            if resp.status_code != 200:
                continue
            
            text = resp.text.strip()
            if not text or "404" in text:
                continue
            
            lines_ = text.splitlines()
            if not lines_ or "Date" not in lines_[0]:
                continue
            
            # CSV parse
            header = [col.strip() for col in lines_[0].split(",")]
            date_idx = 0
            high_idx = 2
            low_idx = 3
            
            for idx, col in enumerate(header):
                col_lower = col.lower()
                if col_lower == "date":
                    date_idx = idx
                elif col_lower == "high":
                    high_idx = idx
                elif col_lower == "low":
                    low_idx = idx
            
            bars = []
            for line in lines_[1:]:
                if not line.strip():
                    continue
                    
                parts = [p.strip() for p in line.split(",")]
                if len(parts) <= max(date_idx, high_idx, low_idx):
                    continue
                
                try:
                    date_str = parts[date_idx]
                    if not date_str or len(date_str) < 8:
                        continue
                    
                    high_val = float(parts[high_idx]) if parts[high_idx] else 0
                    low_val = float(parts[low_idx]) if parts[low_idx] else 0
                    
                    if high_val > 0 and low_val > 0:
                        bars.append({
                            "datetime": date_str,
                            "high": high_val,
                            "low": low_val,
                        })
                except (ValueError, IndexError):
                    continue
            
            if bars:
                logger.info(f"✅ Stooq'dan {len(bars)} veri alındı")
                return bars[-outputsize:] if len(bars) > outputsize else bars
                
        except Exception as e:
            logger.warning(f"⚠️ Stooq hatası: {e}")
            continue
    
    raise ValueError(f"Stooq: '{user_symbol}' için veri alınamadı")


# ----------------------------------------------------------------------------
# BIST ENDEKSLERİ: isyatirimhisse
# ----------------------------------------------------------------------------

BIST_INDEX_ALIASES = {
    "XU100": "XU100", "BIST100": "XU100",
    "XU030": "XU030", "XU30": "XU030", "BIST30": "XU030",
    "XU500": "XU500", "BIST500": "XU500",
}


def _normalize_date_str(raw) -> str:
    raw = str(raw).strip()
    if len(raw) >= 10 and raw[4] == "-" and raw[7] == "-":
        return raw[:10]
    for sep in ("-", ".", "/"):
        parts = raw.split(sep)
        if len(parts) == 3:
            if len(parts[2]) == 4:
                gg, aa, yyyy = parts
                return f"{yyyy}-{aa.zfill(2)}-{gg.zfill(2)}"
            if len(parts[0]) == 4:
                yyyy, aa, gg = parts
                return f"{yyyy}-{aa.zfill(2)}-{gg.zfill(2)}"
    raise ValueError(f"Tarih formatı tanınamadı: {raw}")


def fetch_bars_bist_index(user_symbol: str, interval: str, outputsize: int):
    if interval == "4h":
        raise ValueError("İş Yatırım kaynağında 4 saatlik veri yok.")

    if isyatirimhisse is None:
        raise ValueError("isyatirimhisse kütüphanesi kurulu değil.")

    index_code = BIST_INDEX_ALIASES[user_symbol.strip().upper().replace(" ", "")]

    today = datetime.now(TR_TZ).date()
    start = today - timedelta(days=800)

    logger.info(f"📊 İş Yatırım çağrısı: {index_code}")
    
    try:
        df = isyatirimhisse.fetch_index_data(
            indices=index_code,
            start_date=start.strftime("%d-%m-%Y"),
            end_date=today.strftime("%d-%m-%Y"),
        )
    except Exception as e:
        raise ValueError(f"İş Yatırım hatası: {e}")

    if df is None or df.empty:
        raise ValueError(f"İş Yatırım'dan '{user_symbol}' için veri alınamadı.")

    date_col = next((c for c in df.columns if "tarih" in c.lower() or "date" in c.lower()), None)
    high_col = next((c for c in df.columns if "yuksek" in c.lower() or "high" in c.lower()), None)
    low_col = next((c for c in df.columns if "dusuk" in c.lower() or "low" in c.lower()), None)
    close_col = next(
        (c for c in df.columns if any(k in c.lower() for k in ("kapanis", "close", "deger", "value"))),
        None,
    )

    if date_col is None:
        raise ValueError(f"İş Yatırım verisi beklenmeyen formatta.")

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
    logger.info(f"✅ İş Yatırım'dan {len(bars)} veri alındı")
    return bars[-outputsize:] if len(bars) > outputsize else bars


# ----------------------------------------------------------------------------
# ANA fetch_bars FONKSİYONU (ÇOK KAYNAKLI)
# ----------------------------------------------------------------------------

GRAMS_PER_TROY_OUNCE = 31.1034768

# Yahoo Finance'in desteklediği semboller (Twelve Data'da kapalı olanlar)
YAHOO_PRIMARY_SYMBOLS = {
    "XAGUSD", "XPTUSD", "XPDUSD", "VIX", "DXY"
}


def fetch_bars(user_symbol: str, interval: str, outputsize: int):
    """
    Sıralama:
      1) XAUTRYG -> XAUUSD ve USDTRY üzerinden TÜRETİLİR.
      2) XU100/XU030/XU500 -> isyatirimhisse
      3) XAGUSD, XPTUSD, XPDUSD, VIX, DXY -> Yahoo Finance (önce)
      4) Diğer her şey -> Twelve Data
      5) Twelve Data başarısız -> Yahoo Finance
      6) Yahoo Finance başarısız -> Stooq
    """
    normalized_input = user_symbol.strip().upper().replace(" ", "")

    # 1) XAUTRYG (Gram Altın) türetme
    if normalized_input in ("XAUTRYG", "GRAMALTIN"):
        logger.info(f"🔄 {user_symbol} türetiliyor (XAUUSD + USDTRY)")
        
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

    # 3) Yahoo Finance öncelikli semboller (Twelve Data'da kapalı olanlar)
    if normalized_input in YAHOO_PRIMARY_SYMBOLS:
        try:
            logger.info(f"📊 {user_symbol} için Yahoo Finance kullanılıyor (öncelikli)")
            return fetch_bars_yahoo(user_symbol, interval, outputsize)
        except Exception as e:
            logger.warning(f"⚠️ Yahoo Finance başarısız, Stooq deneniyor: {e}")
            return fetch_bars_stooq(user_symbol, interval, outputsize)

    # 4) Twelve Data + yedekler
    try:
        return fetch_bars_twelvedata(user_symbol, interval, outputsize)
    except ValueError as e:
        if str(e) == "UPGRADE_REQUIRED":
            logger.info(f"🔄 '{user_symbol}' Twelve Data'da kapalı, Yahoo Finance deneniyor...")
            try:
                return fetch_bars_yahoo(user_symbol, interval, outputsize)
            except Exception as yahoo_err:
                logger.warning(f"⚠️ Yahoo Finance başarısız: {yahoo_err}")
                try:
                    logger.info(f"🔄 Stooq deneniyor...")
                    return fetch_bars_stooq(user_symbol, interval, outputsize)
                except Exception as stooq_err:
                    raise ValueError(
                        f"Tüm kaynaklar başarısız:\n"
                        f"Twelve Data: {str(e)}\n"
                        f"Yahoo Finance: {yahoo_err}\n"
                        f"Stooq: {stooq_err}"
                    )
        raise


# ----------------------------------------------------------------------------
# DÖNEM SINIRLARI VE HESAPLAMA (ÖNCEKİYLE AYNI)
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
        uyari = "⚠️ Bu dönem çok oynak; Destek 2 matematiksel olarak negatif çıktı."
    elif destek1 < 0:
        uyari = "⚠️ Bu dönem çok oynak; Destek 1 matematiksel olarak negatif çıktı."

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

    # 4 Saatlik
    try:
        four_hour_bars = fetch_bars(user_symbol, "4h", FOUR_HOUR_OUTPUTSIZE)
        four_hour_bars = _filter_weekend_bars_if_not_crypto(four_hour_bars, user_symbol)
        last_bar = max(four_hour_bars, key=_parse_bar_datetime) if four_hour_bars else None
        if last_bar is None:
            raise ValueError("Yeterli 4 saatlik veri bulunamadı.")
        results["4 Saatlik"] = _levels_from_bars([last_bar], birim="adet 4 saatlik mum")
    except Exception as e:
        logger.error(f"4 Saatlik hatası: {e}")
        results["4 Saatlik"] = {"hata": str(e)}

    # Günlük, Haftalık, Aylık
    try:
        short_size = _compute_short_daily_outputsize(today)
        daily_bars = fetch_bars(user_symbol, "1day", short_size)
        daily_bars = _filter_weekend_bars_if_not_crypto(daily_bars, user_symbol)
    except Exception as e:
        logger.error(f"Günlük veri hatası: {e}")
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
            logger.error(f"Günlük hesaplama hatası: {e}")
            results["Günlük"] = {"hata": str(e)}

        try:
            is_crypto = _is_crypto_symbol(user_symbol)
            start, end = get_last_completed_week_range(today, is_crypto=is_crypto)
            results["Haftalık"] = _levels_from_bars(_filter_by_range(daily_bars, start, end), birim="gün")
        except Exception as e:
            logger.error(f"Haftalık hatası: {e}")
            results["Haftalık"] = {"hata": str(e)}

        try:
            start, end = get_last_completed_month_range(today)
            results["Aylık"] = _levels_from_bars(_filter_by_range(daily_bars, start, end), birim="gün")
        except Exception as e:
            logger.error(f"Aylık hatası: {e}")
            results["Aylık"] = {"hata": str(e)}

    # 6 Aylık, Yıllık
    try:
        long_size = _compute_long_weekly_outputsize(today)
        weekly_bars = fetch_bars(user_symbol, "1week", long_size)
    except Exception as e:
        logger.error(f"Haftalık veri hatası: {e}")
        error = {"hata": str(e)}
        results["6 Aylık"] = error
        results["Yıllık"] = error
        weekly_bars = None

    if weekly_bars is not None:
        try:
            start, end = get_last_completed_half_year_range(today)
            results["6 Aylık"] = _levels_from_bars(_filter_by_range(weekly_bars, start, end), birim="hafta")
        except Exception as e:
            logger.error(f"6 Aylık hatası: {e}")
            results["6 Aylık"] = {"hata": str(e)}

        try:
            start, end = get_last_completed_year_range(today)
            results["Yıllık"] = _levels_from_bars(_filter_by_range(weekly_bars, start, end), birim="hafta")
        except Exception as e:
            logger.error(f"Yıllık hatası: {e}")
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
        "Bana bir enstrüman kodu gönder:\n"
        "• *BTCUSD*, *XAUUSD*, *EURUSD* (Twelve Data)\n"
        "• *XAGUSD*, *XPTUSD*, *XPDUSD*, *VIX*, *DXY* (Yahoo Finance)\n"
        "• *XU100*, *XU030*, *XU500* (BIST - İş Yatırım)\n"
        "• *XAUTRYG* (Gram Altın - Türetilmiş)\n\n"
        "🕐 Günlük  📅 Haftalık  🗓️ Aylık  📈 6 Aylık  🏆 Yıllık\n"
        "için Denge, Direnç ve Destek seviyelerini hesaplar.\n\n"
        "_Yalnızca TAMAMLANMIŞ (kapanmış) son periyot kullanılır._",
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

    try:
        results = calculate_all_periods(user_symbol)
    except Exception as e:
        await processing_msg.edit_text(f"❌ Hata: {str(e)}")
        return

    separator = "━" * 24
    blocks = [f"💰 *{user_symbol.upper()}*", separator, ""]
    for i, period_name in enumerate(PERIOD_NAMES):
        blocks.append(format_period_block(period_name, results.get(period_name, {"hata": "sonuç yok"})))
        if i < len(PERIOD_NAM
