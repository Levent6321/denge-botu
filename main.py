"""
DENGE ARALIĞI TELEGRAM BOTU (2 Kaynaklı Hibrit Model - Stooq Kaldırıldı)
============================================================================
1) Twelve Data      -> BTCUSD, XAUUSD, EURUSD gibi standart forex/kripto/emtia
2) isyatirimhisse    -> XU100, XU030, XU500 gibi BIST endeksleri (İş Yatırım)
3) MetalpriceAPI     -> Twelve Data'da kapalı olan XAGUSD, XPTUSD, XPDUSD için 
4) yfinance (yedek)  -> Son çare olarak (PA=F, SI=F gibi vadeli işlemler)
"""

import os
import time
import logging
import statistics
from collections import Counter
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import requests
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes

try:
    import isyatirimhisse
except ImportError:
    isyatirimhisse = None

try:
    import yfinance as yf
except ImportError:
    yf = None

# ----------------------------------------------------------------------------
# AYARLAR
# ----------------------------------------------------------------------------

load_dotenv()

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TWELVE_DATA_API_KEY = os.getenv("TWELVE_DATA_API_KEY")
METALPRICEAPI_KEY = os.getenv("METALPRICEAPI_KEY")

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
# BASİT BELLEK-İÇİ ÖNBELLEK
# ----------------------------------------------------------------------------
_CACHE: dict[str, tuple[float, object]] = {}
_CACHE_TTL_SECONDS = 900

def _cached_fetch(cache_key: str, fetch_fn):
    now = time.time()
    cached = _CACHE.get(cache_key)
    if cached is not None:
        ts, data = cached
        if now - ts < _CACHE_TTL_SECONDS:
            return data
    data = fetch_fn()
    _CACHE[cache_key] = (now, data)
    return data

def _is_rate_limit_text(text: str) -> bool:
    lowered = text.lower()
    return any(phrase in lowered for phrase in [
        "too many requests", "rate limit", "rate-limited", "429",
    ])

# ----------------------------------------------------------------------------
# SEMBOL NORMALİZASYON
# ----------------------------------------------------------------------------

def normalize_symbol(user_symbol: str) -> str:
    s = user_symbol.strip().upper().replace(" ", "")
    if "/" in s:
        return s
    if len(s) > 3:
        base, quote = s[:-3], s[-3:]
        return f"{base}/{quote}"
    return s

# ----------------------------------------------------------------------------
# VERİ ÇEKME - ANA KAYNAK: Twelve Data
# ----------------------------------------------------------------------------

def fetch_bars_twelvedata(user_symbol: str, interval: str, outputsize: int):
    if not TWELVE_DATA_API_KEY:
        raise ValueError("TWELVE_DATA_API_KEY tanımlı değil.")
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
        if any(word in msg.lower() for word in [
            "plan", "grow", "venture", "available starting", "not found"
        ]):
            raise ValueError("UPGRADE_REQUIRED")
        raise ValueError(msg)
    if "values" not in data:
        raise ValueError(f"'{symbol}' için veri bulunamadı.")
    return data["values"]

# ----------------------------------------------------------------------------
# VERİ ÇEKME - BIST ENDEKSLERİ
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
        (c for c in df.columns if any(k in c.lower() for k in ("kapanis", "close", "deger", "value", index_code.lower()))), None)
    if date_col is None or (close_col is None and (high_col is None or low_col is None)):
        raise ValueError(f"İş Yatırım verisi beklenmeyen formatta.")
    bars = []
    for _, row in df.iterrows():
        try:
            dt_str = _normalize_date_str(row[date_col])
            close_val = float(row[close_col]) if close_col else None
            high_val = float(row[high_col]) if high_col else close_val
            low_val = float(row[low_col]) if low_col else close_val
            if high_val is None or low_val is None: continue
            bars.append({
                "datetime": dt_str,
                "high": high_val,
                "low": low_val,
                "close": close_val if close_val is not None else (high_val + low_val) / 2,
            })
        except Exception: continue
    if not bars: raise ValueError(f"'{user_symbol}' için ayrıştırılabilir veri bulunamadı.")
    bars.sort(key=lambda b: b["datetime"])
    return bars[-outputsize:] if len(bars) > outputsize else bars

# ----------------------------------------------------------------------------
# VERİ ÇEKME - YEDEK KAYNAK: MetalpriceAPI
# ----------------------------------------------------------------------------

METALPRICEAPI_URL = "https://api.metalpriceapi.com/v1/timeframe"
METALPRICEAPI_SYMBOL_MAP = {"XAGUSD": "XAG", "XPTUSD": "XPT", "XPDUSD": "XPD"}

def _fetch_bars_metalpriceapi_uncached(user_symbol: str, interval: str, outputsize: int):
    if interval == "4h": raise ValueError("MetalpriceAPI kaynağında 4 saatlik veri yok.")
    if not METALPRICEAPI_KEY: raise ValueError("METALPRICEAPI_KEY tanımlı değil.")
    normalized_input = user_symbol.strip().upper().replace(" ", "")
    metal_code = METALPRICEAPI_SYMBOL_MAP.get(normalized_input)
    if metal_code is None: raise ValueError(f"MetalpriceAPI '{user_symbol}' sembolünü desteklemiyor.")
    today = datetime.now(TR_TZ).date()
    days_needed = min(outputsize * 7 + 14, 30)
    start_date = today - timedelta(days=days_needed)
    end_date = today - timedelta(days=1)
    params = {
        "api_key": METALPRICEAPI_KEY,
        "start_date": start_date.isoformat(),
        "end_date": end_date.isoformat(),
        "base": "USD",
        "currencies": metal_code,
    }
    try:
        resp = requests.get(METALPRICEAPI_URL, params=params, timeout=15)
        data = resp.json()
    except Exception as e:
        raise ValueError(f"MetalpriceAPI'ye bağlanılamadı: {e}")
    if not data.get("success"):
        err = data.get("error", {})
        raise ValueError(f"MetalpriceAPI hata: {err.get('info', data)}")
    usd_key = f"USD{metal_code}"
    daily_bars = []
    for date_str, values in sorted((data.get("rates") or {}).items()):
        price = values.get(usd_key)
        if price is None: continue
        price = float(price)
        daily_bars.append({"datetime": date_str, "high": price, "low": price, "close": price})
    if not daily_bars: raise ValueError(f"MetalpriceAPI: '{user_symbol}' için veri bulunamadı.")
    if interval == "1week":
        weekly_bars = _aggregate_daily_to_weekly(daily_bars)
        return weekly_bars[-outputsize:] if len(weekly_bars) > outputsize else weekly_bars
    return daily_bars[-outputsize:] if len(daily_bars) > outputsize else daily_bars

def fetch_bars_metalpriceapi(user_symbol: str, interval: str, outputsize: int):
    cache_key = f"metalpriceapi:{user_symbol.strip().upper()}:{interval}:{outputsize}"
    return _cached_fetch(cache_key, lambda: _fetch_bars_metalpriceapi_uncached(user_symbol, interval, outputsize))

# ----------------------------------------------------------------------------
# VERİ ÇEKME - 2. YEDEK KAYNAK: Yahoo Finance (yfinance)
# ----------------------------------------------------------------------------

YFINANCE_INTERVAL_MAP = {"1day": "1d", "1week": "1wk"}
YFINANCE_FUTURES_FALLBACK = {"XAGUSD": "SI=F", "XPTUSD": "PL=F", "XPDUSD": "PA=F"}

def _normalize_yfinance_symbol(user_symbol: str) -> str:
    s = user_symbol.strip().upper().replace(" ", "").replace("/", "")
    if len(s) > 3:
        base, quote = s[:-3], s[-3:]
        if base in CRYPTO_BASES: return f"{base}-{quote}"
    return f"{s}=X"

def _fetch_bars_yfinance_uncached(user_symbol: str, interval: str, outputsize: int):
    if interval not in YFINANCE_INTERVAL_MAP: raise ValueError("4 saatlik desteklenmiyor.")
    if yf is None: raise ValueError("yfinance kütüphanesi kurulu değil.")
    yf_interval = YFINANCE_INTERVAL_MAP[interval]
    normalized_input = user_symbol.strip().upper().replace(" ", "")
    candidates = [_normalize_yfinance_symbol(user_symbol)]
    if normalized_input in YFINANCE_FUTURES_FALLBACK:
        candidates.append(YFINANCE_FUTURES_FALLBACK[normalized_input])
    days_multiplier = 7 if yf_interval == "1wk" else 2
    period_days = min(outputsize * days_multiplier + 30, 3650)
    last_err = None
    rate_limited = False
    max_attempts = 3
    for candidate in candidates:
        for attempt in range(max_attempts):
            try:
                time.sleep(1.5) # Rate-limit koruması
                df = yf.Ticker(candidate).history(
                    period=f"{period_days}d",
                    interval=yf_interval,
                    auto_adjust=False,
                )
                if df is None or df.empty:
                    last_err = ValueError(f"'{candidate}' için veri dönmedi."); break
                bars = []
                for idx, row in df.iterrows():
                    try:
                        bars.append({
                            "datetime": idx.strftime("%Y-%m-%d"),
                            "high": float(row["High"]),
                            "low": float(row["Low"]),
                            "close": float(row["Close"]),
                        })
                    except (TypeError, ValueError): continue
                if not bars:
                    last_err = ValueError(f"'{candidate}' için ayrıştırılabilir veri yok."); break
                return bars[-outputsize:] if len(bars) > outputsize else bars
            except Exception as e:
                last_err = e
                if _is_rate_limit_text(str(e)):
                    rate_limited = True
                    time.sleep(1.5 * (attempt + 1))
                    continue
                break
    if rate_limited:
        raise ValueError("Yahoo Finance şu anda istek limiti uyguluyor (Too Many Requests). Lütfen birkaç dakika sonra tekrar deneyin.")
    raise ValueError(f"yfinance: '{user_symbol}' için veri alınamadı ({last_err}).")

def fetch_bars_yfinance(user_symbol: str, interval: str, outputsize: int):
    cache_key = f"yfinance:{user_symbol.strip().upper()}:{interval}:{outputsize}"
    return _cached_fetch(cache_key, lambda: _fetch_bars_yfinance_uncached(user_symbol, interval, outputsize))

# ----------------------------------------------------------------------------
# ANA fetch_bars FONKSİYONU
# ----------------------------------------------------------------------------
GRAMS_PER_TROY_OUNCE = 31.1034768

def fetch_bars(user_symbol: str, interval: str, outputsize: int):
    normalized_input = user_symbol.strip().upper().replace(" ", "")
    if normalized_input in ("XAUTRYG", "GRAMALTIN"):
        xau_bars = fetch_bars("XAUUSD", interval, outputsize)
        usdtry_bars = fetch_bars("USDTRY", interval, outputsize)
        usdtry_by_date = {b["datetime"]: b for b in usdtry_bars}
        result = []
        for xb in xau_bars:
            ub = usdtry_by_date.get(xb["datetime"])
            if ub is None: continue
            xb_close = xb.get("close", (float(xb["high"]) + float(xb["low"])) / 2)
            ub_close = ub.get("close", (float(ub["high"]) + float(ub["low"])) / 2)
            result.append({
                "datetime": xb["datetime"],
                "high": (float(xb["high"]) / GRAMS_PER_TROY_OUNCE) * float(ub["high"]),
                "low": (float(xb["low"]) / GRAMS_PER_TROY_OUNCE) * float(ub["low"]),
                "close": (float(xb_close) / GRAMS_PER_TROY_OUNCE) * float(ub_close),
            })
        if not result: raise ValueError("XAUTRYG için tarihler eşleştirilemedi.")
        return result
    if normalized_input in BIST_INDEX_ALIASES:
        return fetch_bars_bist_index(user_symbol, interval, outputsize)
    try:
        return fetch_bars_twelvedata(user_symbol, interval, outputsize)
    except ValueError as e:
        if str(e) != "UPGRADE_REQUIRED": raise
    logger.info(f"🔄 '{user_symbol}' Twelve Data kapalı, yedekler deneniyor...")
    fallback_sources = []
    if normalized_input in METALPRICEAPI_SYMBOL_MAP and METALPRICEAPI_KEY:
        fallback_sources.append(("MetalpriceAPI", fetch_bars_metalpriceapi))
    fallback_sources.append(("yfinance", fetch_bars_yfinance))
    errors = []
    for name, fetch_fn in fallback_sources:
        try:
            return fetch_fn(user_symbol, interval, outputsize)
        except Exception as source_err:
            logger.info(f"🔄 '{user_symbol}' {name} başarısız oldu... ({source_err})")
            errors.append(f"{name} ({source_err})")
    raise ValueError(f"Twelve Data kapalı; " + "; ".join(errors) + " denemeleri başarısız oldu.")

# ----------------------------------------------------------------------------
# DÖNEM SINIRLARI (Geçmiş ve Gelecek Hesaplamaları)
# ----------------------------------------------------------------------------
def get_last_completed_week_range(today: date, is_crypto: bool = False):
    weekday = today.weekday()
    if is_crypto:
        this_monday = today - timedelta(days=weekday)
        last_monday = this_monday - timedelta(days=7)
        last_sunday = last_monday + timedelta(days=6)
        return last_monday, last_sunday
    if weekday >= 5: week_monday = today - timedelta(days=weekday)
    else: week_monday = today - timedelta(days=weekday) - timedelta(days=7)
    week_friday = week_monday + timedelta(days=4)
    return week_monday, week_friday

def get_last_completed_month_range(today: date):
    first_of_this_month = today.replace(day=1)
    last_day_prev_month = first_of_this_month - timedelta(days=1)
    first_day_prev_month = last_day_prev_month.replace(day=1)
    return first_day_prev_month, last_day_prev_month

def get_last_completed_half_year_range(today: date):
    year = today.year
    if today.month <= 6: return date(year - 1, 7, 1), date(year - 1, 12, 31)
    return date(year, 1, 1), date(year, 6, 30)

def get_last_completed_year_range(today: date):
    last_year = today.year - 1
    return date(last_year, 1, 1), date(last_year, 12, 31)

def get_current_week_range(today: date, is_crypto: bool = False):
    weekday = today.weekday()
    if is_crypto:
        monday = today - timedelta(days=weekday)
        sunday = monday + timedelta(days=6)
        return monday, sunday
    if weekday >= 5:
        next_monday = today - timedelta(days=weekday) + timedelta(days=7)
        next_friday = next_monday + timedelta(days=4)
        return next_monday, next_friday
    this_monday = today - timedelta(days=weekday)
    this_friday = this_monday + timedelta(days=4)
    return this_monday, this_friday

def _next_trading_day(today: date, is_crypto: bool = False) -> date:
    if is_crypto: return today
    d = today
    while d.weekday() >= 5: d += timedelta(days=1)
    return d

def get_current_month_range(today: date):
    first_of_month = today.replace(day=1)
    if today.month == 12: next_month_first = date(today.year + 1, 1, 1)
    else: next_month_first = date(today.year, today.month + 1, 1)
    last_of_month = next_month_first - timedelta(days=1)
    return first_of_month, last_of_month

def get_current_half_year_range(today: date):
    year = today.year
    if today.month <= 6: return date(year, 1, 1), date(year, 6, 30)
    return date(year, 7, 1), date(year, 12, 31)

def get_current_year_range(today: date):
    return date(today.year, 1, 1), date(today.year, 12, 31)

# ----------------------------------------------------------------------------
# HESAPLAMA VE BİRLEŞTİRME
# ----------------------------------------------------------------------------
def _parse_bar_date(bar: dict) -> date:
    return datetime.strptime(bar["datetime"][:10], "%Y-%m-%d").date()

def _parse_bar_datetime(bar: dict) -> datetime:
    raw = bar["datetime"]
    if len(raw) > 10: return datetime.strptime(raw, "%Y-%m-%d %H:%M:%S")
    return datetime.strptime(raw, "%Y-%m-%d")

def _filter_by_range(bars, start: date, end: date):
    return [b for b in bars if start <= _parse_bar_date(b) <= end]

def _bar_close(bar: dict) -> float:
    close_val = bar.get("close")
    if close_val is not None:
        try: return float(close_val)
        except: pass
    return (float(bar["high"]) + float(bar["low"])) / 2

def _aggregate_daily_to_weekly(daily_bars):
    weeks = {}
    for bar in sorted(daily_bars, key=lambda b: b["datetime"]):
        d = datetime.strptime(bar["datetime"][:10], "%Y-%m-%d").date()
        monday = d - timedelta(days=d.weekday())
        key = monday.isoformat()
        h = float(bar["high"]); l = float(bar["low"]); c = float(bar.get("close", h))
        if key not in weeks: weeks[key] = {"high": h, "low": l, "close": c, "last_date": d}
        else:
            w = weeks[key]
            w["high"] = max(w["high"], h)
            w["low"] = min(w["low"], l)
            if d >= w["last_date"]: w["close"] = c; w["last_date"] = d
    result = []
    for key in sorted(weeks.keys()):
        w = weeks[key]
        result.append({"datetime": w["last_date"].isoformat(), "high": w["high"], "low": w["low"], "close": w["close"]})
    return result

CRYPTO_BASES = {"BTC", "ETH", "XRP", "LTC", "BCH", "ADA", "SOL", "DOGE", "DOT", "MATIC", "BNB", "AVAX", "LINK", "TRX", "SHIB", "ATOM", "UNI", "XLM", "ETC", "FIL", "APT", "ARB", "OP", "NEAR", "ICP", "AAVE", "SAND", "MANA", "ALGO", "VET"}

def _is_crypto_symbol(user_symbol: str) -> bool:
    normalized = normalize_symbol(user_symbol)
    base = normalized.split("/")[0].upper()
    return base in CRYPTO_BASES

def _filter_weekend_bars_if_not_crypto(bars, user_symbol: str):
    if _is_crypto_symbol(user_symbol): return bars
    return [b for b in bars if _parse_bar_date(b).weekday() < 5]

def _last_completed_day_bars(bars, today: date):
    completed = [b for b in bars if _parse_bar_date(b) < today]
    if not completed: return []
    latest = max(_parse_bar_date(b) for b in completed)
    return [b for b in completed if _parse_bar_date(b) == latest]

def _levels_from_bars(bars, birim: str = "gün") -> dict:
    if not bars: raise ValueError("bu dönem henüz tamamlanmamış veya veri yok")
    values = []
    for bar in bars:
        h = round(float(bar["high"]), 4)
        l = round(float(bar["low"]), 4)
        values.append(h)
        if l != h: values.append(l)
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
    if destek2 < 0: uyari = "⚠️ Dönem çok oynak; Destek 2 matematiksel olarak negatif çıktı."
    elif destek1 < 0: uyari = "⚠️ Dönem çok oynak; Destek 1 matematiksel olarak negatif çıktı."
    return {"denge": denge, "ortalama": ortalama, "direnc1": denge + half_range, "direnc2": denge + range_, "destek1": destek1, "destek2": destek2, "range": range_, "mod": mods, "adet": len(bars), "birim": birim, "baslangic": dates[0].isoformat(), "bitis": dates[-1].isoformat(), "uyari": uyari}

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
    is_crypto = _is_crypto_symbol(user_symbol)
    hedef_gun = _next_trading_day(today, is_crypto)
    results = {}
    guncel_fiyat = None
    guncel_fiyat_zaman = None

    # --- 4 Saatlik ---
    try:
        four_hour_bars_raw = fetch_bars(user_symbol, "4h", FOUR_HOUR_OUTPUTSIZE)
        four_hour_bars = _filter_weekend_bars_if_not_crypto(four_hour_bars_raw, user_symbol)
        last_bar = max(four_hour_bars, key=_parse_bar_datetime) if four_hour_bars else None
        if last_bar is None: raise ValueError("Yeterli 4 saatlik veri bulunamadı.")
        results["4 Saatlik"] = _levels_from_bars([last_bar], birim="adet 4 saatlik mum")
        results["4 Saatlik"]["baslangic"] = hedef_gun.isoformat()
        results["4 Saatlik"]["bitis"] = hedef_gun.isoformat()
        guncel_fiyat = _bar_close(last_bar)
        guncel_fiyat_zaman = _parse_bar_datetime(last_bar)
    except Exception as e: results["4 Saatlik"] = {"hata": str(e)}

    # --- Günlük, Haftalık, Aylık ---
    try:
        short_size = _compute_short_daily_outputsize(today)
        daily_bars = fetch_bars(user_symbol, "1day", short_size)
        daily_bars = _filter_weekend_bars_if_not_crypto(daily_bars, user_symbol)
    except Exception as e:
        error = {"hata": str(e)}
        results["Günlük"] = error; results["Haftalık"] = error; results["Aylık"] = error
        daily_bars = None

    if daily_bars is not None:
        if guncel_fiyat is None:
            latest_daily_bar = max(daily_bars, key=_parse_bar_date) if daily_bars else None
            if latest_daily_bar is not None:
                guncel_fiyat = _bar_close(latest_daily_bar)
                guncel_fiyat_zaman = _parse_bar_datetime(latest_daily_bar)
        try:
            bars = _last_completed_day_bars(daily_bars, today)
            results["Günlük"] = _levels_from_bars(bars, birim="gün")
            results["Günlük"]["baslangic"] = hedef_gun.isoformat()
            results["Günlük"]["bitis"] = hedef_gun.isoformat()
        except Exception as e: results["Günlük"] = {"hata": str(e)}
        try:
            start, end = get_last_completed_week_range(today, is_crypto=is_crypto)
            results["Haftalık"] = _levels_from_bars(_filter_by_range(daily_bars, start, end), birim="gün")
            hedef_start, hedef_end = get_current_week_range(today, is_crypto=is_crypto)
            results["Haftalık"]["baslangic"] = hedef_start.isoformat()
            results["Haftalık"]["bitis"] = hedef_end.isoformat()
        except Exception as e: results["Haftalık"] = {"hata": str(e)}
        try:
            start, end = get_last_completed_month_range(today)
            results["Aylık"] = _levels_from_bars(_filter_by_range(daily_bars, start, end), birim="gün")
            hedef_start, hedef_end = get_current_month_range(today)
            results["Aylık"]["baslangic"] = hedef_start.isoformat()
            results["Aylık"]["bitis"] = hedef_end.isoformat()
        except Exception as e: results["Aylık"] = {"hata": str(e)}

    # --- 6 Aylık, Yıllık ---
    try:
        long_size = _compute_long_weekly_outputsize(today)
        weekly_bars = fetch_bars(user_symbol, "1week", long_size)
    except Exception as e:
        error = {"hata": str(e)}
        results["6 Aylık"] = error; results["Yıllık"] = error
        weekly_bars = None

    if weekly_bars is not None:
        try:
            start, end = get_last_completed_half_year_range(today)
            results["6 Aylık"] = _levels_from_bars(_filter_by_range(weekly_bars, start, end), birim="hafta")
            hedef_start, hedef_end = get_current_half_year_range(today)
            results["6 Aylık"]["baslangic"] = hedef_start.isoformat()
            results["6 Aylık"]["bitis"] = hedef_end.isoformat()
        except Exception as e: results["6 Aylık"] = {"hata": str(e)}
        try:
            start, end = get_last_completed_year_range(today)
            results["Yıllık"] = _levels_from_bars(_filter_by_range(weekly_bars, start, end), birim="hafta")
            hedef_start, hedef_end = get_current_year_range(today)
            results["Yıllık"]["baslangic"] = hedef_start.isoformat()
            results["Yıllık"]["bitis"] = hedef_end.isoformat()
        except Exception as e: results["Yıllık"] = {"hata": str(e)}

    results["_guncel_fiyat"] = guncel_fiyat
    results["_guncel_fiyat_zaman"] = guncel_fiyat_zaman
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
        "Bana bir enstrüman kodu gönder (örn: *BTCUSD*, *XAUUSD
