"""
Anthropic Load Balancer - Main FastAPI Application

A reverse proxy that load balances requests across multiple Anthropic API
subscriptions to avoid rate limits and maximize throughput.
"""
import logging
import sys
from contextlib import asynccontextmanager
from datetime import datetime

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from .config import load_config, Config
from .tracker import SubscriptionTracker
from .proxy import AnthropicProxy

# Configure logging
def setup_logging(config: Config):
    """Configure logging based on config."""
    level = getattr(logging, config.logging.level.upper(), logging.INFO)
    
    if config.logging.format == "json":
        from pythonjsonlogger import jsonlogger
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(jsonlogger.JsonFormatter(
            "%(timestamp)s %(level)s %(name)s %(message)s",
            rename_fields={"timestamp": "ts", "level": "lvl"},
        ))
    else:
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(logging.Formatter(
            "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
        ))
    
    logging.root.handlers = [handler]
    logging.root.setLevel(level)
    
    # Quiet down httpx
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


# Global instances
config: Config | None = None
tracker: SubscriptionTracker | None = None
proxy: AnthropicProxy | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan handler."""
    global config, tracker, proxy
    
    # Startup
    logger = logging.getLogger(__name__)
    
    try:
        config = load_config()
    except FileNotFoundError as e:
        logger.error(str(e))
        sys.exit(1)
    
    setup_logging(config)
    logger = logging.getLogger(__name__)  # Re-get after setup
    
    logger.info("=" * 60)
    logger.info("Anthropic Load Balancer starting up")
    logger.info("=" * 60)
    
    # Initialize tracker
    tracker = SubscriptionTracker(
        subscriptions=config.subscriptions,
        cooldown_seconds=config.rate_limit.cooldown_seconds,
    )
    
    # Initialize proxy
    proxy = AnthropicProxy(tracker=tracker)
    await proxy.startup()
    
    # Log subscription info
    for sub in config.subscriptions:
        status = "enabled" if sub.enabled else "disabled"
        logger.info(f"  Subscription '{sub.name}': max_concurrent={sub.max_concurrent}, priority={sub.priority}, {status}")
    
    logger.info(f"Server listening on {config.server.host}:{config.server.port}")
    logger.info("=" * 60)
    
    yield
    
    # Shutdown
    logger.info("Shutting down...")
    await proxy.shutdown()
    logger.info("Shutdown complete")


# Create FastAPI app
app = FastAPI(
    title="Anthropic Load Balancer",
    description="Reverse proxy for load balancing across Anthropic API subscriptions",
    version="1.0.0",
    lifespan=lifespan,
)


# ============================================================================
# Admin Endpoints
# ============================================================================

@app.get("/health")
async def health():
    """Health check endpoint."""
    return {"status": "healthy", "timestamp": datetime.utcnow().isoformat()}


@app.get("/status")
async def status():
    """Get current load balancer status."""
    if tracker is None:
        return JSONResponse(
            {"error": "Not initialized"},
            status_code=503,
        )
    
    status_data = tracker.get_status()
    status_data["timestamp"] = datetime.utcnow().isoformat()
    return status_data


@app.get("/")
async def root():
    """Root endpoint with basic info."""
    return {
        "service": "Anthropic Load Balancer",
        "version": "1.0.0",
        "endpoints": {
            "health": "/health",
            "status": "/status",
            "proxy": "/v1/*",
        },
    }


# ============================================================================
# Proxy Endpoints
# ============================================================================

@app.api_route("/v1/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH"])
async def proxy_v1(request: Request, path: str):
    """Proxy all /v1/* requests to Anthropic API."""
    if proxy is None:
        return JSONResponse(
            {"error": {"type": "not_ready", "message": "Service not initialized"}},
            status_code=503,
        )
    
    return await proxy.proxy_request(request, f"/v1/{path}")


# ============================================================================
# CLI Entry Point
# ============================================================================

def main():
    """Run the server via CLI."""
    import uvicorn
    
    # Try to load config for server settings
    try:
        cfg = load_config()
        host = cfg.server.host
        port = cfg.server.port
    except FileNotFoundError:
        host = "0.0.0.0"
        port = 8080
    
    uvicorn.run(
        "src.main:app",
        host=host,
        port=port,
        workers=1,  # Single worker for consistent in-memory state
        log_level="info",
    )


if __name__ == "__main__":
    main()
