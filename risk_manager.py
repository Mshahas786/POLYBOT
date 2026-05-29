#!/usr/bin/env python3
"""
PolyBot Risk Manager v6.0 - Full Module 5: Position Sizing, Circuit Breakers, Toxic Flow Guard
"""

import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Tuple, Optional, List, Dict, Any


class RiskManager:
    """
    Centralized risk controls for PolyBot v6.0.
    All monetary values are in USDC.
    """

    def __init__(self, cfg: Dict[str, Any], bot_dir: Path):
        self.cfg = cfg
        self.bot_dir = bot_dir
        self.state_path = bot_dir / "risk_state.json"
        self._load_cfg()

    def _load_cfg(self):
        rc = self.cfg.get("risk_management", {})
        self.enabled = rc.get("enabled", True)
        # Daily limits
        self.daily_loss_limit = float(rc.get("daily_loss_limit_usdc", 10.0))
        self.hourly_loss_pct = float(rc.get("hourly_loss_pct", 5.0))
        self.max_drawdown_pct = float(rc.get("max_drawdown_pct", 15.0))
        self.max_consecutive_losses = int(rc.get("max_consecutive_losses", 4))
        self.cooldown_after_loss = int(rc.get("cooldown_after_loss_seconds", 300))
        self.cooldown_after_hourly_loss = int(rc.get("cooldown_after_hourly_loss_seconds", 3600))
        # Position sizing
        self.base_bet_pct = float(rc.get("base_bet_pct", 1.0))  # % of bankroll
        self.high_conf_bet_pct = float(rc.get("high_conf_bet_pct", 2.0))
        self.arb_bet_pct = float(rc.get("arb_bet_pct", 5.0))
        self.max_concurrent_positions = int(rc.get("max_concurrent_positions", 3))
        # Win rate monitor
        self.lookback_trades = int(rc.get("auto_stop_win_rate_lookback_trades", 20))
        self.win_rate_threshold = float(rc.get("auto_stop_win_rate_threshold", 45.0))
        # Price caps
        self.max_token_price = float(rc.get("max_token_price", 0.95))
        self.initial_bankroll = float(rc.get("initial_bankroll", 100.0))
        # Kelly
        self.use_kelly = rc.get("use_kelly_sizing", False)
        self.kelly_fraction = float(rc.get("kelly_fraction", 0.25))
        # Stale feed guard
        self.stale_feed_seconds = int(rc.get("stale_feed_seconds", 45))
        # Toxic flow
        self.toxic_lag_threshold = float(rc.get("toxic_lag_threshold", 1.5))
        self.edge_lag_max = float(rc.get("edge_lag_max", 1.5))
        self.edge_lag_min = float(rc.get("edge_lag_min", 0.3))

        self.last_loss_time: Optional[float] = None
        self.hourly_loss_triggered_time: Optional[float] = None
        self.block_reason: Optional[str] = None
        self.stale_feed_triggered: bool = False
        self._load_state()

    # ── Persistence ──────────────────────────────────────────
    def _load_state(self):
        if self.state_path.exists():
            try:
                with open(self.state_path, "r", encoding="utf-8") as f:
                    self.state = json.load(f)
            except Exception:
                self.state = self._default_state()
        else:
            self.state = self._default_state()
        self._maybe_roll_day()

    def _default_state(self) -> Dict[str, Any]:
        return {
            "date": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
            "daily_pnl": 0.0,
            "daily_wins": 0,
            "daily_losses": 0,
            "consecutive_losses": 0,
            "peak_bankroll": self.initial_bankroll,
            "current_bankroll": self.initial_bankroll,
            "total_trades_all_time": 0,
            "total_wins_all_time": 0,
            "total_losses_all_time": 0,
            "circuit_breaker_tripped": False,
            "circuit_breaker_reason": None,
            "circuit_breaker_time": None,
            "hourly_pnl": 0.0,
            "hourly_reset_time": int(time.time()),
            "position_count": 0,
            "win_rate_reduced": False,
        }

    def _save_state(self):
        try:
            if self.state_path.exists():
                backup_path = self.state_path.with_suffix(".json.bak")
                backup_path.write_text(self.state_path.read_text(encoding="utf-8"), encoding="utf-8")
            with open(self.state_path, "w", encoding="utf-8") as f:
                json.dump(self.state, f, indent=2)
        except Exception as e:
            print(f"[ERROR] Risk state save failed: {e}")

    def _maybe_roll_day(self):
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if self.state.get("date") != today:
            self.state["date"] = today
            self.state["daily_pnl"] = 0.0
            self.state["daily_wins"] = 0
            self.state["daily_losses"] = 0
            self.state["circuit_breaker_tripped"] = False
            self.state["circuit_breaker_reason"] = None
            self.state["circuit_breaker_time"] = None
            self._save_state()

    def _maybe_reset_hourly(self):
        now = int(time.time())
        last_reset = self.state.get("hourly_reset_time", 0)
        if now - last_reset >= 3600:
            self.state["hourly_pnl"] = 0.0
            self.state["hourly_reset_time"] = now
            self.state["hourly_loss_triggered_time"] = None
            self._save_state()

    # ── Trade Lifecycle ──────────────────────────────────────
    def record_trade(self, direction: str, bet_size: float, token_price: float, outcome: Optional[str] = None):
        self.state["total_trades_all_time"] += 1
        self.state["position_count"] = self.state.get("position_count", 0) + 1
        self._save_state()

    def release_position(self):
        self.state["position_count"] = max(0, self.state.get("position_count", 1) - 1)
        self._save_state()

    def record_outcome(self, win: bool, bet_size: float, token_price: float):
        self._maybe_roll_day()
        self._maybe_reset_hourly()
        profit = bet_size * (1.0 / token_price - 1.0) if win else -bet_size
        self.state["daily_pnl"] += profit
        self.state["hourly_pnl"] = self.state.get("hourly_pnl", 0.0) + profit
        self.state["current_bankroll"] += profit

        if win:
            self.state["daily_wins"] += 1
            self.state["total_wins_all_time"] += 1
            self.state["consecutive_losses"] = 0
            self.last_loss_time = None
        else:
            self.state["daily_losses"] += 1
            self.state["total_losses_all_time"] += 1
            self.state["consecutive_losses"] += 1
            self.last_loss_time = time.time()

        if self.state["current_bankroll"] > self.state["peak_bankroll"]:
            self.state["peak_bankroll"] = self.state["current_bankroll"]

        # Check hourly loss circuit breaker
        self._check_hourly_loss()

        self._save_state()

    def _check_hourly_loss(self):
        self._maybe_reset_hourly()
        start_roll = self.initial_bankroll
        current = self.state["current_bankroll"]
        session_drawdown = (start_roll - current) / start_roll * 100 if start_roll > 0 else 0
        if session_drawdown >= self.hourly_loss_pct:
            self.hourly_loss_triggered_time = time.time()
            self.state["hourly_loss_triggered_time"] = time.time()
            self.trip_circuit_breaker(
                f"HOURLY_LOSS: Drawdown {session_drawdown:.1f}% (threshold {self.hourly_loss_pct}%)"
            )

    # ── Pre-Trade Checks ─────────────────────────────────────
    def can_trade(self, token_price: float, lag_score: float = 0.0,
                  chainlink_stale_seconds: float = 0.0) -> Tuple[bool, str]:
        if not self.enabled:
            return True, "risk_disabled"

        self._maybe_roll_day()

        if self.state.get("circuit_breaker_tripped"):
            cb_reason = self.state.get("circuit_breaker_reason", "unknown")
            # Auto-reset after 60 min if hourly loss
            if "HOURLY_LOSS" in str(cb_reason) and self.hourly_loss_triggered_time:
                elapsed = time.time() - self.hourly_loss_triggered_time
                if elapsed >= self.cooldown_after_hourly_loss:
                    self.reset_circuit_breaker()
                    return True, "circuit_breaker_auto_reset"
            return False, f"CIRCUIT_BREAKER: {cb_reason}"

        # Stale feed guard
        if chainlink_stale_seconds > self.stale_feed_seconds:
            self.block_reason = f"STALE_FEED: Chainlink unchanged for {chainlink_stale_seconds:.0f}s (limit {self.stale_feed_seconds}s)"
            return False, self.block_reason

        # Toxic flow guard
        if lag_score > self.toxic_lag_threshold:
            self.block_reason = f"TOXIC_FLOW: lag_score {lag_score:.2f}% > {self.toxic_lag_threshold}%"
            return False, self.block_reason

        # Drawdown guard (full stop at 15%)
        peak = self.state["peak_bankroll"]
        current = self.state["current_bankroll"]
        if peak > 0:
            drawdown_pct = (peak - current) / peak * 100
            if drawdown_pct >= self.max_drawdown_pct:
                self.trip_circuit_breaker(f"MAX_DRAWDOWN: {drawdown_pct:.1f}% (limit {self.max_drawdown_pct}%)")
                return False, f"DRAWDOWN: {drawdown_pct:.1f}% (max {self.max_drawdown_pct:.1f}%)"

        # Daily loss limit
        if self.state["daily_pnl"] <= -self.daily_loss_limit:
            return False, f"DAILY_LIMIT: Lost ${abs(self.state['daily_pnl']):.2f} (limit ${self.daily_loss_limit:.2f})"

        # Consecutive losses
        if self.state["consecutive_losses"] >= self.max_consecutive_losses:
            return False, f"STREAK: {self.state['consecutive_losses']} losses (max {self.max_consecutive_losses})"

        # Cooldown after loss
        if self.last_loss_time and self.cooldown_after_loss > 0:
            elapsed = time.time() - self.last_loss_time
            if elapsed < self.cooldown_after_loss:
                remain = int(self.cooldown_after_loss - elapsed)
                return False, f"COOLDOWN: {remain}s after loss"

        # Token price guard
        if token_price > self.max_token_price:
            return False, f"OVERPRICED: token ${token_price:.3f} > max ${self.max_token_price:.3f}"

        return True, "ok"

    # ── Position Sizing ──────────────────────────────────────
    def get_bet_size(self, base_bet: float, edge: float, confidence: float,
                     is_high_conf: bool = False, is_arb: bool = False) -> float:
        if not self.enabled:
            return min(base_bet, 10.0)

        bankroll = self.state["current_bankroll"]
        if bankroll <= 0:
            return 0.5

        if is_arb:
            # Arb: up to 5% of bankroll
            size = bankroll * (self.arb_bet_pct / 100.0)
            size = min(size, base_bet * 5)
            return round(max(0.5, size), 2)

        if is_high_conf:
            pct = self.high_conf_bet_pct
        else:
            pct = self.base_bet_pct

        # Check if win-rate reduction is active
        if self.state.get("win_rate_reduced", False):
            pct *= 0.5

        size = bankroll * (pct / 100.0)

        if self.use_kelly and edge > 0.01:
            p = confidence / 100.0
            q = 1.0 - p
            b = (1.0 - edge) / edge if edge > 0.01 else 1.0
            kelly = max(0, (b * p - q) / b) if b != 0 else 0
            kelly_size = bankroll * kelly * self.kelly_fraction
            size = min(size, kelly_size)

        size = max(0.5, min(size, bankroll * 0.1))
        return round(size, 2)

    # ── Win-Rate Monitor ────────────────────────────────────
    def check_recent_win_rate(self, recent_trades: List[Dict[str, Any]]) -> Tuple[bool, str]:
        if not self.enabled:
            return True, ""

        resolved = [t for t in recent_trades if t.get("outcome") in ("win", "loss")]
        if len(resolved) < self.lookback_trades:
            return True, ""

        window = resolved[-self.lookback_trades:]
        wins = sum(1 for t in window if t["outcome"] == "win")
        wr = wins / len(window) * 100

        if wr < self.win_rate_threshold:
            self.state["win_rate_reduced"] = True
            self._save_state()
            return True, f"WIN_RATE_REDUCED: {wr:.1f}% over last {len(window)} (below {self.win_rate_threshold}%)"
        else:
            if self.state.get("win_rate_reduced", False):
                self.state["win_rate_reduced"] = False
                self._save_state()
            return True, ""

    def trip_circuit_breaker(self, reason: str):
        self.state["circuit_breaker_tripped"] = True
        self.state["circuit_breaker_reason"] = reason
        self.state["circuit_breaker_time"] = datetime.now(timezone.utc).isoformat()
        self.block_reason = reason
        self._save_state()

    def reset_circuit_breaker(self):
        self.state["circuit_breaker_tripped"] = False
        self.state["circuit_breaker_reason"] = None
        self.state["circuit_breaker_time"] = None
        self.state["hourly_loss_triggered_time"] = None
        self.hourly_loss_triggered_time = None
        self.block_reason = None
        self._save_state()

    def reset_all_blocks(self):
        self.state["circuit_breaker_tripped"] = False
        self.state["circuit_breaker_reason"] = None
        self.state["circuit_breaker_time"] = None
        self.state["consecutive_losses"] = 0
        self.state["win_rate_reduced"] = False
        self.state["position_count"] = 0
        self.state["hourly_loss_triggered_time"] = None
        self.state["hourly_pnl"] = 0.0
        self.last_loss_time = None
        self.hourly_loss_triggered_time = None
        self.block_reason = None
        self._save_state()

    # ── Stats for API ────────────────────────────────────────
    def get_stats(self) -> Dict[str, Any]:
        self._maybe_roll_day()
        peak = self.state["peak_bankroll"]
        current = self.state["current_bankroll"]
        drawdown = ((peak - current) / peak * 100) if peak > 0 else 0.0
        return {
            "daily_pnl": round(self.state["daily_pnl"], 2),
            "daily_wins": self.state["daily_wins"],
            "daily_losses": self.state["daily_losses"],
            "consecutive_losses": self.state["consecutive_losses"],
            "current_bankroll": round(current, 2),
            "peak_bankroll": round(peak, 2),
            "drawdown_pct": round(drawdown, 2),
            "circuit_breaker_tripped": self.state["circuit_breaker_tripped"],
            "circuit_breaker_reason": self.state["circuit_breaker_reason"],
            "total_trades": self.state["total_trades_all_time"],
            "total_wins": self.state["total_wins_all_time"],
            "total_losses": self.state["total_losses_all_time"],
            "position_count": self.state.get("position_count", 0),
            "win_rate_reduced": self.state.get("win_rate_reduced", False),
            "hourly_pnl": round(self.state.get("hourly_pnl", 0.0), 2),
            "base_bet_pct": self.base_bet_pct,
            "max_drawdown_pct": self.max_drawdown_pct,
            "stale_feed_seconds": self.stale_feed_seconds,
            "toxic_lag_threshold": self.toxic_lag_threshold,
        }
