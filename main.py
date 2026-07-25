"""
DENGE ARALIĞI TELEGRAM BOTU (Tek Dosya)
========================================
BTCUSD, XAUUSD, EURUSD gibi enstrümanlar için Günlük / Haftalık / Aylık /
6 Aylık / Yıllık Denge, Direnç 1-2 ve Destek 1-2 seviyelerini hesaplar.
 
YÖNTEM:
  1) Dönemdeki HER GÜNÜN high ve low değeri tek tek alınır (N gün -> 2N sayı).
  2) Denge = bu 2N sayının aritmetik ortalaması.
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
 
# Tek seferde geriye dönük kaç GÜNLÜK mum çekileceği (800 gün ~ 2-3 yıl geriye gider,
# "son tamamlanmış yıl" gibi en uzun dönem ihtiyacını bile karşılar).
HISTORY_OUTPUTSIZE = 800
 
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
 
 
def fetch_daily_bars(user_symbol: str, outputsize: int):
    """Son `outputsize` adet GÜNLÜK mumu Twelve Data'dan çeker."""
    if not TWELVE_DATA_API_KEY:
        raise ValueError("TWELVE_DATA_API_KEY tanımlı değil. Ortam değişkenlerini kontrol edin.")
 
    symbol = normalize_symbol(user_symbol)
    params = {
        "symbol": symbol,
        "interval": "1day",
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
 
def get_last_completed_week_range(today: date):
    """Hafta Pazartesi-Pazar kabul edilir. İçinde bulunduğumuz hafta kapanmamış sayılır."""
    this_monday = today - timedelta(days=today.weekday())
    last_monday = this_monday - timedelta(days=7)
    last_sunday = last_monday + timedelta(days=6)
    return last_monday, last_sunday
 
 
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
 
 
def _dedupe_weekend_bars(bars):
    """
    Bazı veri sağlayıcıları emtia/forex sembolleri için hafta sonuna (Cmt/Paz)
    Cuma kapanışının BİREBİR AYNISI olan "hayalet" mumlar döndürebilir (gerçek
    işlem olmamasına rağmen). Bu, ortalamayı (dengeyi) Cuma değerlerini fazladan
    sayarak yanlış ağırlıklandırır.
 
    Bu fonksiyon: bir Cumartesi/Pazar mumu, kendisinden önceki en son hafta içi
    mumla high VE low değerlerinde birebir aynıysa, gerçek işlem olmadığı kabul
    edilip listeden çıkarılır. Kripto gibi gerçekten hafta sonu fiyatı değişen
    semboller bundan etkilenmez, çünkü değerler eşleşmeyecektir.
    """
    sorted_bars = sorted(bars, key=_parse_bar_date)
    result = []
    last_weekday_bar = None
    for bar in sorted_bars:
        d = _parse_bar_date(bar)
        if d.weekday() >= 5 and last_weekday_bar is not None:
            same_high = abs(float(bar["high"]) - float(last_weekday_bar["high"])) < 1e-9
            same_low = abs(float(bar["low"]) - float(last_weekday_bar["low"])) < 1e-9
            if same_high and same_low:
                continue  # hayalet hafta sonu mumu, atla
        result.append(bar)
        if d.weekday() < 5:
            last_weekday_bar = bar
    return result
 
 
def _last_completed_day_bars(bars, today: date):
    """Bugün HARİÇ, en güncel tamamlanmış tek günün mumunu döner."""
    completed = [b for b in bars if _parse_bar_date(b) < today]
    if not completed:
        return []
    latest = max(_parse_bar_date(b) for b in completed)
    return [b for b in completed if _parse_bar_date(b) == latest]
 
 
def _levels_from_bars(bars) -> dict:
    if not bars:
        raise ValueError("bu dönem henüz tamamlanmamış veya veri yok")
 
    values = []
    for bar in bars:
        values.append(round(float(bar["high"]), 4))
        values.append(round(float(bar["low"]), 4))
 
    denge = sum(values) / len(values)
    range_ = max(values) - min(values)
    half_range = range_ * 0.5
 
    counts = Counter(values)
    mods = sorted([v for v, c in counts.items() if c >= 2])
    dates = sorted({_parse_bar_date(b) for b in bars})
 
    return {
        "denge": denge,
        "direnc1": denge + half_range,
        "direnc2": denge + range_,
        "destek1": denge - half_range,
        "destek2": denge - range_,
        "range": range_,
        "mod": mods,
        "gun_sayisi": len(bars),
        "baslangic": dates[0].isoformat(),
        "bitis": dates[-1].isoformat(),
    }
 
 
def calculate_all_periods(user_symbol: str) -> dict:
    today = datetime.now(TR_TZ).date()
 
    try:
        all_bars = fetch_daily_bars(user_symbol, HISTORY_OUTPUTSIZE)
        all_bars = _dedupe_weekend_bars(all_bars)
    except Exception as e:
        error = {"hata": str(e)}
        return {name: error for name in PERIOD_NAMES}
 
    results = {}
 
    try:
        bars = _last_completed_day_bars(all_bars, today)
        results["Günlük"] = _levels_from_bars(bars)
    except Exception as e:
        results["Günlük"] = {"hata": str(e)}
 
    try:
        start, end = get_last_completed_week_range(today)
        results["Haftalık"] = _levels_from_bars(_filter_by_range(all_bars, start, end))
    except Exception as e:
        results["Haftalık"] = {"hata": str(e)}
 
    try:
        start, end = get_last_completed_month_range(today)
        results["Aylık"] = _levels_from_bars(_filter_by_range(all_bars, start, end))
    except Exception as e:
        results["Aylık"] = {"hata": str(e)}
 
    try:
        start, end = get_last_completed_half_year_range(today)
        results["6 Aylık"] = _levels_from_bars(_filter_by_range(all_bars, start, end))
    except Exception as e:
        results["6 Aylık"] = {"hata": str(e)}
 
    try:
        start, end = get_last_completed_year_range(today)
        results["Yıllık"] = _levels_from_bars(_filter_by_range(all_bars, start, end))
    except Exception as e:
        results["Yıllık"] = {"hata": str(e)}
 
    return results
 
 
# ----------------------------------------------------------------------------
# TELEGRAM BOTU
# ----------------------------------------------------------------------------
 
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Merhaba! 👋\n\n"
        "Bana bir enstrüman kodu gönder (örn: *BTCUSD*, *XAUUSD*, *EURUSD*).\n\n"
        "Sana Günlük, Haftalık, Aylık, 6 Aylık ve Yıllık için, SADECE TAMAMLANMIŞ "
        "(kapanmış) son periyoda göre Denge, Direnç 1/2 ve Destek 1/2 seviyelerini "
        "hesaplayayım.",
        parse_mode="Markdown",
    )
 
 
def format_period_block(period_name: str, result: dict) -> str:
    if "hata" in result:
        return f"*{period_name}*\n⚠️ {result['hata']}\n"
 
    tarih_araligi = f"{result['baslangic']} → {result['bitis']}"
    lines = [f"*{period_name}* ({result['gun_sayisi']} gün, {tarih_araligi})"]
    lines.append(f"  Direnç 2: `{result['direnc2']:,.2f}`")
    lines.append(f"  Direnç 1: `{result['direnc1']:,.2f}`")
    lines.append(f"  Denge:    `{result['denge']:,.2f}`")
    lines.append(f"  Destek 1: `{result['destek1']:,.2f}`")
    lines.append(f"  Destek 2: `{result['destek2']:,.2f}`")
 
    if result["mod"]:
        mod_str = ", ".join(f"{v:,.2f}" for v in result["mod"])
        lines.append(f"  🔁 Mod (tekrarlayan seviye): `{mod_str}`")
 
    return "\n".join(lines)
 
 
async def handle_symbol(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_symbol = update.message.text.strip()
    processing_msg = await update.message.reply_text(f"⏳ {user_symbol.upper()} hesaplanıyor...")
 
    results = calculate_all_periods(user_symbol)
 
    blocks = [f"📊 *{user_symbol.upper()} — Denge / Direnç / Destek*", ""]
    for period_name in PERIOD_NAMES:
        blocks.append(format_period_block(period_name, results.get(period_name, {"hata": "sonuç yok"})))
        blocks.append("")
 
    message = "\n".join(blocks).strip()
 
    if len(message) <= 4000:
        await processing_msg.edit_text(message, parse_mode="Markdown")
    else:
        await processing_msg.delete()
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
 
