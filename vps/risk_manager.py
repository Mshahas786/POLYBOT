#!/usr/bin/env python3
"""
PolyBot Risk Manager v1.0
Daily limits, drawdown guards, consecutive-loss brakes, and Kelly sizing.
"""

import json
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Tuple, Optional, List, Dict, Any


class RiskManager:
    """
    Centralized risk controls for PolyBot.
    All monetary values are in USDC.
    """

    def __init__(self, cfg: Dict[str, Any], bot_dir: Path):
        self.cfg = cfg
        self.bot_dir = bot_dir
        self.state_path = bot_dir / "risk_state.json"

        # Load risk-specific config with safe defaults
        rc = cfg.get("risk_management", {})
        self.enabled = rc.get("enabled", True)
        self.daily_loss_limit = float(rc.get("daily_loss_limit_usdc", 10.0))
        self.max_drawdown_pct = float(rc.get("max_drawdown_pct", 25.0))
        self.max_consecutive_losses = int(rc.get("max_consecutive_losses", 4))
        self.cooldown_after_loss = int(rc.get("cooldown_after_loss_seconds", 300))
        self.use_kelly = rc.get("use_kelly_sizing", False)
        self.kelly_fraction = float(rc.get("kelly_fraction", 0.25))
        self.initial_bankroll = float(rc.get("initial_bankroll", 100.0))
        self.lookback_trades = int(rc.get("auto_stop_win_rate_lookback_trades", 20))
        self.win_rate_threshold = float(rc.get("auto_stop_win_rate_threshold", 45.0))
        self.max_token_price = float(rc.get("max_token_price", 0.75))
        self.slippage_bps = int(rc.get("slippage_bps", 100))

        # Volatile runtime state (not persisted)
        self.last_loss_time: Optional[float] = None
        self.block_reason: Optional[str] = None

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
            "daily_pnl": 0.0,           # +win / -loss in USDC today
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
        }

    def _save_state(self):
        try:
            # Backup existing state before overwrite
            if self.state_path.exists():
                backup_path = self.state_path.with_suffix(".json.bak")
                backup_path.write_text(self.state_path.read_text(encoding="utf-8"), encoding="utf-8")
            with open(self.state_path, "w", encoding="utf-8") as f:
                json.dump(self.state, f, indent=2)
        except Exception as e:
            print(f"[ERROR] Risk state save failed: {e}")

    def _maybe_roll_day(self):
        """Reset daily counters if the date changed."""
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if self.state.get("date") != today:
            self.state["date"] = today
            self.state["daily_pnl"] = 0.0
            self.state["daily_wins"] = 0
            self.state["daily_losses"] = 0
            # Keep consecutive losses — they don't reset at midnight
            self.state["circuit_breaker_tripped"] = False
            self.state["circuit_breaker_reason"] = None
            self.state["circuit_breaker_time"] = None
            self._save_state()

    # ── Trade Lifecycle ──────────────────────────────────────

    def record_trade(self, direction: str, bet_size: float, token_price: float, outcome: Optional[str] = None):
        """Call when a trade is placed. outcome is None until resolved."""
        self.state["total_trades_all_time"] += 1
        self._save_state()

    def record_outcome(self, win: bool, bet_size: float, token_price: float):
        """Call when a trade resolves."""
        self._maybe_roll_day()
        profit = (1.0 - token_price) * bet_size if win else -token_price * bet_size

        self.state["daily_pnl"] += profit
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

        # Update peak bankroll for drawdown calc
        if self.state["current_bankroll"] > self.state["peak_bankroll"]:
            self.state["peak_bankroll"] = self.state["current_bankroll"]

        self._save_state()

    # ── Pre-Trade Checks ─────────────────────────────────────

    def can_trade(self, token_price: float, trades_today_count: int = 0) -> Tuple[bool, str]:
        """
        Returns (allowed, reason).
        Call this BEFORE executing any trade (live or simulated).
        """
        if not self.enabled:
            return True, "risk_disabled"

        self._maybe_roll_day()

        # 1. Circuit breaker (manual or auto-trip)
        if self.state.get("circuit_breaker_tripped"):
            return False, f"CIRCUIT_BREAKER: {self.state.get('circuit_breaker_reason', 'unknown')}"

        # 2. Daily loss limit
        if self.state["daily_pnl"] <= -self.daily_loss_limit:
            return False, f"DAILY_LIMIT: Lost ${abs(self.state['daily_pnl']):.2f} (limit ${self.daily_loss_limit:.2f})"

        # 3. Drawdown guard
        peak = self.state["peak_bankroll"]
        current = self.state["current_bankroll"]
        if peak > 0:
            drawdown_pct = (peak - current) / peak * 100
            if drawdown_pct >= self.max_drawdown_pct:
                return False, f"DRAWDOWN: {drawdown_pct:.1f}% (max {self.max_drawdown_pct:.1f}%)"

        # 4. Consecutive losses
        if self.state["consecutive_losses"] >= self.max_consecutive_losses:
            return False, f"STREAK: {self.state['consecutive_losses']} losses in a row (max {self.max_consecutive_losses})"

        # 5. Cooldown after a loss
        if self.last_loss_time and self.cooldown_after_loss > 0:
            elapsed = time.time() - self.last_loss_time
            if elapsed < self.cooldown_after_loss:
                remain = int(self.cooldown_after_loss - elapsed)
                return False, f"COOLDOWN: {remain}s remaining after last loss"

        # 6. Token price guard (don't buy overpriced tokens)
        if token_price > self.max_token_price:
            return False, f"OVERPRICED: token ${token_price:.3f} > max ${self.max_token_price:.3f}"

        return True, "ok"

    # ── Position Sizing ──────────────────────────────────────

    def get_bet_size(self, base_bet: float, edge: float, confidence: float) -> float:
        """
        Returns the final bet size in USDC.
        If Kelly is enabled, scales base_bet using edge & confidence.
        Otherwise returns base_bet (capped to bankroll).
        """
        if not self.enabled or not self.use_kelly:
            return min(base_bet, self.state["current_bankroll"] * 0.1)  # Hard cap at 10% of roll

        # Simple Kelly for binary outcome: f* = (bp - q) / b
        # where b = odds received, p = prob of win, q = prob of loss
        # We approximate p from confidence, b from token_price
        p = confidence / 100.0
        q = 1.0 - p
        b = (1.0 - edge) / edge if edge > 0.01 else 1.0  # rough odds

        kelly = (b * p - q) / b if b != 0 else 0
        kelly = max(0, kelly)
        size = self.state["current_bankroll"] * kelly * self.kelly_fraction

        # Clamp between reasonable bounds
        size = max(0.5, min(size, base_bet * 2, self.state["current_bankroll"] * 0.1))
        return round(size, 2)

    # ── Win-Rate Auto-Stop (Session-Based) ───────────────────

    def check_recent_win_rate(self, recent_trades: List[Dict[str, Any]]) -> Tuple[bool, str]:
        """
        Check win rate over the LAST N resolved trades (not all-time).
        Returns (ok, reason). If not ok, bot should pause.
        """
        if not self.enabled:
            return True, ""

        resolved = [t for t in recent_trades if t.get("outcome") in ("win", "loss")]
        if len(resolved) < self.lookback_trades:
            return True, ""

        window = resolved[-self.lookback_trades:]
        wins = sum(1 for t in window if t["outcome"] == "win")
        wr = wins / len(window) * 100

        if wr < self.win_rate_threshold:
            self.trip_circuit_breaker(f"Win rate {wr:.1f}% over last {len(window)} trades (threshold {self.win_rate_threshold}%)")
            return False, self.block_reason or "win_rate_low"
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
        }
