import asyncio
import logging
import os
from datetime import datetime
from typing import Dict, List, Tuple
import aiohttp
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes
from telegram.constants import ParseMode

# Loglama
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Environment değişkenleri
TELEGRAM_TOKEN = os.getenv('TELEGRAM_TOKEN')
TWELVEDATA_API_KEY = os.getenv('TWELVEDATA_API_KEY')

# ============================================================
# SEMBOL KÜTÜPHANESİ - Hangi piyasa kaç gün işlem görür?
# ============================================================

FOREX_SEMBOLLERI = {
    'XAUUSD', 'XAGUSD', 'EURUSD', 'GBPUSD', 'USDJPY',
    'USDTRY', 'EURTRY', 'GBPTRY', 'AUDUSD', 'NZDUSD',
    'USDCAD', 'USDCHF', 'EURJPY', 'GBPJPY'
}

KRIPTO_SEMBOLLERI = {
    'BTC', 'ETH', 'SOL', 'DOGE', 'ADA', 'XRP', 'DOT',
    'AVAX', 'MATIC', 'LINK', 'UNI', 'ATOM', 'LTC',
    'BTCUSD', 'ETHUSD', 'BTCUSDT', 'ETHUSDT'
}

HISSE_SEMBOLLERI = {
    'AAPL', 'GOOGL', 'MSFT', 'TSLA', 'AMZN', 'META',
    'NFLX', 'NVDA', 'XU100', 'BIST100'
}

EMTIA_SEMBOLLERI = {
    'XAU', 'XAG', 'WTI', 'BRENT', 'COPPER', 'NGAS'
}

ENDEKS_SEMBOLLERI = {
    'SP500', 'NASDAQ', 'DOW30', 'DAX40', 'FTSE100'
}

def sembol_turu_bul(sembol: str) -> Tuple[str, int]:
    """Sembole göre piyasa türünü ve işlem günü sayısını belirle"""
    sembol = sembol.upper().strip()
    temiz = sembol.replace('USDT', '').replace('USD', '')
    
    if sembol in KRIPTO_SEMBOLLERI or temiz in KRIPTO_SEMBOLLERI:
        return 'KRIPTO', 7
    elif sembol in FOREX_SEMBOLLERI or temiz in FOREX_SEMBOLLERI:
        return 'FOREX/EMTİA', 5
    elif sembol in EMTIA_SEMBOLLERI:
        return 'EMTİA', 5
    elif sembol in HISSE_SEMBOLLERI or temiz in HISSE_SEMBOLLERI:
        return 'HİSSE/ENDEKS', 5
    elif sembol in ENDEKS_SEMBOLLERI:
        return 'ENDEKS', 5
    elif 'XAU' in sembol or 'XAG' in sembol:
        return 'FOREX/EMTİA', 5
    elif any(k in sembol for k in ['BTC', 'ETH', 'COIN']):
        return 'KRIPTO', 7
    else:
        return 'STANDART', 5

# ============================================================
# TWELVE DATA API İŞLEMLERİ
# ============================================================

class TwelveDataAPI:
    """Twelve Data API ile veri çekme"""
    
    BASE_URL = "https://api.twelvedata.com"
    
    def __init__(self, api_key: str):
        self.api_key = api_key
    
    async def get_price_data(self, symbol: str, interval: str, outputsize: int) -> List[Dict]:
        """Fiyat verilerini çek"""
        # Sembol formatını Twelve Data'ya uygun hale getir
        if symbol.upper() in ['XAUUSD', 'XAGUSD']:
            symbol = symbol.upper()  # XAUUSD, XAGUSD aynen kalır
        elif '/' not in symbol:
            # BTC -> BTC/USD, ETH -> ETH/USD
            if 'USDT' in symbol.upper():
                symbol = symbol.upper().replace('USDT', '/USD')
            elif 'USD' not in symbol.upper():
                symbol = f"{symbol.upper()}/USD"
        
        params = {
            'symbol': symbol,
            'interval': interval,
            'outputsize': outputsize,
            'apikey': self.api_key
        }
        
        url = f"{self.BASE_URL}/time_series"
        
        async with aiohttp.ClientSession() as session:
            async with session.get(url, params=params) as response:
                if response.status == 200:
                    data = await response.json()
                    if 'values' in data:
                        return data['values']
                    else:
                        logger.error(f"API hatası: {data.get('message', 'Bilinmeyen')}")
                        return []
                else:
                    logger.error(f"HTTP hatası: {response.status}")
                    return []

# ============================================================
# DENGE HESAPLAMA (ORİJİNAL FORMÜL)
# ============================================================

def denge_formulu(veri: List[Dict]) -> float:
    """
    ORİJİNAL FORMÜL:
    Tüm HIGH ve LOW değerlerini topla, 2'ye böl
    """
    tum_degerler = []
    
    for kayit in veri:
        if 'high' in kayit and 'low' in kayit:
            try:
                tum_degerler.append(float(kayit['high']))
                tum_degerler.append(float(kayit['low']))
            except (ValueError, TypeError):
                continue
    
    if not tum_degerler:
        return 0
    
    toplam = sum(tum_degerler)
    return toplam / 2

def is_gunu_filtrele(veri: List[Dict]) -> List[Dict]:
    """Sadece hafta içi günleri al (Pazartesi-Cuma)"""
    filtrelenmis = []
    for kayit in veri:
        if 'datetime' in kayit:
            try:
                tarih = datetime.strptime(kayit['datetime'][:10], '%Y-%m-%d')
                if tarih.weekday() < 5:  # 0-4 = Pazartesi-Cuma
                    filtrelenmis.append(kayit)
            except:
                continue
    return filtrelenmis if filtrelenmis else veri

async def tum_dengeleri_hesapla(sembol: str, api: TwelveDataAPI) -> Dict:
    """Tüm zaman dilimleri için denge hesapla"""
    
    piyasa_turu, islem_gunu = sembol_turu_bul(sembol)
    haftaici_gerekli = (piyasa_turu != 'KRIPTO')
    
    sonuclar = {}
    
    # 1. GÜNLÜK DENGE (24 saat)
    veri = await api.get_price_data(sembol, '1h', 24)
    sonuclar['gunluk'] = denge_formulu(veri)
    await asyncio.sleep(1.5)
    
    # 2. HAFTALIK DENGE (5 veya 7 gün)
    veri = await api.get_price_data(sembol, '1day', 7)
    if haftaici_gerekli:
        veri = is_gunu_filtrele(veri)[:5]
    sonuclar['haftalik'] = denge_formulu(veri)
    await asyncio.sleep(1.5)
    
    # 3. AYLIK DENGE (30 gün)
    veri = await api.get_price_data(sembol, '1day', 30)
    if haftaici_gerekli:
        veri = is_gunu_filtrele(veri)
    sonuclar['aylik'] = denge_formulu(veri)
    await asyncio.sleep(1.5)
    
    # 4. 6 AYLIK DENGE (26 hafta)
    veri = await api.get_price_data(sembol, '1week', 26)
    sonuclar['alti_aylik'] = denge_formulu(veri)
    await asyncio.sleep(1.5)
    
    # 5. YILLIK DENGE (12 ay)
    veri = await api.get_price_data(sembol, '1month', 12)
    sonuclar['yillik'] = denge_formulu(veri)
    
    return {
        'sembol': sembol.upper(),
        'piyasa_turu': piyasa_turu,
        'islem_gunu': islem_gunu,
        'dengeler': sonuclar,
        'zaman': datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    }

# ============================================================
# TELEGRAM BOT KOMUTLARI
# ============================================================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Başlangıç komutu"""
    mesaj = """
🤖 *DENGE ARALIĞI BOTU*

📊 *Desteklenen Semboller:*
• Kripto (7 gün): BTC, ETH, SOL...
• Forex (5 gün): XAUUSD, EURUSD...
• Hisse (5 gün): AAPL, TSLA, XU100...
• Emtia (5 gün): XAGUSD, WTI...

💡 *Kullanım:* `/denge SEMBOL`
📝 *Örnek:* `/denge XAUUSD`

📐 *Formül:* (Tüm HIGH + Tüm LOW) / 2
    """
    await update.message.reply_text(mesaj, parse_mode=ParseMode.MARKDOWN)

async def denge(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Denge hesaplama komutu"""
    
    if not context.args:
        await update.message.reply_text(
            "⚠️ *Lütfen bir sembol girin!*\n"
            "Örnek: `/denge XAUUSD` veya `/denge BTC`",
            parse_mode=ParseMode.MARKDOWN
        )
        return
    
    sembol = context.args[0]
    piyasa_turu, islem_gunu = sembol_turu_bul(sembol)
    
    # Piyasa türüne göre emoji
    emojiler = {
        'KRIPTO': '🪙',
        'FOREX/EMTİA': '💱',
        'EMTİA': '🏭',
        'HİSSE/ENDEKS': '📈',
        'ENDEKS': '📊',
        'STANDART': '📉'
    }
    emoji = emojiler.get(piyasa_turu, '📉')
    
    # Yükleniyor mesajı
    yukleniyor = await update.message.reply_text(
        f"{emoji} *{sembol.upper()}* hesaplanıyor...\n"
        f"📌 Piyasa: {piyasa_turu} ({islem_gunu} işlem günü)",
        parse_mode=ParseMode.MARKDOWN
    )
    
    try:
        # API bağlantısı
        api = TwelveDataAPI(TWELVEDATA_API_KEY)
        
        # Hesaplama
        sonuc = await tum_dengeleri_hesapla(sembol, api)
        d = sonuc['dengeler']
        
        # Sonuç mesajı
        cevap = f"{emoji} *{sonuc['sembol']} DENGE ARALIKLARI*\n"
        cevap += f"📌 *Piyasa:* {sonuc['piyasa_turu']}\n"
        cevap += f"📅 *İşlem Günü:* {sonuc['islem_gunu']} gün\n\n"
        
        cevap += "━━━━━━━━━━━━━━━━━━━━━\n\n"
        cevap += f"🟢 *Günlük:* {d['gunluk']:,.2f}\n"
        cevap += f"🔵 *Haftalık:* {d['haftalik']:,.2f}\n"
        cevap += f"🟡 *Aylık:* {d['aylik']:,.2f}\n"
        cevap += f"🟣 *6 Aylık:* {d['alti_aylik']:,.2f}\n"
        cevap += f"🔴 *Yıllık:* {d['yillik']:,.2f}\n\n"
        
        cevap += "━━━━━━━━━━━━━━━━━━━━━\n\n"
        cevap += "📐 *Formül:* ∑(HIGH + LOW) / 2\n"
        
        if piyasa_turu == 'KRIPTO':
            cevap += "⚡ *Not:* 7 gün (24/7) üzerinden hesaplandı\n"
        else:
            cevap += "⚠️ *Not:* Sadece iş günleri (Pzt-Cuma) dahil edildi\n"
        
        cevap += f"\n🕐 {sonuc['zaman']}"
        
        await yukleniyor.edit_text(cevap, parse_mode=ParseMode.MARKDOWN)
        
    except Exception as e:
        logger.error(f"Hata: {e}")
        await yukleniyor.edit_text(
            f"❌ *Hata oluştu!*\nLütfen sembolü kontrol edin veya tekrar deneyin.",
            parse_mode=ParseMode.MARKDOWN
        )

# ============================================================
# ANA PROGRAM
# ============================================================

def main():
    """Botu başlat"""
    if not TELEGRAM_TOKEN or not TWELVEDATA_API_KEY:
        logger.error("❌ TELEGRAM_TOKEN veya TWELVEDATA_API_KEY eksik!")
        return
    
    logger.info("🤖 Bot başlatılıyor...")
    
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("denge", denge))
    
    logger.info("✅ Bot çalışıyor!")
    app.run_polling()

if __name__ == "__main__":
    main()