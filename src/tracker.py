"""
Connection and subscription tracking for load balancing decisions.
"""
import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Optional
from contextlib import asynccontextmanager

from .config import SubscriptionConfig

logger = logging.getLogger(__name__)


@dataclass
class SubscriptionState:
    """Runtime state for a subscription."""
    config: SubscriptionConfig
    active_connections: int = 0
    total_requests: int = 0
    total_errors: int = 0
    last_429_at: Optional[float] = None
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    
    @property
    def name(self) -> str:
        return self.config.name
    
    @property
    def api_key(self) -> str:
        return self.config.api_key
    
    @property
    def max_concurrent(self) -> int:
        return self.config.max_concurrent
    
    @property
    def priority(self) -> int:
        return self.config.priority
    
    @property
    def enabled(self) -> bool:
        return self.config.enabled
    
    @property
    def available_capacity(self) -> int:
        """How many more connections this subscription can handle."""
        return max(0, self.max_concurrent - self.active_connections)
    
    def is_in_cooldown(self, cooldown_seconds: int) -> bool:
        """Check if subscription is in cooldown from a recent 429."""
        if self.last_429_at is None:
            return False
        return (time.time() - self.last_429_at) < cooldown_seconds
    
    def to_dict(self, cooldown_seconds: int) -> dict:
        """Serialize state for status endpoint."""
        return {
            "name": self.name,
            "active_connections": self.active_connections,
            "max_concurrent": self.max_concurrent,
            "available": self.available_capacity,
            "in_cooldown": self.is_in_cooldown(cooldown_seconds),
            "cooldown_remaining": max(0, int(cooldown_seconds - (time.time() - (self.last_429_at or 0)))) if self.last_429_at else 0,
            "total_requests": self.total_requests,
            "total_errors": self.total_errors,
            "enabled": self.enabled,
        }


class SubscriptionTracker:
    """
    Tracks subscription states and handles load balancing decisions.
    
    Thread-safe for concurrent access.
    """
    
    def __init__(self, subscriptions: list[SubscriptionConfig], cooldown_seconds: int = 60):
        self.cooldown_seconds = cooldown_seconds
        self._states: dict[str, SubscriptionState] = {
            sub.name: SubscriptionState(config=sub)
            for sub in subscriptions
        }
        self._global_lock = asyncio.Lock()
        logger.info(f"Initialized tracker with {len(subscriptions)} subscriptions")
    
    @property
    def subscriptions(self) -> list[SubscriptionState]:
        """Get all subscription states."""
        return list(self._states.values())
    
    def get_subscription(self, name: str) -> Optional[SubscriptionState]:
        """Get a specific subscription by name."""
        return self._states.get(name)
    
    async def select_subscription(
        self,
        client_id: Optional[str] = None,
        bot_profile: Optional[dict] = None,
        subscription_rates: Optional[dict] = None,
        account_utilization: Optional[dict] = None,
    ) -> Optional[SubscriptionState]:
        """
        Select the best subscription for a new request.
        
        Basic selection criteria:
        1. Must be enabled
        2. Must not be at max capacity
        3. Must not be in cooldown
        
        Smart routing (when profile data provided):
        4. Soft affinity - prefer bot's usual subscription
        5. Spread heavy bots across subscriptions
        6. Prefer subscriptions with more headroom
        7. Consider account utilization and reset times
        
        Args:
            client_id: The bot making the request
            bot_profile: Bot's usage profile from storage.get_bot_profile()
            subscription_rates: Recent request rates per subscription
            account_utilization: Account utilization from tracker API
        
        Returns:
            Best subscription, or None if all are unavailable.
        """
        async with self._global_lock:
            candidates = []
            
            for state in self._states.values():
                # Skip disabled
                if not state.enabled:
                    logger.debug(f"Skipping {state.name}: disabled")
                    continue
                
                # Skip at capacity
                if state.available_capacity <= 0:
                    logger.debug(f"Skipping {state.name}: at capacity ({state.active_connections}/{state.max_concurrent})")
                    continue
                
                # Skip in cooldown
                if state.is_in_cooldown(self.cooldown_seconds):
                    logger.debug(f"Skipping {state.name}: in cooldown")
                    continue
                
                candidates.append(state)
            
            if not candidates:
                logger.warning("No subscriptions available!")
                return None
            
            # Simple routing if no profile data
            if not bot_profile:
                candidates.sort(key=lambda s: (-s.available_capacity, s.priority))
                selected = candidates[0]
                logger.debug(
                    f"Selected {selected.name} (simple) "
                    f"(capacity: {selected.available_capacity})"
                )
                return selected
            
            # Smart routing with profile data
            def score_subscription(state: SubscriptionState) -> tuple:
                """
                Score a subscription for this bot. Higher is better.
                Returns tuple for sorting (higher scores first).
                
                Quota Pacing Logic:
                - Each subscription resets every 7 days
                - Expected utilization = (days_elapsed / 7) * 100%
                - Under-paced (actual < expected) → bonus score
                - Over-paced (actual > expected) → penalty score
                """
                score = 0.0
                
                # Base score: available capacity (0-5 points)
                score += state.available_capacity
                
                # Soft affinity: prefer bot's usual subscription (+3 points)
                if bot_profile.get("preferred_subscription") == state.name:
                    score += 3.0
                
                # QUOTA PACING: Balance usage across the 7-day window
                if account_utilization and state.name in account_utilization:
                    util = account_utilization[state.name]
                    hours_to_reset = util.get("hours_to_reset", 168)  # Default to 7 days
                    util_pct = util.get("utilization_pct", 0)
                    
                    # Calculate days elapsed in the 7-day window
                    # hours_to_reset counts down from 168 (7 days) to 0
                    hours_elapsed = max(0, 168 - hours_to_reset)
                    days_elapsed = hours_elapsed / 24.0
                    
                    # Expected utilization at this point in the cycle
                    expected_pct = (days_elapsed / 7.0) * 100.0
                    
                    # Pacing difference: positive = under-paced, negative = over-paced
                    pacing_diff = expected_pct - util_pct
                    
                    # Scale pacing impact: each 10% difference = 1 point
                    pacing_score = pacing_diff / 10.0
                    
                    # Cap the pacing bonus/penalty to avoid extreme swings
                    pacing_score = max(-5.0, min(5.0, pacing_score))
                    score += pacing_score
                    
                    logger.debug(
                        f"Pacing {state.name}: {util_pct:.0f}% actual vs {expected_pct:.0f}% expected "
                        f"({hours_to_reset:.0f}h to reset) → score {pacing_score:+.1f}"
                    )
                    
                    # Additional penalty for heavy bots on high-utilization accounts
                    if bot_profile.get("classification") == "heavy" and util_pct > 80:
                        score -= 3.0  # Extra penalty for heavy bots on near-limit accounts
                    
                    # Bonus for accounts close to reset with low utilization
                    # (use them up before they reset and waste capacity)
                    if hours_to_reset < 12 and util_pct < 50:
                        score += 2.0  # Push traffic to underutilized accounts near reset
                        logger.debug(f"Near-reset bonus for {state.name}: +2.0")
                
                # Request rate penalty (avoid overwhelming one subscription)
                if subscription_rates and state.name in subscription_rates:
                    rate = subscription_rates[state.name]
                    rpm = rate.get("requests_last_minute", 0)
                    if rpm > 20:  # High request rate
                        score -= 3.0
                    elif rpm > 10:
                        score -= 1.0
                
                # Priority tiebreaker (lower priority number = better)
                priority_bonus = (10 - state.priority) / 10.0
                score += priority_bonus
                
                return (score, -state.priority)
            
            # Sort by score (descending)
            candidates.sort(key=score_subscription, reverse=True)
            
            selected = candidates[0]
            logger.info(
                f"Selected {selected.name} (smart) for {client_id or 'unknown'} "
                f"[{bot_profile.get('classification', '?')}] "
                f"(capacity: {selected.available_capacity})"
            )
            return selected
    
    @asynccontextmanager
    async def acquire_connection(self, subscription: SubscriptionState):
        """
        Context manager to track an active connection.
        
        Usage:
            sub = await tracker.select_subscription()
            async with tracker.acquire_connection(sub):
                # Make the request
                ...
        """
        async with subscription._lock:
            subscription.active_connections += 1
            subscription.total_requests += 1
            logger.debug(f"{subscription.name}: acquired connection ({subscription.active_connections}/{subscription.max_concurrent})")
        
        try:
            yield
        finally:
            async with subscription._lock:
                subscription.active_connections = max(0, subscription.active_connections - 1)
                logger.debug(f"{subscription.name}: released connection ({subscription.active_connections}/{subscription.max_concurrent})")
    
    async def record_429(self, subscription: SubscriptionState):
        """Record a 429 error, putting the subscription in cooldown."""
        async with subscription._lock:
            subscription.last_429_at = time.time()
            subscription.total_errors += 1
            logger.warning(
                f"{subscription.name}: 429 received, entering cooldown for {self.cooldown_seconds}s"
            )
    
    async def record_error(self, subscription: SubscriptionState):
        """Record a non-429 error."""
        async with subscription._lock:
            subscription.total_errors += 1
    
    def get_status(self) -> dict:
        """Get current status of all subscriptions (not thread-safe, use get_status_safe)."""
        subs = [
            state.to_dict(self.cooldown_seconds)
            for state in self._states.values()
        ]
        total_active = sum(s["active_connections"] for s in subs)
        total_capacity = sum(s["max_concurrent"] for s in subs if s["enabled"])
        
        return {
            "subscriptions": subs,
            "total_active": total_active,
            "total_capacity": total_capacity,
            "available_capacity": total_capacity - total_active,
        }
    
    async def get_status_safe(self) -> dict:
        """Get current status with proper locking."""
        async with self._global_lock:
            subs = []
            for state in self._states.values():
                async with state._lock:
                    subs.append(state.to_dict(self.cooldown_seconds))
            
            total_active = sum(s["active_connections"] for s in subs)
            total_capacity = sum(s["max_concurrent"] for s in subs if s["enabled"])
            
            return {
                "subscriptions": subs,
                "total_active": total_active,
                "total_capacity": total_capacity,
                "available_capacity": total_capacity - total_active,
            }
