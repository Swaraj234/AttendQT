import os
from datetime import timezone, timedelta

# Indian Standard Time (IST) timezone used in tests and legacy code
IST = timezone(timedelta(hours=5, minutes=30))

# Backward compatible placeholder for any other helper utilities that might be imported
# (The core logic now resides in app/main.py). This file exists solely to satisfy
# legacy test imports like `from helpers.helper import IST`.
