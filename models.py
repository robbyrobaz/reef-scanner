"""
Data models for Reef Scanner
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional


@dataclass
class WalletMetrics:
    """Aggregated metrics for a wallet"""
    address: str
    total_trades: int = 0
    buy_count: int = 0
    sell_count: int = 0
    win_count: int = 0
    loss_count: int = 0
    avg_roi: float = 0.0
    best_roi: float = 0.0
    worst_roi: float = 0.0
    avg_hold_time_seconds: int = 0
    last_active: Optional[datetime] = None
    favorite_token: str = ""
    trade_pairs: list = field(default_factory=list)

    @property
    def win_rate(self) -> float:
        if self.total_trades == 0:
            return 0.0
        return self.win_count / self.total_trades

    @property
    def score(self) -> float:
        """Weighted score across all metrics"""
        from config import WEIGHT_WIN_RATE, WEIGHT_AVG_ROI, WEIGHT_TRADE_FREQ, WEIGHT_RECENCY

        # Recency score: 1.0 if active < 1 day, 0.0 if > 7 days
        recency_score = 0.0
        if self.last_active:
            days_ago = (datetime.now().astimezone() - self.last_active).days
            recency_score = max(0.0, 1.0 - (days_ago / 7))

        freq_score = min(1.0, self.total_trades / 50)  # 50 trades = max freq score

        return (
            WEIGHT_WIN_RATE * self.win_rate +
            WEIGHT_AVG_ROI * min(1.0, max(0.0, self.avg_roi)) +
            WEIGHT_TRADE_FREQ * freq_score +
            WEIGHT_RECENCY * recency_score
        )
