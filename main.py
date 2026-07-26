"""
DENGE ARALIĞI TELEGRAM BOTU (Tek Dosya)
========================================
BTCUSD, XAUUSD, EURUSD gibi enstrümanlar için Günlük / Haftalık / Aylık /
6 Aylık / Yıllık Denge, Direnç 1-2 ve Destek 1-2 seviyelerini hesaplar.
 
YÖNTEM:
  1) Dönemdeki HER GÜNÜN (ya da 6 Aylık/Yıllık için her HAFTANIN) high ve low
     değeri tek tek alınır (N adet -> 2N sayı).
  2) Denge = bu 2N sayının MEDYANI (aritmetik ortalama değil — uç değerlerden
     daha az etkilenmesi için tercih edildi).
  3) Range = en yüksek değer - en düşük değer.
  4) Direnç 1 = Denge + Range*0.5    Direnç 2 = Denge + Range
     Destek 1  = Denge - Range*0.5    Destek 2  = Denge - Range
  5) Mod: 2N sayı içinde 2+ kez tekrar eden değer varsa işaretlenir.
 
ÖNEMLİ KURAL: Her dönem SADECE TAMAMLANMIŞ (kapanmış) son periyodu kullanır.
  Örn. bugün Çarşamba ise "Haftalık" geçen haftanın (Pzt-Paz) verisini kullanır;
  içinde bulunduğumuz haftanın henüz kapanmamış verisi KULLANILMAZ.
  Aynı kural Günlük/Aylık/6 Aylık/Yıllık için de geçerlidir.
 
ÇALIŞTIRMAK İÇİN GEREKLİ ORTAM DEĞİŞKENLERİ:
  TELEGRAM_BOT_TOKEN   -> BotFather'dan alınan token
  TWELVE_DATA_API_KEY  -> twelvedata.com üzerinden alınan ücretsiz API anahtarı
"""
 
import os
import logging
import statistics
from collections import Counter
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo
 
import requests
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
 
# Twelve Data ücretsiz plan: dakikada 8 KREDİ (yaklaşık her 21 mum = 1 kredi).
# Bu yüzden TEK seferde 800 mum çekmek (~39 kredi) limiti aşıyordu.
# Çözüm: iki KÜÇÜK istek yapıyoruz:
#   1) Günlük mumlarla KISA bir istek (Günlük/Haftalık/Aylık için yeterli)
#   2) Haftalık mumlarla (daha az veri noktası) UZUN bir istek (6 Aylık/Yıllık için)
# Bu ikisinin toplam kredi maliyeti her zaman ~8 kredinin altında kalacak şekilde
# tarihe göre dinamik hesaplanır.
CREDIT_BAR_UNIT = 21  # ~1 kredi = 21 mum (Twelve Data gözlemlenen davranışı)
MAX_SHORT_DAILY_BARS = 60   # güvenlik tavanı (~3 kredi)
MAX_LONG_WEEKLY_BARS = 95   # güvenlik tavanı (~5 kredi)
 
PERIOD_NAMES = ["Günlük", "Haftalık", "Aylık", "6 Aylık", "Yıllık"]
 
TR_TZ = ZoneInfo("Europe/Istanbul")
 
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)
 
 
# ----------------------------------------------------------------------------
# VERİ ÇEKME (Twelve Data API)
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
 
 
def fetch_bars(user_symbol: str, interval: str, outputsize: int):
    """Twelve Data'dan belirtilen aralıkta (1day / 1week) son `outputsize` mumu çeker."""
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
        raise ValueError(data.get("message", f"'{symbol}' için veri alınamadı."))
 
    if "values" not in data:
        raise ValueError(f"'{symbol}' için veri bulunamadı. Sembolü kontrol edin (örn: BTCUSD, XAUUSD, EURUSD).")
 
    return data["values"]
 
 
# ----------------------------------------------------------------------------
# DÖNEM SINIRLARI (son TAMAMLANMIŞ hafta / ay / yarıyıl / yıl)
# ----------------------------------------------------------------------------
 
def get_last_completed_week_range(today: date, is_crypto: bool = False):
    """
    Kripto (7 gün/hafta işlem görür): Hafta Pazartesi-Pazar kabul edilir.
    İçinde bulunduğumuz hafta HENÜZ KAPANMAMIŞ sayılır.
 
    Forex/emtia (Pazartesi-Cuma işlem görür): Hafta Pazartesi-Cuma kabul edilir.
    Cuma kapanışından sonra (yani bugün Cumartesi veya Pazar ise) o haftanın
    ARTIK TAMAMLANDIĞI kabul edilir ve o hafta gösterilir (bir hafta geriye
    gitmeye gerek yoktur, çünkü Cuma akşamı piyasa zaten kapanmıştır).
    """
    weekday = today.weekday()  # 0=Pzt ... 4=Cuma, 5=Cmt, 6=Paz
 
    if is_crypto:
        this_monday = today - timedelta(days=weekday)
        last_monday = this_monday - timedelta(days=7)
        last_sunday = last_monday + timedelta(days=6)
        return last_monday, last_sunday
 
    if weekday >= 5:
        # Bugün Cumartesi/Pazar -> bu haftanın Pzt-Cuma'sı zaten tamamlandı
        week_monday = today - timedelta(days=weekday)
    else:
        # Bugün Pzt-Cuma arası -> bu hafta henüz bitmedi, önceki haftayı kullan
        this_monday = today - timedelta(days=weekday)
        week_monday = this_monday - timedelta(days=7)
 
    week_friday = week_monday + timedelta(days=4)
    return week_monday, week_friday
 
 
def get_last_completed_month_range(today: date):
    """İçinde bulunduğumuz ay kapanmamış sayılır; bir önceki takvim ayı döner."""
    first_of_this_month = today.replace(day=1)
    last_day_prev_month = first_of_this_month - timedelta(days=1)
    first_day_prev_month = last_day_prev_month.replace(day=1)
    return first_day_prev_month, last_day_prev_month
 
 
def get_last_completed_half_year_range(today: date):
    """Yarıyıllar Ocak-Haziran / Temmuz-Aralık kabul edilir."""
    year = today.year
    if today.month <= 6:
        return date(year - 1, 7, 1), date(year - 1, 12, 31)
    return date(year, 1, 1), date(year, 6, 30)
 
 
def get_last_completed_year_range(today: date):
    """İçinde bulunduğumuz takvim yılı kapanmamış sayılır; bir önceki yıl döner."""
    last_year = today.year - 1
    return date(last_year, 1, 1), date(last_year, 12, 31)
 
 
# ----------------------------------------------------------------------------
# HESAPLAMA
# ----------------------------------------------------------------------------
 
def _parse_bar_date(bar: dict) -> date:
    return datetime.strptime(bar["datetime"][:10], "%Y-%m-%d").date()
 
 
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
    """
    XAUUSD, EURUSD gibi forex/emtia sembolleri normalde Pazartesi-Cuma işlem görür.
    Ancak bazı veri sağlayıcıları (Twelve Data dahil) bu sembroller için Cumartesi/
    Pazar günlerine de (gerçek piyasa hareketi olmasa bile) farklı, SENTETİK günlük
    mumlar döndürebilir. Bu durum "haftalık = 5 gün" gibi beklenen periyot
    uzunluklarını bozar (7 gün olarak görünür) ve dengeyi yanlış hesaplatır.
 
    Bu yüzden KRİPTO DIŞINDAKİ semboller için Cumartesi/Pazar mumları, değerlerine
    bakılmaksızın tamamen hesap dışı bırakılır. Kripto (7 gün/hafta gerçekten
    işlem gören) semboller bundan etkilenmez.
    """
    if _is_crypto_symbol(user_symbol):
        return bars
    return [b for b in bars if _parse_bar_date(b).weekday() < 5]
 
 
def _last_completed_day_bars(bars, today: date):
    """Bugün HARİÇ, en güncel tamamlanmış tek günün mumunu döner."""
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
    """Günlük/Haftalık/Aylık için gereken en geniş geçmişi (Aylık'ın önceki ay
    sınırı) dinamik olarak hesaplar, küçük bir güvenlik payı ekler."""
    month_start, _ = get_last_completed_month_range(today)
    days_needed = (today - month_start).days + 7
    return max(15, min(days_needed, MAX_SHORT_DAILY_BARS))
 
 
def _compute_long_weekly_outputsize(today: date) -> int:
    """6 Aylık/Yıllık için gereken en geniş geçmişi (Yıllık'ın önceki yıl
    sınırı) dinamik olarak hesaplar, haftalık mum sayısına çevirir."""
    year_start, _ = get_last_completed_year_range(today)
    days_needed = (today - year_start).days
    weeks_needed = (days_needed // 7) + 3
    return max(30, min(weeks_needed, MAX_LONG_WEEKLY_BARS))
 
 
def calculate_all_periods(user_symbol: str) -> dict:
    today = datetime.now(TR_TZ).date()
    results = {}
 
    # --- KISA İSTEK: günlük mumlar (Günlük / Haftalık / Aylık için) ---
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
 
    # --- UZUN İSTEK: haftalık mumlar (6 Aylık / Yıllık için, kredi tasarrufu) ---
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
    "Günlük": "🕐",
    "Haftalık": "📅",
    "Aylık": "🗓️",
    "6 Aylık": "📈",
    "Yıllık": "🏆",
}
 
 
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "✨ *Denge Aralığı Botu* ✨\n\n"
        "Bana bir enstrüman kodu gönder (örn: *BTCUSD*, *XAUUSD*, *EURUSD*).\n\n"
        "🕐 Günlük  📅 Haftalık  🗓️ Aylık  📈 6 Aylık  🏆 Yıllık\n"
        "için Denge (Medyan), Aritmetik Ortalama, Direnç 1/2 ve Destek 1/2 "
        "seviyelerini hesaplayayım.\n\n"
        "_Yalnızca TAMAMLANMIŞ (kapanmış) son periyot kullanılır._\n"
        "_6 Aylık ve Yıllık, API kredi limiti nedeniyle haftalık mumlarla hesaplanır._",
        parse_mode="Markdown",
    )
 
 
def _format_tr_date(iso_date: str) -> str:
    """'2026-07-13' -> '13.07.2026'"""
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
