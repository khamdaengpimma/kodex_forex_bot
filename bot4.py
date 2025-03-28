import yfinance as yf
import logging
import json
import pandas as pd
from datetime import datetime, timedelta
from telegram import (
    Update,
    Bot,
    KeyboardButton,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove
)
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackContext,
    MessageHandler,
    filters,
    ConversationHandler
)
from ta.volatility import AverageTrueRange
from ta.momentum import RSIIndicator
from ta.trend import MACD
import pytz
import re
from typing import Dict, Any, List, Optional
from config import TOKEN
# Configuration
#TOKEN = "You_token"
CHAT_IDS_FILE = "chat_ids.json"
ALERTS_FILE = "price_alerts.json"
SETTINGS_FILE = "user_settings.json"
LOG_FILE = "forex_bot.log"
DATA_CACHE_FILE = "price_data_cache.json"
CACHE_EXPIRY_MINUTES = 5

# Common forex symbols (Yahoo Finance format)
FOREX_SYMBOLS = {
    "EUR/USD": "EURUSD=X",
    "GBP/USD": "GBPUSD=X",
    "USD/JPY": "USDJPY=X",
    "USD/CHF": "USDCHF=X",
    "AUD/USD": "AUDUSD=X",
    "NZD/USD": "NZDUSD=X",
    "USD/CAD": "USDCAD=X",
    "XAU/USD": "GC=F"  # Gold as a forex-like instrument
}

# Conversation states
(
    MENU, SETTINGS, ALERTS, SYMBOL_SELECTION,
    SET_ALERT_PRICE, SET_ALERT_TYPE,
    SET_TIMEZONE, SET_UPDATE_FREQ,
    SET_INDICATORS, SET_RISK
) = range(10)

# Initialize bot
bot = Bot(token=TOKEN)

# Enhanced logging configuration
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler(LOG_FILE, encoding='utf-8'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

class EnhancedJSONEncoder(json.JSONEncoder):
    def default(self, obj):
        if isinstance(obj, datetime):
            return obj.isoformat()
        return super().default(obj)

class PriceDataCache:
    def __init__(self):
        self.data = {}  # Dictionary to store data by symbol
        self.last_updated = {}
        self.lock = False
    
    def is_valid(self, symbol: str) -> bool:
        if symbol not in self.data or symbol not in self.last_updated:
            return False
        return (datetime.now() - self.last_updated[symbol]) < timedelta(minutes=CACHE_EXPIRY_MINUTES)
    
    def update(self, symbol: str, data: Dict[str, Any]) -> None:
        while self.lock:
            pass
        self.lock = True
        try:
            self.data[symbol] = data
            self.last_updated[symbol] = datetime.now()
            self.save_to_file()
        finally:
            self.lock = False
    
    def save_to_file(self) -> None:
        try:
            with open(DATA_CACHE_FILE, 'w', encoding='utf-8') as f:
                json.dump({
                    'data': self.data,
                    'last_updated': {k: v.isoformat() for k, v in self.last_updated.items()}
                }, f, cls=EnhancedJSONEncoder)
        except Exception as e:
            logger.error(f"Error saving cache: {e}")
    
    def load_from_file(self) -> None:
        try:
            with open(DATA_CACHE_FILE, 'r', encoding='utf-8') as f:
                cache = json.load(f)
                self.data = cache.get('data', {})  # Default to empty dict if 'data' missing
                last_updated = cache.get('last_updated', {})  # Default to empty dict if 'last_updated' missing
                if isinstance(last_updated, dict):  # Check if it's a dictionary
                    self.last_updated = {k: datetime.fromisoformat(v) for k, v in last_updated.items()}
                else:
                    self.last_updated = {}  # Reset to empty dict if invalid
        except (FileNotFoundError, json.JSONDecodeError, KeyError) as e:
            logger.warning(f"Cache load failed: {e}. Initializing with empty cache.")
            self.data = {}
            self.last_updated = {}

class AlertManager:
    def __init__(self):
        self.alerts = {}
        self.lock = False
        self.load_alerts()
    
    def load_alerts(self) -> None:
        try:
            with open(ALERTS_FILE, 'r', encoding='utf-8') as f:
                self.alerts = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            self.alerts = {}
    
    def save_alerts(self) -> None:
        while self.lock:
            pass
        self.lock = True
        try:
            with open(ALERTS_FILE, 'w', encoding='utf-8') as f:
                json.dump(self.alerts, f, cls=EnhancedJSONEncoder)
        finally:
            self.lock = False
    
    def add_alert(self, chat_id: int, symbol: str, alert_type: str, price: float) -> bool:
        try:
            chat_id_str = str(chat_id)
            if chat_id_str not in self.alerts:
                self.alerts[chat_id_str] = []
            
            self.alerts[chat_id_str].append({
                'symbol': symbol,
                'type': alert_type.lower(),
                'price': float(price),
                'created': datetime.now().isoformat(),
                'active': True
            })
            self.save_alerts()
            return True
        except Exception as e:
            logger.error(f"Error adding alert: {e}")
            return False
    
    def get_user_alerts(self, chat_id: int) -> List[Dict[str, Any]]:
        return self.alerts.get(str(chat_id), [])
    
    def remove_alert(self, chat_id: int, alert_index: int) -> bool:
        try:
            chat_id_str = str(chat_id)
            if chat_id_str in self.alerts and 0 <= alert_index < len(self.alerts[chat_id_str]):
                del self.alerts[chat_id_str][alert_index]
                self.save_alerts()
                return True
            return False
        except Exception as e:
            logger.error(f"Error removing alert: {e}")
            return False

class UserSettings:
    def __init__(self):
        self.settings = {}
        self.lock = False
        self.load_settings()
    
    def load_settings(self) -> None:
        try:
            with open(SETTINGS_FILE, 'r', encoding='utf-8') as f:
                self.settings = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            self.settings = {}
    
    def save_settings(self) -> None:
        while self.lock:
            pass
        self.lock = True
        try:
            with open(SETTINGS_FILE, 'w', encoding='utf-8') as f:
                json.dump(self.settings, f, cls=EnhancedJSONEncoder)
        finally:
            self.lock = False
    
    def get_user_settings(self, chat_id: int) -> Dict[str, Any]:
        default_settings = {
            'timezone': 'UTC',
            'update_freq': 30,
            'indicators': ['RSI', 'ATR', 'MACD'],
            'notification': True,
            'risk_appetite': 'medium',
            'symbols': ['EUR/USD']  # Default to EUR/USD
        }
        return {**default_settings, **self.settings.get(str(chat_id), {})}
    
    def update_setting(self, chat_id: int, setting: str, value: Any) -> bool:
        try:
            chat_id_str = str(chat_id)
            if chat_id_str not in self.settings:
                self.settings[chat_id_str] = {}
            self.settings[chat_id_str][setting] = value
            self.save_settings()
            return True
        except Exception as e:
            logger.error(f"Error updating setting: {e}")
            return False

# Initialize managers
price_cache = PriceDataCache()
price_cache.load_from_file()
alert_manager = AlertManager()
user_settings = UserSettings()

def load_chat_ids() -> List[int]:
    try:
        with open(CHAT_IDS_FILE, "r", encoding='utf-8') as file:
            return [int(chat_id) for chat_id in json.load(file)]
    except (FileNotFoundError, json.JSONDecodeError):
        return []

def save_chat_ids(chat_ids: List[int]) -> None:
    try:
        with open(CHAT_IDS_FILE, "w", encoding='utf-8') as file:
            json.dump(list(set(chat_ids)), file)
    except Exception as e:
        logger.error(f"Error saving chat IDs: {e}")

async def get_technical_indicators(data: pd.DataFrame) -> Dict[str, Any]:
    try:
        df = data.copy()
        df['time'] = pd.to_datetime(df.index)
        df.set_index('time', inplace=True)
        
        indicators = {}
        atr = AverageTrueRange(high=df['High'], low=df['Low'], close=df['Close'], window=14)
        indicators['ATR'] = atr.average_true_range().iloc[-1]
        rsi = RSIIndicator(close=df['Close'], window=14)
        indicators['RSI'] = rsi.rsi().iloc[-1]
        macd = MACD(close=df['Close'])
        indicators['MACD'] = macd.macd().iloc[-1]
        indicators['MACD_Signal'] = macd.macd_signal().iloc[-1]
        indicators['MACD_Hist'] = macd.macd_diff().iloc[-1]
        
        return indicators
    except Exception as e:
        logger.error(f"Error calculating indicators: {e}")
        return {}

async def get_forex_price(symbol: str) -> Optional[Dict[str, Any]]:
    yahoo_symbol = FOREX_SYMBOLS.get(symbol, symbol)
    if price_cache.is_valid(yahoo_symbol):
        return price_cache.data[yahoo_symbol]
    
    try:
        forex = yf.Ticker(yahoo_symbol)
        data = forex.history(period="1mo", interval="1h")
        
        if data.empty:
            logger.error(f"No data returned from Yahoo Finance for {symbol}")
            return None
        
        current_price = data['Close'].iloc[-1]
        indicators = await get_technical_indicators(data)
        
        price_data = {
            "symbol": symbol,
            "current_price": float(current_price),
            "open": float(data['Open'].iloc[-1]),
            "weekly_high": float(data.last('1W')['High'].max()),
            "weekly_low": float(data.last('1W')['Low'].min()),
            "daily_high": float(data.last('1D')['High'].max()),
            "daily_low": float(data.last('1D')['Low'].min()),
            "h4_high": float(data.last('4H')['High'].max()),
            "h4_low": float(data.last('4H')['Low'].min()),
            "h1_high": float(data.last('1H')['High'].max()),
            "h1_low": float(data.last('1H')['Low'].min()),
            "timestamp": datetime.now().isoformat(),
            **indicators
        }
        
        price_cache.update(yahoo_symbol, price_data)
        return price_data
    except Exception as e:
        logger.error(f"Error fetching price data for {symbol}: {e}")
        return None

async def calculate_trading_signals(price_data: Dict[str, Any], chat_id: Optional[int] = None) -> Optional[Dict[str, Any]]:
    if not price_data:
        return None
    
    try:
        current_price = price_data['current_price']
        settings = user_settings.get_user_settings(chat_id) if chat_id else {
            'indicators': ['RSI', 'ATR', 'MACD'],
            'risk_appetite': 'medium'
        }
        
        signals = {
            'support': min(price_data['h1_low'], price_data['h4_low']),
            'resistance': max(price_data['h1_high'], price_data['h4_high'])
        }
        
        if 'ATR' in settings['indicators']:
            atr = price_data['ATR']
            signals['ATR'] = atr
            risk_factor = {'low': 0.3, 'medium': 0.5, 'high': 0.7}.get(settings['risk_appetite'], 0.5)
            signals['safe_buy_zone'] = signals['support'] - (atr * risk_factor)
            signals['aggressive_buy_zone'] = signals['support'] - (atr * (risk_factor * 0.5))
            signals['sl_conservative'] = signals['support'] - (atr * 1.5)
            signals['sl_moderate'] = signals['support'] - atr
            signals['tp_1'] = current_price + (atr * 2)
        
        if 'RSI' in settings['indicators']:
            rsi = price_data['RSI']
            signals['RSI'] = rsi
            signals['rsi_signal'] = "NEUTRAL" if 30 <= rsi <= 70 else "OVERBOUGHT" if rsi > 70 else "OVERSOLD"
        
        if 'MACD' in settings['indicators']:
            signals.update({
                'MACD': price_data['MACD'],
                'MACD_Signal': price_data['MACD_Signal'],
                'MACD_Hist': price_data['MACD_Hist'],
                'macd_signal': "BULLISH" if price_data['MACD'] > price_data['MACD_Signal'] else "BEARISH"
            })
        
        signals['tp_2'] = signals['resistance']
        return signals
    except Exception as e:
        logger.error(f"Error calculating trading signals: {e}")
        return None

async def generate_trading_message(price_data: Dict[str, Any], signals: Dict[str, Any], 
                                 chat_id: Optional[int] = None) -> str:
    if not price_data or not signals:
        return "⚠️ Error fetching market data. Please try again later."
    
    try:
        settings = user_settings.get_user_settings(chat_id) if chat_id else {}
        tz = pytz.timezone(settings.get('timezone', 'UTC'))
        local_time = datetime.now(tz).strftime('%Y-%m-%d %H:%M:%S')
        
        price_change_1h = ((price_data['current_price'] - price_data['open']) / price_data['open']) * 100
        price_change_daily = ((price_data['current_price'] - price_data['daily_low']) / 
                            (price_data['daily_high'] - price_data['daily_low'])) * 100
        
        message = (
            f"🏆 <b>{price_data['symbol']} Trading Update</b> 🏆\n"
            f"⏰ {local_time} ({settings.get('timezone', 'UTC')})\n\n"
            f"💰 <b>Current Price:</b> {price_data['current_price']:.2f}\n"
            f"📈 <b>1H Change:</b> {price_change_1h:+.2f}%\n"
            f"📊 <b>Daily Range:</b> {price_change_daily:.1f}%\n\n"
            f"📉 <b>Price Ranges:</b>\n"
            f"Weekly: {price_data['weekly_low']:.2f} - {price_data['weekly_high']:.2f}\n"
            f"Daily: {price_data['daily_low']:.2f} - {price_data['daily_high']:.2f}\n"
            f"4H: {price_data['h4_low']:.2f} - {price_data['h4_high']:.2f}\n"
            f"1H: {price_data['h1_low']:.2f} - {price_data['h1_high']:.2f}\n\n"
        )
        
        if 'RSI' in settings.get('indicators', ['RSI']):
            rsi_icon = "🟢" if signals['RSI'] < 30 else "🔴" if signals['RSI'] > 70 else "🟡"
            message += f"{rsi_icon} <b>RSI (14):</b> {signals['RSI']:.2f} ({signals.get('rsi_signal', '')})\n"
        
        if 'ATR' in settings.get('indicators', ['ATR']):
            message += f"📊 <b>ATR (14):</b> {signals['ATR']:.2f}\n"
        
        if 'MACD' in settings.get('indicators', ['MACD']):
            macd_icon = "🟢" if signals.get('macd_signal') == "BULLISH" else "🔴" if signals.get('macd_signal') == "BEARISH" else "🟡"
            message += f"{macd_icon} <b>MACD:</b> {signals['MACD']:.2f} ({signals.get('macd_signal', '')})\n"
        
        message += (
            f"\n🎯 <b>Key Levels:</b>\n"
            f"🛡️ <b>Support:</b> {signals['support']:.2f}\n"
            f"🚀 <b>Resistance:</b> {signals['resistance']:.2f}\n"
        )
        
        if 'ATR' in settings.get('indicators', ['ATR']):
            message += (
                f"\n🟢 <b>Buy Zones:</b>\n"
                f"Safe: {signals.get('safe_buy_zone', 0):.2f} | "
                f"Aggressive: {signals.get('aggressive_buy_zone', 0):.2f}\n"
                f"\n⚠️ <b>Stop Losses:</b>\n"
                f"Conservative: {signals.get('sl_conservative', 0):.2f} | "
                f"Moderate: {signals.get('sl_moderate', 0):.2f}\n"
                f"\n✅ <b>Take Profits:</b>\n"
                f"TP1: {signals.get('tp_1', 0):.2f} | "
                f"TP2: {signals['tp_2']:.2f}\n"
            )
        
        recommendation = await generate_recommendation(price_data, signals, settings)
        message += f"\n📝 {recommendation}"
        return message
    except Exception as e:
        logger.error(f"Error generating trading message: {e}")
        return "⚠️ Error generating analysis."

async def generate_recommendation(price_data: Dict[str, Any], signals: Dict[str, Any], 
                                settings: Dict[str, Any]) -> str:
    try:
        current_price = price_data['current_price']
        recommendation = "⚪ <b>NEUTRAL</b> - Wait for better entry"
        
        if 'RSI' in settings.get('indicators', ['RSI']) and 'ATR' in settings.get('indicators', ['ATR']):
            if current_price <= signals.get('safe_buy_zone', 0) and signals.get('rsi_signal') == "OVERSOLD":
                recommendation = "✅ <b>STRONG BUY</b> - Oversold at safe zone"
            elif current_price >= signals['resistance'] and signals.get('rsi_signal') == "OVERBOUGHT":
                recommendation = "🔴 <b>SELL</b> - Overbought at resistance"
            elif current_price <= signals.get('aggressive_buy_zone', 0):
                recommendation = "🟡 <b>BUY</b> - Aggressive zone"
        
        if 'MACD' in settings.get('indicators', ['MACD']) and "NEUTRAL" in recommendation:
            recommendation = (
                "🟢 <b>BULLISH</b> - MACD crossover" if signals.get('macd_signal') == "BULLISH" 
                else "🔴 <b>BEARISH</b> - MACD crossover"
            )
        
        risk_note = {'low': " (Low Risk)", 'medium': "", 'high': " (High Risk)"}.get(settings.get('risk_appetite', 'medium'), "")
        return f"{recommendation}{risk_note}"
    except Exception as e:
        logger.error(f"Error generating recommendation: {e}")
        return "⚪ <b>NEUTRAL</b>"

async def check_alerts(current_price: float, symbol: str) -> None:
    try:
        for chat_id_str, alerts in alert_manager.alerts.items():
            chat_id = int(chat_id_str)
            for alert in alerts[:]:  # Copy to allow modification
                if alert.get('active', True) and alert['symbol'] == symbol:
                    alert_price = alert['price']
                    alert_type = alert['type']
                    if (alert_type == 'above' and current_price >= alert_price) or \
                       (alert_type == 'below' and current_price <= alert_price):
                        await bot.send_message(
                            chat_id=chat_id,
                            text=f"🚨 <b>ALERT</b> 🚨\n{symbol} Price {alert_type} {alert_price:.2f}\nCurrent: {current_price:.2f}",
                            parse_mode='HTML'
                        )
                        alert['active'] = False
            alert_manager.save_alerts()
    except Exception as e:
        logger.error(f"Error in check_alerts: {e}")

async def send_price_update(context: CallbackContext, chat_id: Optional[int] = None) -> None:
    try:
        recipients = [chat_id] if chat_id else load_chat_ids()
        for cid in recipients:
            settings = user_settings.get_user_settings(cid)
            for symbol in settings['symbols']:
                price_data = await get_forex_price(symbol)
                if not price_data:
                    continue
                
                await check_alerts(price_data['current_price'], symbol)
                signals = await calculate_trading_signals(price_data, cid)
                message = await generate_trading_message(price_data, signals, cid)
                await send_message_with_menu(cid, message)
    except Exception as e:
        logger.error(f"Error in send_price_update: {e}")

async def send_message_with_menu(chat_id: int, message: str) -> None:
    try:
        keyboard = [
            [KeyboardButton("🔄 Refresh"), KeyboardButton("📊 Analysis")],
            [KeyboardButton("🔔 Alerts"), KeyboardButton("⚙️ Settings")],
            [KeyboardButton("📈 Symbols"), KeyboardButton("🛑 Stop")]
        ]
        await bot.send_message(
            chat_id=chat_id,
            text=message,
            parse_mode='HTML',
            reply_markup=ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
        )
    except Exception as e:
        logger.error(f"Error sending message: {e}")

async def start(update: Update, context: CallbackContext) -> int:
    chat_id = update.effective_chat.id
    chat_ids = load_chat_ids()
    
    if chat_id not in chat_ids:
        chat_ids.append(chat_id)
        save_chat_ids(chat_ids)
    
    await update.message.reply_text(
        "🚀 <b>Forex Trading Bot</b>\nReal-time updates activated!",
        parse_mode='HTML',
        reply_markup=ReplyKeyboardMarkup([
            [KeyboardButton("🔄 Refresh"), KeyboardButton("📊 Analysis")],
            [KeyboardButton("🔔 Alerts"), KeyboardButton("⚙️ Settings")],
            [KeyboardButton("📈 Symbols"), KeyboardButton("🛑 Stop")]
        ], resize_keyboard=True)
    )
    await send_price_update(context, chat_id)
    return MENU

async def stop(update: Update, context: CallbackContext) -> int:
    chat_id = update.effective_chat.id
    chat_ids = load_chat_ids()
    
    if chat_id in chat_ids:
        chat_ids.remove(chat_id)
        save_chat_ids(chat_ids)
        await update.message.reply_text(
            "✅ Unsubscribed. Use /start to resubscribe.",
            reply_markup=ReplyKeyboardRemove()
        )
    return ConversationHandler.END

async def handle_menu(update: Update, context: CallbackContext) -> int:
    text = update.message.text
    chat_id = update.effective_chat.id
    
    if text == "🔄 Refresh":
        await send_price_update(context, chat_id)
    elif text == "📊 Analysis":
        await send_price_update(context, chat_id)
    elif text == "🔔 Alerts":
        await show_alerts_menu(update, context)
        return ALERTS
    elif text == "⚙️ Settings":
        await show_settings_menu(update, context)
        return SETTINGS
    elif text == "📈 Symbols":
        await show_symbol_selection(update, context)
        return SYMBOL_SELECTION
    elif text == "🛑 Stop":
        return await stop(update, context)
    return MENU

async def show_alerts_menu(update: Update, context: CallbackContext) -> None:
    chat_id = update.effective_chat.id
    user_alerts = alert_manager.get_user_alerts(chat_id)
    
    message = "🔔 <b>Your Alerts:</b>\n" + (
        "\n".join(f"{i+1}. {a['symbol']} {a['type'].upper()} {a['price']:.2f} ({'ACTIVE' if a['active'] else 'TRIGGERED'})" 
                 for i, a in enumerate(user_alerts)) or "No alerts set."
    )
    keyboard = [[KeyboardButton("➕ New Alert"), KeyboardButton("⬅️ Back")]] + \
               [[KeyboardButton(f"❌ Delete {i+1}")] for i in range(len(user_alerts))]
    
    await update.message.reply_text(
        message,
        parse_mode='HTML',
        reply_markup=ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
    )

async def show_settings_menu(update: Update, context: CallbackContext) -> None:
    chat_id = update.effective_chat.id
    settings = user_settings.get_user_settings(chat_id)
    
    message = (
        f"⚙️ <b>Settings</b>\n"
        f"1. Timezone: {settings['timezone']}\n"
        f"2. Frequency: {settings['update_freq']} min\n"
        f"3. Indicators: {', '.join(settings['indicators'])}\n"
        f"4. Notifications: {'ON' if settings['notification'] else 'OFF'}\n"
        f"5. Risk: {settings['risk_appetite'].upper()}\n"
        f"6. Symbols: {', '.join(settings['symbols'])}"
    )
    keyboard = [
        [KeyboardButton("1. Timezone"), KeyboardButton("2. Frequency")],
        [KeyboardButton("3. Indicators"), KeyboardButton("4. Notifications")],
        [KeyboardButton("5. Risk"), KeyboardButton("6. Symbols")],
        [KeyboardButton("⬅️ Back")]
    ]
    
    await update.message.reply_text(
        message,
        parse_mode='HTML',
        reply_markup=ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
    )

async def show_symbol_selection(update: Update, context: CallbackContext) -> None:
    chat_id = update.effective_chat.id
    settings = user_settings.get_user_settings(chat_id)
    
    message = f"📈 <b>Current Symbols:</b> {', '.join(settings['symbols'])}\nSelect or enter symbols (e.g., EUR/USD, GBP/USD):"
    keyboard = [
        [KeyboardButton("EUR/USD"), KeyboardButton("GBP/USD")],
        [KeyboardButton("USD/JPY"), KeyboardButton("USD/CHF")],
        [KeyboardButton("AUD/USD"), KeyboardButton("NZD/USD")],
        [KeyboardButton("USD/CAD"), KeyboardButton("XAU/USD")],
        [KeyboardButton("⬅️ Back")]
    ]
    
    await update.message.reply_text(
        message,
        parse_mode='HTML',
        reply_markup=ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
    )

async def handle_alerts(update: Update, context: CallbackContext) -> int:
    text = update.message.text
    chat_id = update.effective_chat.id
    
    if text == "⬅️ Back":
        return await cancel(update, context)
    elif text == "➕ New Alert":
        await show_symbol_selection(update, context)
        context.user_data['alert_setup'] = True
        return SYMBOL_SELECTION
    elif text.startswith("❌ Delete "):
        try:
            alert_index = int(text.split()[-1]) - 1
            if alert_manager.remove_alert(chat_id, alert_index):
                await update.message.reply_text("✅ Alert deleted!")
            await show_alerts_menu(update, context)
        except Exception:
            await update.message.reply_text("⚠️ Invalid selection")
        return ALERTS
    return ALERTS

async def handle_settings(update: Update, context: CallbackContext) -> int:
    text = update.message.text
    chat_id = update.effective_chat.id
    
    if text == "⬅️ Back":
        return await cancel(update, context)
    elif text == "1. Timezone":
        await update.message.reply_text(
            "Enter timezone (e.g., America/New_York):",
            reply_markup=ReplyKeyboardMarkup([
                [KeyboardButton("UTC"), KeyboardButton("America/New_York"),],
                [KeyboardButton("Asia/Tokyo"),KeyboardButton("Europe/London")],
                [KeyboardButton("Australia/Sydney"),KeyboardButton("⬅️ Back")]
                
            ], resize_keyboard=True)
        )
        return SET_TIMEZONE
    elif text == "2. Frequency":
        await update.message.reply_text(
            "Enter frequency (15-1440 min):",
            reply_markup=ReplyKeyboardMarkup([
                [KeyboardButton("15"), KeyboardButton("30"), KeyboardButton("60")],
                [KeyboardButton("⬅️ Back")]
            ], resize_keyboard=True)
        )
        return SET_UPDATE_FREQ
    elif text == "3. Indicators":
        await update.message.reply_text(
            "Enter indicators (e.g., RSI,ATR):",
            reply_markup=ReplyKeyboardMarkup([
                [KeyboardButton("RSI,ATR,MACD"), KeyboardButton("RSI,ATR")],
                [KeyboardButton("⬅️ Back")]
            ], resize_keyboard=True)
        )
        return SET_INDICATORS
    elif text == "4. Notifications":
        settings = user_settings.get_user_settings(chat_id)
        new_value = not settings['notification']
        user_settings.update_setting(chat_id, 'notification', new_value)
        await show_settings_menu(update, context)
        return SETTINGS
    elif text == "5. Risk":
        await update.message.reply_text(
            "Select risk level:",
            reply_markup=ReplyKeyboardMarkup([
                [KeyboardButton("Low"), KeyboardButton("Medium"), KeyboardButton("High")],
                [KeyboardButton("⬅️ Back")]
            ], resize_keyboard=True)
        )
        return SET_RISK
    elif text == "6. Symbols":
        await show_symbol_selection(update, context)
        return SYMBOL_SELECTION
    return SETTINGS

async def handle_symbol_selection(update: Update, context: CallbackContext) -> int:
    text = update.message.text
    chat_id = update.effective_chat.id
    
    if text == "⬅️ Back":
        if context.user_data.get('alert_setup'):
            await show_alerts_menu(update, context)
            return ALERTS
        await show_settings_menu(update, context)
        return SETTINGS
    
    symbols = [s.strip() for s in text.split(',')]
    valid_symbols = [s for s in symbols if s in FOREX_SYMBOLS]
    
    if not valid_symbols:
        await update.message.reply_text("⚠️ Invalid symbol(s). Try again:")
        return SYMBOL_SELECTION
    
    if context.user_data.get('alert_setup'):
        context.user_data['alert_symbol'] = valid_symbols[0]  # Use first symbol for alert
        await update.message.reply_text(
            f"Enter alert price for {valid_symbols[0]} (e.g., 1.2000):",
            reply_markup=ReplyKeyboardMarkup([[KeyboardButton("⬅️ Back")]], resize_keyboard=True)
        )
        return SET_ALERT_PRICE
    else:
        user_settings.update_setting(chat_id, 'symbols', valid_symbols)
        await update.message.reply_text(f"✅ Symbols updated: {', '.join(valid_symbols)}")
        return await cancel(update, context)

async def set_alert_price(update: Update, context: CallbackContext) -> int:
    text = update.message.text
    if text == "⬅️ Back":
        await show_alerts_menu(update, context)
        return ALERTS
    try:
        price = float(text)
        context.user_data['alert_price'] = price
        await update.message.reply_text(
            "Select alert type:",
            reply_markup=ReplyKeyboardMarkup([
                [KeyboardButton("Above"), KeyboardButton("Below")],
                [KeyboardButton("⬅️ Back")]
            ], resize_keyboard=True)
        )
        return SET_ALERT_TYPE
    except ValueError:
        await update.message.reply_text("⚠️ Enter a valid number:")
        return SET_ALERT_PRICE

async def set_alert_type(update: Update, context: CallbackContext) -> int:
    text = update.message.text
    chat_id = update.effective_chat.id
    
    if text == "⬅️ Back":
        await show_alerts_menu(update, context)
        return ALERTS
    elif text in ["Above", "Below"]:
        price = context.user_data.get('alert_price')
        symbol = context.user_data.get('alert_symbol')
        if alert_manager.add_alert(chat_id, symbol, text.lower(), price):
            await update.message.reply_text(f"✅ Alert set: {symbol} {text} {price:.2f}")
        context.user_data.pop('alert_setup', None)
        return await cancel(update, context)
    return SET_ALERT_TYPE

async def set_timezone(update: Update, context: CallbackContext) -> int:
    text = update.message.text
    chat_id = update.effective_chat.id
    
    if text == "⬅️ Back":
        await show_settings_menu(update, context)
        return SETTINGS
    try:
        pytz.timezone(text)
        user_settings.update_setting(chat_id, 'timezone', text)
        await update.message.reply_text(f"✅ Timezone set to {text}")
        return await cancel(update, context)
    except pytz.UnknownTimeZoneError:
        await update.message.reply_text("⚠️ Invalid timezone:")
        return SET_TIMEZONE

async def set_update_freq(update: Update, context: CallbackContext) -> int:
    text = update.message.text
    chat_id = update.effective_chat.id
    
    if text == "⬅️ Back":
        await show_settings_menu(update, context)
        return SETTINGS
    try:
        freq = int(text)
        if 15 <= freq <= 1440:
            user_settings.update_setting(chat_id, 'update_freq', freq)
            context.application.job_queue.run_repeating(
                send_price_update,
                interval=freq * 60,
                first=10
            )
            await update.message.reply_text(f"✅ Frequency set to {freq} min")
            return await cancel(update, context)
        raise ValueError
    except ValueError:
        await update.message.reply_text("⚠️ Enter 15-1440:")
        return SET_UPDATE_FREQ

async def set_indicators(update: Update, context: CallbackContext) -> int:
    text = update.message.text
    chat_id = update.effective_chat.id
    
    if text == "⬅️ Back":
        await show_settings_menu(update, context)
        return SETTINGS
    indicators = [i.strip().upper() for i in text.split(',')]
    valid = ['RSI', 'ATR', 'MACD']
    if all(i in valid for i in indicators):
        user_settings.update_setting(chat_id, 'indicators', indicators)
        await update.message.reply_text(f"✅ Indicators set: {', '.join(indicators)}")
        return await cancel(update, context)
    await update.message.reply_text("⚠️ Use RSI, ATR, MACD:")
    return SET_INDICATORS

async def set_risk(update: Update, context: CallbackContext) -> int:
    text = update.message.text
    chat_id = update.effective_chat.id
    
    if text == "⬅️ Back":
        await show_settings_menu(update, context)
        return SETTINGS
    if text in ["Low", "Medium", "High"]:
        user_settings.update_setting(chat_id, 'risk_appetite', text.lower())
        await update.message.reply_text(f"✅ Risk set to {text}")
        return await cancel(update, context)
    return SET_RISK

async def cancel(update: Update, context: CallbackContext) -> int:
    await update.message.reply_text(
        "↩️ Back to main menu",
        reply_markup=ReplyKeyboardMarkup([
            [KeyboardButton("🔄 Refresh"), KeyboardButton("📊 Analysis")],
            [KeyboardButton("🔔 Alerts"), KeyboardButton("⚙️ Settings")],
            [KeyboardButton("📈 Symbols"), KeyboardButton("🛑 Stop")]
        ], resize_keyboard=True)
    )
    return MENU

def main() -> None:
    app = Application.builder().token(TOKEN).build()
    
    conv_handler = ConversationHandler(
        entry_points=[CommandHandler('start', start)],
        states={
            MENU: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_menu)],
            SETTINGS: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_settings)],
            ALERTS: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_alerts)],
            SYMBOL_SELECTION: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_symbol_selection)],
            SET_ALERT_PRICE: [MessageHandler(filters.TEXT & ~filters.COMMAND, set_alert_price)],
            SET_ALERT_TYPE: [MessageHandler(filters.TEXT & ~filters.COMMAND, set_alert_type)],
            SET_TIMEZONE: [MessageHandler(filters.TEXT & ~filters.COMMAND, set_timezone)],
            SET_UPDATE_FREQ: [MessageHandler(filters.TEXT & ~filters.COMMAND, set_update_freq)],
            SET_INDICATORS: [MessageHandler(filters.TEXT & ~filters.COMMAND, set_indicators)],
            SET_RISK: [MessageHandler(filters.TEXT & ~filters.COMMAND, set_risk)],
        },
        fallbacks=[CommandHandler('stop', stop)]
    )
    
    app.add_handler(conv_handler)
    app.job_queue.run_repeating(send_price_update, interval=1800, first=10)  # This line should now work
    app.run_polling()

if __name__ == '__main__':
    main()
