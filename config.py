import os
from dotenv import load_dotenv

load_dotenv()

BINGX_API_KEY = os.getenv("BINGX_API_KEY", "")
BINGX_SECRET_KEY = os.getenv("BINGX_SECRET_KEY", "")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = int(os.getenv("TELEGRAM_CHAT_ID", "0"))

SYMBOL = "DOGE-USDT"
MARGIN = 10.0
LEVERAGE = 10
POSITION_SIZE = 100.0  # USD nominal (MARGIN * LEVERAGE)

SAR_STEP = 0.02
SAR_MAX = 0.2
SMA_FAST = 50
SMA_SLOW = 100

TF_ENTRY = "5m"
TF_CONFIRM = "15m"
CANDLES_LIMIT = 200

LOOP_INTERVAL = 10  # seconds between ticks

# Paper trading: log signals without opening real orders
PAPER_MODE = os.getenv("PAPER_MODE", "true").lower() == "true"

# Per-strategy override: set SAR_PAPER_MODE=false to trade SAR live
SAR_PAPER_MODE = os.getenv("SAR_PAPER_MODE", "false").lower() == "true"

# Data directory for state files and trade log (use Railway volume mount path)
DATA_DIR = os.path.abspath(os.getenv("DATA_DIR", "."))
