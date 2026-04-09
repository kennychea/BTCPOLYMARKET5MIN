"""Pytest configuration — ensure safe import of cvd_5min_bot.py."""
import os

# Force paper mode and strip API keys BEFORE any test imports the bot module.
# This prevents the module-level HL setup from connecting to real services.
os.environ["PAPER_MODE"] = "true"
os.environ.pop("HYPER_LIQUID_KEY", None)
os.environ.pop("PRIVATE_KEY", None)
os.environ.pop("PUBLIC_KEY", None)
os.environ.pop("API_KEY", None)
os.environ.pop("SECRET", None)
os.environ.pop("PASSPHRASE", None)
