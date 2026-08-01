"""
DENGE ARALIĞI TELEGRAM BOTU (3 Kaynaklı Hibrit Model)
============================================================================
1) Twelve Data      -> BTCUSD, XAUUSD, EURUSD gibi standart forex/kripto/emtia
2) isyatirimhisse    -> XU100, XU030, XU500 gibi BIST endeksleri (İş Yatırım)
3) Stooq (yedek)     -> Twelve Data'da ücretsiz planda kapalı olan XAGUSD,
                        XPTUSD, XPDUSD, VIX, DXY gibi semboller için denenir
                        (garantisi yoktur, Twelve Data reddederse devreye girer)
XAUTRYG (Gram Altın/TL) ise XAUUSD ve USDTRY üzerinden TÜRETİLİR.

GÜNCELLEME NOTU: Stooq ve yfinance sık sık "Too Many Requests" (rate limit)
hatası verdiği için şu iyileştirmeler eklendi:
  - Her iki kaynağa da gerçek tarayıcı User-Agent'ı ile istek atılıyor.
  - yfinance isteklerinde basit retry/backoff var.
  - Sonuçlar kısa süreli (5 dk) bellek-içi önbellekte tutuluyor, böylece
    aynı sembol için art arda gelen istekler (4 saatlik/günlük/haftalık/
    aylık) gereksiz yere kaynağı tekrar tekrar yormuyor.
  - "Too many requests / rate limit" durumu ayrı ve daha anlaşılır bir
    hata mesajıyla kullanıcıya bildiriliyor.
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

TWELVE_DATA_URL = "https://api.twelvedata.com/time_series"

CREDIT_BAR_UNIT = 21
MAX_SHORT_DAILY_BARS = 60
MAX_LONG_WEEKLY_BARS = 84
FOUR_HOUR_OUTPUTSIZE = 20

PERIOD_NAMES = ["4 Saatlik", "Günlük", "Haftalık", "Aylık", "6 Aylık", "Yıllık"]

TR_TZ = ZoneInfo("Europe/Istanbul")

# Stooq / Yahoo gibi siteler User-Agent'sız isteklerde daha kolay rate-limit
# uyguluyor; gerçek bir tarayıcı gibi görünmek için ortak header seti.
BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9,tr;q=0.8",
}

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)


# ----------------------------------------------------------------------------
# BASİT BELLEK-İÇİ ÖNBELLEK (rate limit'i azaltmak için)
# ----------------------------------------------------------------------------
# Aynı (kaynak, sembol, interval, outputsize) kombinasyonu kısa süre içinde
# tekrar istenirse ağa gitmek yerine önbellekten döner. Bu, tek bir sembol
# sorgusunda 4 saatlik/günlük/haftalık gibi birden fazla çağrının Stooq/
# yfinance'i art arda yormasını engeller.

_CACHE: dict[str, tuple[float, object]] = {}
_CACHE_TTL_SECONDS = 300  # 5 dakika


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
    """'BTCUSD' -> 'BTC/USD', 'XAUUSD' -> 'XAU/USD' gibi Twelve Data formatına çevirir."""
    s = user_symbol.strip().upper().replace(" ", "")
    if "/" in s:
        return s
    if len(s) > 3:
        base, quote = s[:-3], s[-3:]
        return f"{base}/{quote}"
    return s


def _normalize_stooq_symbol(user_symbol: str) -> str:
    """Kullanıcı girdisini Stooq formatına çevirir (yedek kaynak için)."""
    s = user_symbol.strip().upper().replace(" ", "")

    # VIX özel durumu (endeksler Stooq'ta '^' öneki alır)
    if s in ("VIX", "VIXUSD"):
        return "^vix"

    # DXY: Stooq'ta net teyit edilemedi, en olası tahmin denenir
    if s in ("DXY", "DXYUSD"):
        return "usdx"

    # Forex/emtia genel formatı: küçük harf, ayraçsız (xauusd, xagusd, xptusd, xpdusd vb.)
    return s.replace("/", "").lower()


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
        if any(word in msg.lower() for word in [
            "plan", "grow", "venture", "available starting",
            "not found", "no data", "invalid symbol", "does not exist",
        ]):
            raise ValueError("UPGRADE_REQUIRED")
        raise ValueError(msg)

    if "values" not in data:
        raise ValueError(f"'{symbol}' için veri bulunamadı. Sembolü kontrol edin (örn: BTCUSD, XAUUSD, EURUSD).")

    return data["values"]


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
        raise ValueError("isyatirimhisse kütüphanesi kurulu değil.")

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
            bars.append({
                "datetime": dt_str,
                "high": high_val,
                "low": low_val,
                "close": close_val if close_val is not None else (high_val + low_val) / 2,
            })
        except Exception:
            continue

    if not bars:
        raise ValueError(f"'{user_symbol}' için ayrıştırılabilir veri bulunamadı.")

    bars.sort(key=lambda b: b["datetime"])
    return bars[-outputsize:] if len(bars) > outputsize else bars


# ----------------------------------------------------------------------------
# VERİ ÇEKME - YEDEK KAYNAK: Stooq (sadece Twelve Data başarısız olursa)
# ----------------------------------------------------------------------------

def _fetch_bars_stooq_uncached(user_symbol: str, interval: str, outputsize: int):
    """
    Yedek veri kaynağı. Sadece Twelve Data 'UPGRADE_REQUIRED' hatası
    verdiğinde (ücretsiz planda kapalı semboller için) çağrılır.
    Stooq'ta gün-içi (4 saatlik) veri YOKTUR, sadece günlük/haftalık.
    NOT: Stooq'un resmi API'si yoktur, bu basit CSV linkine dayanır;
    bazı semboller (özellikle DXY) garantili çalışmayabilir. Ayrıca
    User-Agent'sız isteklerde sıkça "Too Many Requests" ile rate limit
    uygular; bu yüzden tarayıcı benzeri header'lar gönderiyoruz.
    """
    if interval == "4h":
        raise ValueError("Stooq kaynağında 4 saatlik veri yok.")

    stooq_interval = "w" if interval == "1week" else "d"
    symbol = _normalize_stooq_symbol(user_symbol)
    url = f"https://stooq.com/q/d/l/?s={symbol}&i={stooq_interval}"

    try:
        resp = requests.get(url, headers=BROWSER_HEADERS, timeout=15)
    except Exception as e:
        raise ValueError(f"Stooq'a bağlanılamadı: {e}")

    text = resp.text.strip()

    if _is_rate_limit_text(text) or resp.status_code == 429:
        raise ValueError(
            "Stooq şu anda istek limiti uyguluyor (Too Many Requests). "
            "Lütfen birkaç dakika sonra tekrar deneyin."
        )

    lines_ = text.splitlines()
    if not lines_ or "Date" not in lines_[0]:
        preview = text[:200].replace("\n", " ")
        raise ValueError(
            f"Stooq: '{user_symbol}' için veri alınamadı "
            f"(sembol desteklenmiyor ya da günlük istek limiti dolmuş olabilir. "
            f"Dönen içerik: {preview!r})"
        )

    bars = []
    for line in lines_[1:]:
        parts = line.split(",")
        if len(parts) < 5:
            continue
        try:
            bar = {
                "datetime": parts[0],
                "high": float(parts[2]),
                "low": float(parts[3]),
            }
            if len(parts) > 4 and parts[4]:
                bar["close"] = float(parts[4])
            bars.append(bar)
        except ValueError:
            continue

    if not bars:
        raise ValueError(f"Stooq: '{user_symbol}' için ayrıştırılabilir veri bulunamadı.")

    return bars[-outputsize:] if len(bars) > outputsize else bars


def fetch_bars_stooq(user_symbol: str, interval: str, outputsize: int):
    """Stooq çağrısını kısa süreli önbellekle sarmalar (rate limit'i azaltmak için)."""
    cache_key = f"stooq:{user_symbol.strip().upper()}:{interval}:{outputsize}"
    return _cached_fetch(cache_key, lambda: _fetch_bars_stooq_uncached(user_symbol, interval, outputsize))


# ----------------------------------------------------------------------------
# VERİ ÇEKME - 2. YEDEK KAYNAK: Yahoo Finance (yfinance)
# ----------------------------------------------------------------------------
# Sadece Twelve Data VE Stooq ikisi de başarısız olursa denenir.
# yfinance'te gün-içi (4 saatlik) veri bu botun ihtiyacına uygun şekilde
# YOKTUR (destekli değildir), sadece günlük/haftalık.

YFINANCE_INTERVAL_MAP = {"1day": "1d", "1week": "1wk"}

# Bazı metal/emtia sembolleri Yahoo'da forex çifti (ör. XAGUSD=X) olarak
# çalışmıyor veya yfinance'te tuhaf iç hatalara (ör. NoneType) yol açıyor;
# bu yüzden bu sembollerde doğrudan en yakın vadeli işlem kontratına düşülür
# (spota çok yakın hareket eder).
YFINANCE_FUTURES_FALLBACK = {
    "XAGUSD": "SI=F",   # Gümüş vadeli
    "XPTUSD": "PL=F",   # Platin vadeli
    "XPDUSD": "PA=F",   # Paladyum vadeli
}

# yfinance'in kendi Session'ı - tarayıcı benzeri header'larla, tekrar
# tekrar yeni Session oluşturmamak için modül seviyesinde bir kez kurulur.
_YFINANCE_SESSION = None


def _get_yfinance_session():
    global _YFINANCE_SESSION
    if _YFINANCE_SESSION is None:
        session = requests.Session()
        session.headers.update(BROWSER_HEADERS)
        _YFINANCE_SESSION = session
    return _YFINANCE_SESSION


def _normalize_yfinance_symbol(user_symbol: str) -> str:
    """'BTCUSD' -> 'BTC-USD', 'XAUUSD' -> 'XAUUSD=X' gibi Yahoo Finance formatına çevirir."""
    s = user_symbol.strip().upper().replace(" ", "").replace("/", "")
    if len(s) > 3:
        base, quote = s[:-3], s[-3:]
        if base in CRYPTO_BASES:
            return f"{base}-{quote}"
    return f"{s}=X"


def _fetch_bars_yfinance_uncached(user_symbol: str, interval: str, outputsize: int):
    """2. yedek veri kaynağı. Sadece Twelve Data VE Stooq başarısız olursa çağrılır.
    Yahoo da rate-limit uyguladığından basit bir retry/backoff eklendi."""
    if interval not in YFINANCE_INTERVAL_MAP:
        raise ValueError("yfinance kaynağında bu aralık (4 saatlik) desteklenmiyor.")

    if yf is None:
        raise ValueError("yfinance kütüphanesi kurulu değil (pip install yfinance).")

    yf_interval = YFINANCE_INTERVAL_MAP[interval]
    normalized_input = user_symbol.strip().upper().replace(" ", "")

    candidates = [_normalize_yfinance_symbol(user_symbol)]
    if normalized_input in YFINANCE_FUTURES_FALLBACK:
        candidates.append(YFINANCE_FUTURES_FALLBACK[normalized_input])

    # outputsize'ın üstüne, hafta sonu/tatil kayıplarını telafi etmek için pay bırakılır.
    days_multiplier = 7 if yf_interval == "1wk" else 2
    period_days = min(outputsize * days_multiplier + 30, 3650)

    session = _get_yfinance_session()

    last_err = None
    rate_limited = False
    max_attempts = 3

    for candidate in candidates:
        for attempt in range(max_attempts):
            try:
                df = yf.Ticker(candidate, session=session).history(
                    period=f"{period_days}d",
                    interval=yf_interval,
                    auto_adjust=False,
                )
                if df is None or df.empty:
                    last_err = ValueError(f"'{candidate}' için veri dönmedi.")
                    break  # boş veri retry ile düzelmez, sonraki adaya geç

                bars = []
                for idx, row in df.iterrows():
                    try:
                        bars.append({
                            "datetime": idx.strftime("%Y-%m-%d"),
                            "high": float(row["High"]),
                            "low": float(row["Low"]),
                            "close": float(row["Close"]),
                        })
                    except (TypeError, ValueError):
                        continue

                if not bars:
                    last_err = ValueError(f"'{candidate}' için ayrıştırılabilir veri yok.")
                    break

                return bars[-outputsize:] if len(bars) > outputsize else bars

            except Exception as e:
                last_err = e
                if _is_rate_limit_text(str(e)):
                    rate_limited = True
                    # Basit üstel bekleme: 1.5s, 3s, 4.5s
                    time.sleep(1.5 * (attempt + 1))
                    continue
                break  # rate limit dışı bir hata ise tekrar denemenin anlamı yok

    if rate_limited:
        raise ValueError(
            "Yahoo Finance şu anda istek limiti uyguluyor (Too Many Requests). "
            "Lütfen birkaç dakika sonra tekrar deneyin."
        )
    raise ValueError(f"yfinance: '{user_symbol}' için veri alınamadı ({last_err}).")


def fetch_bars_yfinance(user_symbol: str, interval: str, outputsize: int):
    """yfinance çağrısını kısa süreli önbellekle sarmalar (rate limit'i azaltmak için)."""
    cache_key = f"yfinance:{user_symbol.strip().upper()}:{interval}:{outputsize}"
    return _cached_fetch(cache_key, lambda: _fetch_bars_yfinance_uncached(user_symbol, interval, outputsize))


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
      3) Diğer her şey -> önce Twelve Data; 'UPGRADE_REQUIRED' hatası
         alırsa (ücretsiz planda kapalı sembol) Stooq denenir (garantisiz).
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
            xb_close = xb.get("close", (float(xb["high"]) + float(xb["low"])) / 2)
            ub_close = ub.get("close", (float(ub["high"]) + float(ub["low"])) / 2)
            result.append({
                "datetime": xb["datetime"],
                "high": (float(xb["high"]) / GRAMS_PER_TROY_OUNCE) * float(ub["high"]),
                "low": (float(xb["low"]) / GRAMS_PER_TROY_OUNCE) * float(ub["low"]),
                "close": (float(xb_close) / GRAMS_PER_TROY_OUNCE) * float(ub_close),
            })

        if not result:
            raise ValueError("XAUTRYG için XAUUSD ve USDTRY tarihleri eşleştirilemedi.")
        return result

    if normalized_input in BIST_INDEX_ALIASES:
        return fetch_bars_bist_index(user_symbol, interval, outputsize)

    try:
        return fetch_bars_twelvedata(user_symbol, interval, outputsize)
    except ValueError as e:
        if str(e) == "UPGRADE_REQUIRED":
            logger.info(f"🔄 '{user_symbol}' Twelve Data ücretsiz planda kapalı, Stooq deneniyor...")
            try:
                return fetch_bars_stooq(user_symbol, interval, outputsize)
            except Exception as stooq_err:
                logger.info(f"🔄 '{user_symbol}' Stooq da başarısız oldu, yfinance deneniyor... ({stooq_err})")
                try:
                    return fetch_bars_yfinance(user_symbol, interval, outputsize)
                except Exception as yf_err:
                    raise ValueError(
                        f"Twelve Data ücretsiz planda kapalı; Stooq ({stooq_err}) ve "
                        f"yfinance ({yf_err}) denemeleri de başarısız oldu."
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
# HEDEF DÖNEM SINIRLARI
# ----------------------------------------------------------------------------
# Denge aralığı mantığı: bir önceki TAMAMLANMIŞ dönemin (gün/hafta/ay/...)
# verisinden hesaplanan Denge/Ortalama/Direnç/Destek seviyeleri, o dönemin
# kendisi için değil, BİR SONRAKİ (şu an içinde bulunulan / gelecek) dönem
# için geçerlidir. Örn: Temmuz'un günlük mumlarından hesaplanan Aylık
# seviyeler, Ağustos ayı boyunca kullanılacak seviyelerdir.
# Bu yüzden ekranda gösterilen tarih aralığı, verinin geldiği dönem değil,
# seviyelerin GEÇERLİ OLDUĞU (hedef) dönem olmalıdır.

def get_current_day_range(today: date):
    return today, today


def get_current_week_range(today: date, is_crypto: bool = False):
    weekday = today.weekday()
    if is_crypto:
        monday = today - timedelta(days=weekday)
        sunday = monday + timedelta(days=6)
        return monday, sunday

    if weekday >= 5:
        # Hafta sonu: piyasa kapalı, hedef BİR SONRAKİ hafta (Pzt-Cum).
        next_monday = today - timedelta(days=weekday) + timedelta(days=7)
        next_friday = next_monday + timedelta(days=4)
        return next_monday, next_friday

    this_monday = today - timedelta(days=weekday)
    this_friday = this_monday + timedelta(days=4)
    return this_monday, this_friday


def _next_trading_day(today: date, is_crypto: bool = False) -> date:
    """Kripto için her gün işlem günüdür. Diğerlerinde hafta sonuysa
    bir sonraki Pazartesi'ye kaydırılır (Günlük/4 Saatlik hedef tarihi için)."""
    if is_crypto:
        return today
    d = today
    while d.weekday() >= 5:
        d += timedelta(days=1)
    return d


def get_current_month_range(today: date):
    first_of_month = today.replace(day=1)
    if today.month == 12:
        next_month_first = date(today.year + 1, 1, 1)
    else:
        next_month_first = date(today.year, today.month + 1, 1)
    last_of_month = next_month_first - timedelta(days=1)
    return first_of_month, last_of_month


def get_current_half_year_range(today: date):
    year = today.year
    if today.month <= 6:
        return date(year, 1, 1), date(year, 6, 30)
    return date(year, 7, 1), date(year, 12, 31)


def get_current_year_range(today: date):
    return date(today.year, 1, 1), date(today.year, 12, 31)


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


def _bar_close(bar: dict) -> float:
    """Bar içinde 'close' varsa onu, yoksa high/low ortalamasını döndürür."""
    close_val = bar.get("close")
    if close_val is not None:
        try:
            return float(close_val)
        except (TypeError, ValueError):
            pass
    return (float(bar["high"]) + float(bar["low"])) / 2


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
        h = round(float(bar["high"]), 4)
        l = round(float(bar["low"]), 4)
        values.append(h)
        # high == low ise (ör. BIST endekslerinde gerçek yüksek/düşük veri
        # olmadığı için ikisi de kapanışa eşitleniyor), aynı değeri iki kez
        # eklemek o barı yapay olarak "tekrarlayan seviye" (mod) yapar.
        # Bu yüzden eşitse sadece bir kez sayıyoruz.
        if l != h:
            values.append(l)

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
    is_crypto = _is_crypto_symbol(user_symbol)
    hedef_gun = _next_trading_day(today, is_crypto)
    results = {}

    # Güncel fiyat: en taze veriden (önce 4 saatlik, olmazsa günlük) yakalanır.
    guncel_fiyat = None
    guncel_fiyat_zaman = None

    # --- 4 Saatlik ---
    try:
        four_hour_bars_raw = fetch_bars(user_symbol, "4h", FOUR_HOUR_OUTPUTSIZE)
        four_hour_bars = _filter_weekend_bars_if_not_crypto(four_hour_bars_raw, user_symbol)
        last_bar = max(four_hour_bars, key=_parse_bar_datetime) if four_hour_bars else None
        if last_bar is None:
            raise ValueError("Yeterli 4 saatlik veri bulunamadı.")
        results["4 Saatlik"] = _levels_from_bars([last_bar], birim="adet 4 saatlik mum")
        # Bu seviyeler son kapanan 4 saatlik mumdan hesaplanır ama BİR SONRAKİ
        # 4 saatlik mum için geçerlidir; tarih etiketi bugünü gösterir.
        results["4 Saatlik"]["baslangic"] = hedef_gun.isoformat()
        results["4 Saatlik"]["bitis"] = hedef_gun.isoformat()
        guncel_fiyat = _bar_close(last_bar)
        guncel_fiyat_zaman = _parse_bar_datetime(last_bar)
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
        if guncel_fiyat is None:
            latest_daily_bar = max(daily_bars, key=_parse_bar_date) if daily_bars else None
            if latest_daily_bar is not None:
                guncel_fiyat = _bar_close(latest_daily_bar)
                guncel_fiyat_zaman = _parse_bar_datetime(latest_daily_bar)

        try:
            bars = _last_completed_day_bars(daily_bars, today)
            results["Günlük"] = _levels_from_bars(bars, birim="gün")
            # Son kapanan günden hesaplanır, BUGÜN için geçerlidir.
            results["Günlük"]["baslangic"] = hedef_gun.isoformat()
            results["Günlük"]["bitis"] = hedef_gun.isoformat()
        except Exception as e:
            results["Günlük"] = {"hata": str(e)}

        try:
            start, end = get_last_completed_week_range(today, is_crypto=is_crypto)
            results["Haftalık"] = _levels_from_bars(_filter_by_range(daily_bars, start, end), birim="gün")
            hedef_start, hedef_end = get_current_week_range(today, is_crypto=is_crypto)
            results["Haftalık"]["baslangic"] = hedef_start.isoformat()
            results["Haftalık"]["bitis"] = hedef_end.isoformat()
        except Exception as e:
            results["Haftalık"] = {"hata": str(e)}

        try:
            start, end = get_last_completed_month_range(today)
            results["Aylık"] = _levels_from_bars(_filter_by_range(daily_bars, start, end), birim="gün")
            hedef_start, hedef_end = get_current_month_range(today)
            results["Aylık"]["baslangic"] = hedef_start.isoformat()
            results["Aylık"]["bitis"] = hedef_end.isoformat()
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
            hedef_start, hedef_end = get_current_half_year_range(today)
            results["6 Aylık"]["baslangic"] = hedef_start.isoformat()
            results["6 Aylık"]["bitis"] = hedef_end.isoformat()
        except Exception as e:
            results["6 Aylık"] = {"hata": str(e)}

        try:
            start, end = get_last_completed_year_range(today)
            results["Yıllık"] = _levels_from_bars(_filter_by_range(weekly_bars, start, end), birim="hafta")
            hedef_start, hedef_end = get_current_year_range(today)
            results["Yıllık"]["baslangic"] = hedef_start.isoformat()
            results["Yıllık"]["bitis"] = hedef_end.isoformat()
        except Exception as e:
            results["Yıllık"] = {"hata": str(e)}

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

    birim_etiketi = f"{result['adet']} {result['birim']} verisiyle hesaplandı"
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
        f"{icon} *{period_name}*",
        f"_{birim_etiketi}_",
        f"_📆 Geçerlilik: {tarih_araligi}_",
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


def _format_current_price_line(guncel_fiyat, guncel_fiyat_zaman):
    if guncel_fiyat is None:
        return None
    zaman_str = ""
    if guncel_fiyat_zaman is not None:
        if guncel_fiyat_zaman.time() == guncel_fiyat_zaman.min.time():
            zaman_str = guncel_fiyat_zaman.strftime("%d.%m.%Y")
        else:
            zaman_str = guncel_fiyat_zaman.strftime("%d.%m.%Y %H:%M")
    fiyat_str = f"{guncel_fiyat:,.2f}"
    if zaman_str:
        return f"💵 *Güncel Fiyat:* `{fiyat_str}`  _({zaman_str} itibarıyla)_"
    return f"💵 *Güncel Fiyat:* `{fiyat_str}`"


async def handle_symbol(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_symbol = update.message.text.strip()
    processing_msg = await update.message.reply_text(f"⏳ {user_symbol.upper()} hesaplanıyor...")

    results = calculate_all_periods(user_symbol)
    guncel_fiyat_satiri = _format_current_price_line(
        results.pop("_guncel_fiyat", None), results.pop("_guncel_fiyat_zaman", None)
    )

    separator = "━" * 24
    header_blocks = [f"💰 *{user_symbol.upper()}*"]
    if guncel_fiyat_satiri:
        header_blocks.append(guncel_fiyat_satiri)
    header_blocks.append(separator)

    blocks = header_blocks + [""]
    for i, period_name in enumerate(PERIOD_NAMES):
        blocks.append(format_period_block(period_name, results.get(period_name, {"hata": "sonuç yok"})))
        if i < len(PERIOD_NAMES) - 1:
            blocks.append("")

    message = "\n".join(blocks).strip()

    if len(message) <= 4000:
        await processing_msg.edit_text(message, parse_mode="Markdown")
    else:
        await processing_msg.delete()
        await update.message.reply_text("\n".join(header_blocks), parse_mode="Markdown")
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
