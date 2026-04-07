"""
Copy Trading Config — read/write data/copy_config.json
"""

import json
import os
from dataclasses import dataclass, field, asdict
from typing import Dict, Optional

from config import COPY_CONFIG_FILE


@dataclass
class CopyEntry:
    """Per-wallet copy settings"""
    enabled: bool = False
    alloc_sol: float = 0.01
    last_sig: str = ""
    last_copy_ts: int = 0


@dataclass
class CopyConfig:
    """Full copy trading config"""
    user_wallet: str = ""
    global_enabled: bool = False
    trade_mode: str = "paper"  # "paper" or "live"
    keypair_path: str = ""
    copies: Dict[str, CopyEntry] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "user_wallet": self.user_wallet,
            "global_enabled": self.global_enabled,
            "trade_mode": self.trade_mode,
            "keypair_path": self.keypair_path,
            "copies": {
                addr: asdict(entry) for addr, entry in self.copies.items()
            }
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
    except (json.JSONDecodeError, KeyError, TypeError):
        return CopyConfig()


def save_copy_config(config: CopyConfig) -> None:
    """Save copy config to JSON file"""
    os.makedirs(os.path.dirname(COPY_CONFIG_FILE), exist_ok=True)
    with open(COPY_CONFIG_FILE, "w") as f:
        json.dump(config.to_dict(), f, indent=2)


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
