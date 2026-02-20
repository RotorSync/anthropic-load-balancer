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
    
    async def select_subscription(self) -> Optional[SubscriptionState]:
        """
        Select the best subscription for a new request.
        
        Selection criteria:
        1. Must be enabled
        2. Must not be at max capacity
        3. Must not be in cooldown
        4. Prefer highest available capacity
        5. Tiebreaker: lowest priority number
        
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
            
            # Sort by: available capacity (desc), then priority (asc)
            candidates.sort(key=lambda s: (-s.available_capacity, s.priority))
            
            selected = candidates[0]
            logger.debug(
                f"Selected {selected.name} "
                f"(capacity: {selected.available_capacity}, priority: {selected.priority})"
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
        """Get current status of all subscriptions."""
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
