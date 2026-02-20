"""
Anthropic Load Balancer - Main FastAPI Application

A reverse proxy that load balances requests across multiple Anthropic API
subscriptions to avoid rate limits and maximize throughput.
"""
import logging
import os
import stat
import sys
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse

from .config import load_config, Config, reload_config
from .tracker import SubscriptionTracker
from .proxy import AnthropicProxy
from .storage import UsageStorage

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


def check_config_permissions(config_path: Path):
    """Warn if config file has overly permissive permissions."""
    logger = logging.getLogger(__name__)
    try:
        mode = os.stat(config_path).st_mode
        if mode & stat.S_IROTH:  # World-readable
            logger.warning(
                f"Config file {config_path} is world-readable! "
                f"Consider: chmod 600 {config_path}"
            )
    except OSError:
        pass


def is_localhost(request: Request) -> bool:
    """Check if request is from localhost."""
    client = request.client
    if client is None:
        return False
    host = client.host
    return host in ("127.0.0.1", "::1", "localhost")


# Global instances
config: Config | None = None
tracker: SubscriptionTracker | None = None
proxy: AnthropicProxy | None = None
storage: UsageStorage | None = None
config_path: Path | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan handler."""
    global config, tracker, proxy, storage, config_path
    
    # Startup
    logger = logging.getLogger(__name__)
    
    try:
        config = load_config()
        config_path = Path(__file__).parent.parent / "config.json"
    except FileNotFoundError as e:
        logger.error(str(e))
        sys.exit(1)
    
    setup_logging(config)
    logger = logging.getLogger(__name__)  # Re-get after setup
    
    # Security check
    if config_path:
        check_config_permissions(config_path)
    
    # Validate subscription names are unique
    names = [sub.name for sub in config.subscriptions]
    if len(names) != len(set(names)):
        logger.error("Duplicate subscription names in config!")
        sys.exit(1)
    
    logger.info("=" * 60)
    logger.info("Anthropic Load Balancer starting up")
    logger.info("=" * 60)
    
    # Initialize tracker
    tracker = SubscriptionTracker(
        subscriptions=config.subscriptions,
        cooldown_seconds=config.rate_limit.cooldown_seconds,
    )
    
    # Initialize storage
    storage = UsageStorage()
    
    # Initialize proxy
    proxy = AnthropicProxy(tracker=tracker, storage=storage)
    await proxy.startup()
    
    # Log subscription info (names only, not tokens!)
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
    # Don't expose docs publicly
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)


# ============================================================================
# Admin Endpoints (localhost only)
# ============================================================================

@app.get("/health")
async def health(request: Request):
    """Health check endpoint."""
    # Health is public for load balancer checks
    return {"status": "healthy", "timestamp": datetime.utcnow().isoformat()}


@app.get("/status")
async def status(request: Request):
    """Get current load balancer status. Localhost only."""
    if not is_localhost(request):
        raise HTTPException(status_code=403, detail="Admin endpoints are localhost only")
    
    if tracker is None:
        return JSONResponse(
            {"error": "Not initialized"},
            status_code=503,
        )
    
    status_data = await tracker.get_status_safe()
    status_data["timestamp"] = datetime.utcnow().isoformat()
    return status_data


@app.post("/admin/reload")
async def admin_reload(request: Request):
    """Reload configuration from disk. Localhost only."""
    global config, tracker
    
    if not is_localhost(request):
        raise HTTPException(status_code=403, detail="Admin endpoints are localhost only")
    
    logger = logging.getLogger(__name__)
    
    try:
        new_config = reload_config()
        
        # Validate subscription names are unique
        names = [sub.name for sub in new_config.subscriptions]
        if len(names) != len(set(names)):
            raise ValueError("Duplicate subscription names in config")
        
        # Reinitialize tracker with new config
        tracker = SubscriptionTracker(
            subscriptions=new_config.subscriptions,
            cooldown_seconds=new_config.rate_limit.cooldown_seconds,
        )
        
        config = new_config
        
        logger.info("Configuration reloaded successfully")
        return {"status": "reloaded", "subscriptions": len(config.subscriptions)}
        
    except Exception as e:
        logger.error(f"Failed to reload config: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to reload: {str(e)}")


@app.get("/admin/clients")
async def admin_clients(request: Request):
    """Get all known clients with stats. Localhost only."""
    if not is_localhost(request):
        raise HTTPException(status_code=403, detail="Admin endpoints are localhost only")
    
    if storage is None:
        return JSONResponse({"error": "Storage not initialized"}, status_code=503)
    
    clients = await storage.get_clients()
    
    # Also get live connection info from tracker
    live_status = await tracker.get_status_safe() if tracker else {"subscriptions": []}
    
    return {
        "clients": [
            {
                "client_id": c.client_id,
                "total_requests": c.total_requests,
                "total_input_tokens": c.total_input_tokens,
                "total_output_tokens": c.total_output_tokens,
                "total_tokens": c.total_input_tokens + c.total_output_tokens,
                "last_seen": c.last_seen.isoformat(),
            }
            for c in clients
        ],
        "live": live_status,
        "timestamp": datetime.utcnow().isoformat(),
    }


@app.get("/admin/usage")
async def admin_usage(request: Request, period: str = "day"):
    """Get usage statistics. Localhost only."""
    if not is_localhost(request):
        raise HTTPException(status_code=403, detail="Admin endpoints are localhost only")
    
    if storage is None:
        return JSONResponse({"error": "Storage not initialized"}, status_code=503)
    
    if period not in ("day", "week", "month"):
        raise HTTPException(status_code=400, detail="Period must be day, week, or month")
    
    usage = await storage.get_usage(period)
    
    return {
        "period": usage.period,
        "start": usage.start_time.isoformat(),
        "end": usage.end_time.isoformat(),
        "total_requests": usage.total_requests,
        "total_input_tokens": usage.total_input_tokens,
        "total_output_tokens": usage.total_output_tokens,
        "total_tokens": usage.total_input_tokens + usage.total_output_tokens,
        "by_client": usage.by_client,
        "by_subscription": usage.by_subscription,
    }


@app.get("/admin/client/{client_id}")
async def admin_client_detail(request: Request, client_id: str, period: str = "day"):
    """Get detailed usage for a specific client. Localhost only."""
    if not is_localhost(request):
        raise HTTPException(status_code=403, detail="Admin endpoints are localhost only")
    
    if storage is None:
        return JSONResponse({"error": "Storage not initialized"}, status_code=503)
    
    if period not in ("day", "week", "month"):
        raise HTTPException(status_code=400, detail="Period must be day, week, or month")
    
    return await storage.get_client_usage(client_id, period)


@app.get("/")
async def root():
    """Root endpoint with basic info."""
    return {
        "service": "Anthropic Load Balancer",
        "version": "1.0.0",
        "endpoints": {
            "health": "/health",
            "status": "/status (localhost only)",
            "clients": "/admin/clients (localhost only)",
            "usage": "/admin/usage?period=day|week|month (localhost only)",
            "reload": "/admin/reload (localhost only)",
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
