"""
Structured logging for weather bot.
Colored output: GREEN=buy/profit, YELLOW=info, ORANGE=loss, RED=error
"""

import os
import sys
import logging
from datetime import datetime, timezone


class ColorFormatter(logging.Formatter):
    """Colored log formatter for terminal."""
    RESET = '\033[0m'
    GREEN = '\033[92m'
    YELLOW = '\033[93m'
    ORANGE = '\033[38;5;208m'
    RED = '\033[91m'
    CYAN = '\033[96m'
    BOLD = '\033[1m'
    DIM = '\033[2m'

    LEVEL_COLORS = {
        logging.DEBUG: DIM,
        logging.INFO: '',
        logging.WARNING: ORANGE,
        logging.ERROR: RED,
        logging.CRITICAL: RED + BOLD,
    }

    def format(self, record):
        color = self.LEVEL_COLORS.get(record.levelno, '')
        msg = record.getMessage()

        # Custom coloring based on content
        if any(w in msg for w in ['BUY', 'CONFIRMED', '📌', '🎯', '💎']):
            color = self.GREEN + self.BOLD
        elif any(w in msg for w in ['PROFIT', 'WON', 'REDEEM', '✅', '💰', '📈']):
            color = self.GREEN
        elif any(w in msg for w in ['HOLD', 'WAITING', '⏳', 'status', 'DASHBOARD']):
            color = self.YELLOW
        elif any(w in msg for w in ['LOSS', 'LOST', 'STOP', '❌', '🛑']):
            color = self.ORANGE
        elif any(w in msg for w in ['ERROR', 'FAIL', 'CRITICAL']):
            color = self.RED

        formatted = f"[{self.formatTime(record, '%H:%M:%S')}] {color}{msg}{self.RESET}"
        return formatted


def setup_logger(name: str = 'weather_bot', log_file: str = None) -> logging.Logger:
    """Create a logger with both console and file output."""
    logger = logging.getLogger(name)
    if logger.handlers:
        return logger

    from config import Config
    level = getattr(logging, Config.LOG_LEVEL.upper(), logging.INFO)
    logger.setLevel(level)

    fmt = logging.Formatter(
        '[%(asctime)s] [%(levelname)-5s] %(message)s',
        datefmt='%H:%M:%S'
    )

    # Console handler with colors
    ch = logging.StreamHandler(sys.stdout)
    ch.setLevel(level)
    ch.setFormatter(ColorFormatter())
    logger.addHandler(ch)

    # File handler
    log_path = log_file or Config.LOG_FILE
    try:
        os.makedirs(os.path.dirname(log_path) if os.path.dirname(log_path) else '.', exist_ok=True)
        from logging.handlers import RotatingFileHandler
        fh = RotatingFileHandler(log_path, maxBytes=5 * 1024 * 1024, backupCount=3, encoding='utf-8')
        fh.setLevel(logging.DEBUG)
        fh.setFormatter(logging.Formatter(
            '[%(asctime)s] [%(levelname)-5s] %(name)s — %(message)s'
        ))
        logger.addHandler(fh)
    except Exception as e:
        logger.warning(f"Could not create log file {log_path}: {e}")

    return logger


# Global logger instance
log = setup_logger()
