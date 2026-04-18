"""
Copy Trading Config — read/write data/copy_config.json
"""

import contextlib
import fcntl
import json
import os
from dataclasses import dataclass, field, asdict
from typing import Dict, Optional

from config import COPY_CONFIG_FILE

_LOCK_FILE = COPY_CONFIG_FILE + ".lock"


@contextlib.contextmanager
def config_lock():
    """
    Exclusive advisory lock around copy_config.json read-modify-write cycles.
    Prevents the wallet_rotator cron and the copy_engine polling loop from
    clobbering each other's writes.  Blocking acquire — typical hold time is
    under 1 ms so contention is fine.
    """
    os.makedirs(os.path.dirname(_LOCK_FILE), exist_ok=True)
    with open(_LOCK_FILE, "w") as lf:
        fcntl.flock(lf, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lf, fcntl.LOCK_UN)


@dataclass
class CopyEntry:
    """Per-wallet copy settings.

    copy_mode:
      "live"  — execute real swaps when engine is in trade_mode=live (default, backward-compat)
      "watch" — subscribe + simulate only; paper-style PnL recorded, no real tx fired
                even when engine is in live mode. Used to evaluate candidate wallets
                alongside the real copy list before promoting them.
    """
    enabled: bool = False
    alloc_sol: float = 0.01
    last_sig: str = ""
    last_copy_ts: int = 0
    label: str = ""
    copy_mode: str = "live"
    # Strategy tag — used by dashboard to bucket stats separately.
    # "default" = normal copy/watch; "large_order" = only act on source buys
    # >= min_source_sol. CSV error field tagged "watch_large" for dashboard filter.
    strategy: str = "default"
    min_source_sol: float = 0.0


@dataclass
class CopyConfig:
    """Full copy trading config"""
    user_wallet: str = ""
    global_enabled: bool = False
    trade_mode: str = "paper"  # "paper" or "live"
    keypair_path: str = ""
    copies: Dict[str, CopyEntry] = field(default_factory=dict)

    def to_dict(self) -> dict:
        copies = {}
        for addr, entry in self.copies.items():
            d = asdict(entry)
            if not d.get("label"):
                d.pop("label", None)  # omit empty label to keep JSON clean
            copies[addr] = d
        return {
            "user_wallet": self.user_wallet,
            "global_enabled": self.global_enabled,
            "trade_mode": self.trade_mode,
            "keypair_path": self.keypair_path,
            "copies": copies,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "CopyConfig":
        copies = {}
        for addr, entry_data in data.get("copies", {}).items():
            copies[addr] = CopyEntry(**entry_data)
        return cls(
            user_wallet=data.get("user_wallet", ""),
            global_enabled=data.get("global_enabled", False),
            trade_mode=data.get("trade_mode", "paper"),
            keypair_path=data.get("keypair_path", ""),
            copies=copies,
        )


def load_copy_config() -> CopyConfig:
    """Load copy config from JSON file"""
    if not os.path.exists(COPY_CONFIG_FILE):
        return CopyConfig()
    try:
        with open(COPY_CONFIG_FILE, "r") as f:
            data = json.load(f)
        return CopyConfig.from_dict(data)
    except (json.JSONDecodeError, KeyError, TypeError) as e:
        # LOUD failure — silent fallback caused a 170-wallet config to be
        # overwritten with empty defaults on Apr 18. Never again.
        import sys
        print(f"❌ CONFIG LOAD FAILED ({type(e).__name__}: {e}) — refusing to start with empty defaults. Fix {COPY_CONFIG_FILE}.", file=sys.stderr)
        sys.exit(1)


def save_copy_config(config: CopyConfig) -> None:
    """Save copy config to JSON file (atomic write via tmp + rename)."""
    os.makedirs(os.path.dirname(COPY_CONFIG_FILE), exist_ok=True)
    tmp = COPY_CONFIG_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(config.to_dict(), f, indent=2)
    os.replace(tmp, COPY_CONFIG_FILE)


def set_user_wallet(wallet: str) -> CopyConfig:
    """Set the user's wallet address"""
    config = load_copy_config()
    config.user_wallet = wallet
    save_copy_config(config)
    return config


def toggle_copy(wallet_addr: str, enabled: bool, alloc_sol: Optional[float] = None) -> CopyConfig:
    """Enable or disable copying a specific wallet"""
    config = load_copy_config()
    if wallet_addr not in config.copies:
        config.copies[wallet_addr] = CopyEntry()
    config.copies[wallet_addr].enabled = enabled
    if alloc_sol is not None:
        config.copies[wallet_addr].alloc_sol = alloc_sol
    save_copy_config(config)
    return config


def set_alloc(wallet_addr: str, alloc_sol: float) -> CopyConfig:
    """Set allocation for a wallet"""
    config = load_copy_config()
    if wallet_addr not in config.copies:
        config.copies[wallet_addr] = CopyEntry()
    config.copies[wallet_addr].alloc_sol = max(0.001, alloc_sol)
    save_copy_config(config)
    return config


def get_enabled_copies() -> Dict[str, CopyEntry]:
    """Get all enabled copy entries"""
    config = load_copy_config()
    return {addr: entry for addr, entry in config.copies.items() if entry.enabled}


def set_global_enabled(enabled: bool) -> CopyConfig:
    """Toggle global copy trading on/off"""
    config = load_copy_config()
    config.global_enabled = enabled
    save_copy_config(config)
    return config
