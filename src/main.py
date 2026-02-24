"""
Anthropic Load Balancer - Main FastAPI Application

A reverse proxy that load balances requests across multiple Anthropic API
subscriptions to avoid rate limits and maximize throughput.
"""
import json
import logging
import os
import stat
import sys
import time
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse, HTMLResponse, FileResponse
from fastapi.staticfiles import StaticFiles

from .config import load_config, Config, reload_config
from .tracker import SubscriptionTracker
from .proxy import AnthropicProxy
from .storage import UsageStorage
from .oauth import OAuthManager

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


def is_local_network(request: Request) -> bool:
    """Check if request is from localhost or local network (192.168.68.0/24)."""
    client = request.client
    if client is None:
        return False
    host = client.host
    # Allow localhost
    if host in ("127.0.0.1", "::1", "localhost"):
        return True
    # Allow local network subnet
    if host.startswith("192.168.68."):
        return True
    return False


# Alias for backward compatibility
is_localhost = is_local_network


# Global instances
config: Config | None = None
tracker: SubscriptionTracker | None = None
proxy: AnthropicProxy | None = None
storage: UsageStorage | None = None
oauth_manager: OAuthManager | None = None
config_path: Path | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan handler."""
    global config, tracker, proxy, storage, oauth_manager, config_path
    
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
    
    # Initialize OAuth manager
    oauth_manager = OAuthManager(config=config, config_path=str(config_path))
    await oauth_manager.startup()
    
    # Initialize proxy
    proxy = AnthropicProxy(tracker=tracker, storage=storage, oauth_manager=oauth_manager)
    await proxy.startup()
    
    # Log subscription info (names only, not tokens!)
    for sub in config.subscriptions:
        status = "enabled" if sub.enabled else "disabled"
        oauth_info = " (OAuth)" if sub.is_oauth else ""
        logger.info(f"  Subscription '{sub.name}': max_concurrent={sub.max_concurrent}, priority={sub.priority}, {status}{oauth_info}")
    
    logger.info(f"Server listening on {config.server.host}:{config.server.port}")
    logger.info("=" * 60)
    
    # Run initial OAuth refresh pass at startup for fast recovery
    logger.info("Running initial OAuth token refresh pass...")
    await oauth_manager.proactive_refresh_pass()
    
    # Start background tasks
    import asyncio
    import httpx
    
    async def update_profiles_and_utilization():
        """Background task to update bot profiles and utilization data."""
        while True:
            try:
                # Update utilization from usage API
                try:
                    async with httpx.AsyncClient(timeout=10.0) as client:
                        response = await client.get("http://localhost:5050/api/usage")
                        if response.status_code == 200:
                            data = response.json()
                            utilization = {}
                            for account in data.get("accounts", []):
                                utilization[account["id"]] = {
                                    "five_hour": account.get("five_hour", {}),
                                    "seven_day": account.get("seven_day", {}),
                                }
                            tracker.set_utilization_data(utilization)
                            logger.debug(f"Updated utilization data for {len(utilization)} accounts")
                except Exception as e:
                    logger.debug(f"Could not fetch utilization data: {e}")
                
            except Exception as e:
                logger.error(f"Error in profile update task: {e}")
            
            await asyncio.sleep(60)  # Update every 60 seconds
    
    async def proactive_token_refresh():
        """Background task to refresh OAuth tokens before they expire."""
        while True:
            try:
                await oauth_manager.proactive_refresh_pass()
                # Clear auth_failed for any subscriptions that now have valid tokens
                for sub_state in tracker.subscriptions:
                    if sub_state.auth_failed and sub_state.config.is_oauth:
                        if not oauth_manager.needs_refresh(sub_state.config):
                            await tracker.clear_auth_failed(sub_state)
            except Exception as e:
                logger.error(f"Error in token refresh task: {e}")
            await asyncio.sleep(60)
    
    # Start background tasks
    update_task = asyncio.create_task(update_profiles_and_utilization())
    refresh_task = asyncio.create_task(proactive_token_refresh())
    
    yield
    
    # Cancel background tasks
    update_task.cancel()
    refresh_task.cancel()
    try:
        await update_task
    except asyncio.CancelledError:
        pass
    try:
        await refresh_task
    except asyncio.CancelledError:
        pass
    
    # Shutdown
    logger.info("Shutting down...")
    await proxy.shutdown()
    await oauth_manager.shutdown()
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


@app.get("/admin/profiles")
async def admin_profiles(request: Request):
    """Get bot usage profiles. Localhost only."""
    if not is_local_network(request):
        raise HTTPException(status_code=403, detail="Admin endpoints are localhost only")
    
    if storage is None:
        return JSONResponse({"error": "Storage not initialized"}, status_code=503)
    
    profiles = await storage.get_bot_profiles()
    return {"profiles": profiles, "timestamp": datetime.utcnow().isoformat()}


@app.get("/admin/flow")
async def admin_flow(request: Request, minutes: int = 5):
    """
    Get token flow data for visualization.
    
    Returns client -> subscription flows for the specified time window.
    """
    if not is_local_network(request):
        raise HTTPException(status_code=403, detail="Admin endpoints are localhost only")
    
    if storage is None:
        return JSONResponse({"error": "Storage not initialized"}, status_code=503)
    
    # Clamp minutes to reasonable range
    minutes = max(1, min(60, minutes))
    
    return await storage.get_flow_data(minutes)


@app.get("/admin/limits")
async def admin_limits(request: Request):
    """Proxy to the usage API for account limits. Localhost only."""
    if not is_local_network(request):
        raise HTTPException(status_code=403, detail="Admin endpoints are localhost only")
    
    import httpx
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get("http://localhost:5050/api/usage")
            return JSONResponse(response.json())
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=502)


@app.get("/admin/tokens")
async def admin_tokens(request: Request):
    """Get OAuth token status for all subscriptions. Localhost only."""
    if not is_local_network(request):
        raise HTTPException(status_code=403, detail="Admin endpoints are localhost only")
    
    if tracker is None:
        return JSONResponse({"error": "Not initialized"}, status_code=503)
    
    now = time.time()
    tokens = []
    for sub_state in tracker.subscriptions:
        info = {
            "name": sub_state.name,
            "is_oauth": sub_state.config.is_oauth,
            "auth_failed": sub_state.auth_failed,
            "enabled": sub_state.enabled,
        }
        if sub_state.config.is_oauth:
            expires_at = sub_state.config.token_expires_at or 0
            info["token_expires_at"] = expires_at
            info["expires_in_seconds"] = max(0, int(expires_at - now))
            info["has_refresh_token"] = bool(sub_state.config.refresh_token)
        tokens.append(info)
    
    return {"tokens": tokens, "timestamp": datetime.utcnow().isoformat()}


@app.get("/admin/oauth/start")
async def admin_oauth_start(request: Request, sub: str):
    """Start OAuth flow for a subscription. Returns URL to open in browser."""
    if not is_local_network(request):
        raise HTTPException(status_code=403, detail="Admin endpoints are localhost only")

    if oauth_manager is None or config is None:
        return JSONResponse({"error": "Not initialized"}, status_code=503)

    # Verify subscription exists
    sub_names = [s.name for s in config.subscriptions]
    if sub not in sub_names:
        raise HTTPException(status_code=404, detail=f"Subscription '{sub}' not found. Available: {sub_names}")

    auth_url = oauth_manager.start_auth(sub)
    return {
        "subscription": sub,
        "auth_url": auth_url,
        "instructions": "Open auth_url in your browser, log in, then copy the code from the callback page and POST it to /admin/oauth/callback",
    }


@app.post("/admin/oauth/callback")
async def admin_oauth_callback(request: Request, sub: str, code: str):
    """Exchange OAuth code for tokens and store them for a subscription."""
    if not is_local_network(request):
        raise HTTPException(status_code=403, detail="Admin endpoints are localhost only")

    if oauth_manager is None or tracker is None:
        return JSONResponse({"error": "Not initialized"}, status_code=503)

    result = await oauth_manager.exchange_code(sub, code)

    if result["success"]:
        # Clear auth_failed if it was set
        sub_state = tracker.get_subscription(sub)
        if sub_state and sub_state.auth_failed:
            await tracker.clear_auth_failed(sub_state)
        return {
            "status": "authorized",
            "subscription": sub,
            "expires_in": result["expires_in"],
            "message": f"Tokens stored. {sub} is now active with auto-refresh.",
        }
    else:
        raise HTTPException(status_code=400, detail=result["error"])


@app.post("/admin/oauth/manual")
async def admin_oauth_manual(request: Request):
    """Manually set OAuth tokens for a subscription. Localhost only."""
    if not is_local_network(request):
        raise HTTPException(status_code=403, detail="Admin endpoints are localhost only")

    if config is None or tracker is None:
        return JSONResponse({"error": "Not initialized"}, status_code=503)

    from .config import save_config
    body = await request.json()
    sub_name = body.get("subscription")
    access_token = body.get("access_token", "").strip()
    refresh_token = body.get("refresh_token", "").strip()

    if not sub_name or not access_token:
        raise HTTPException(status_code=400, detail="subscription and access_token are required")

    # Find subscription
    sub_config = None
    for sub in config.subscriptions:
        if sub.name == sub_name:
            sub_config = sub
            break

    if not sub_config:
        sub_names = [s.name for s in config.subscriptions]
        raise HTTPException(status_code=404, detail=f"Subscription '{sub_name}' not found. Available: {sub_names}")

    # Update tokens
    sub_config.api_key = access_token
    if refresh_token:
        sub_config.refresh_token = refresh_token
        sub_config.token_expires_at = time.time() + 3600  # Assume 1h, refresh will update
    else:
        # No refresh token — clear OAuth fields so it behaves like a static key
        sub_config.refresh_token = None
        sub_config.token_expires_at = None

    # Persist
    save_config(config, str(config_path))

    # Clear auth_failed
    sub_state = tracker.get_subscription(sub_name)
    if sub_state and sub_state.auth_failed:
        await tracker.clear_auth_failed(sub_state)

    logger = logging.getLogger(__name__)
    has_refresh = bool(refresh_token)
    logger.info(f"{sub_name}: manual token update (refresh_token: {has_refresh})")

    return {
        "status": "updated",
        "subscription": sub_name,
        "has_refresh_token": has_refresh,
        "message": f"Tokens set for {sub_name}." + (" Auto-refresh enabled." if has_refresh else " No refresh token — token will not auto-refresh."),
    }


# ============================================================================
# Subscription Management Endpoints
# ============================================================================

@app.get("/admin/subscriptions")
async def admin_list_subscriptions(request: Request):
    """List all subscriptions with masked API keys. Local network only."""
    if not is_local_network(request):
        raise HTTPException(status_code=403, detail="Admin endpoints are localhost only")

    if config is None:
        return JSONResponse({"error": "Not initialized"}, status_code=503)

    def mask_key(key: str) -> str:
        if len(key) > 16:
            return key[:12] + "..." + key[-4:]
        return "***"

    subs = []
    for sub in config.subscriptions:
        subs.append({
            "name": sub.name,
            "api_key_masked": mask_key(sub.api_key),
            "max_concurrent": sub.max_concurrent,
            "priority": sub.priority,
            "enabled": sub.enabled,
        })

    return {"subscriptions": subs}


@app.post("/admin/subscriptions")
async def admin_add_subscription(request: Request):
    """Add a new subscription. Local network only."""
    global config, tracker

    if not is_local_network(request):
        raise HTTPException(status_code=403, detail="Admin endpoints are localhost only")

    logger = logging.getLogger(__name__)
    body = await request.json()

    name = body.get("name", "").strip()
    api_key = body.get("api_key", "").strip() or "sk-ant-placeholder-authorize-via-auth-tab"
    if not name:
        raise HTTPException(status_code=400, detail="name is required")

    if config and any(s.name == name for s in config.subscriptions):
        raise HTTPException(status_code=409, detail=f"Subscription '{name}' already exists")

    new_sub = {
        "name": name,
        "api_key": api_key,
        "max_concurrent": int(body.get("max_concurrent", 5)),
        "priority": int(body.get("priority", 1)),
        "enabled": bool(body.get("enabled", True)),
    }

    try:
        with open(config_path) as f:
            raw_config = json.load(f)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to read config: {e}")

    raw_config.setdefault("subscriptions", []).append(new_sub)

    try:
        with open(config_path, "w") as f:
            json.dump(raw_config, f, indent=2)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to write config: {e}")

    try:
        new_config = reload_config()
        tracker = SubscriptionTracker(
            subscriptions=new_config.subscriptions,
            cooldown_seconds=new_config.rate_limit.cooldown_seconds,
        )
        config = new_config
        logger.info(f"Added subscription '{name}'")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to reload: {e}")

    return {"status": "added", "name": name}


@app.put("/admin/subscriptions/{name}")
async def admin_update_subscription(request: Request, name: str):
    """Update a subscription. Local network only."""
    global config, tracker

    if not is_local_network(request):
        raise HTTPException(status_code=403, detail="Admin endpoints are localhost only")

    logger = logging.getLogger(__name__)
    body = await request.json()

    try:
        with open(config_path) as f:
            raw_config = json.load(f)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to read config: {e}")

    found = False
    for sub in raw_config.get("subscriptions", []):
        if sub.get("name") == name:
            if "api_key" in body and body["api_key"].strip():
                sub["api_key"] = body["api_key"].strip()
            if "max_concurrent" in body:
                sub["max_concurrent"] = int(body["max_concurrent"])
            if "priority" in body:
                sub["priority"] = int(body["priority"])
            if "enabled" in body:
                sub["enabled"] = bool(body["enabled"])
            found = True
            break

    if not found:
        raise HTTPException(status_code=404, detail=f"Subscription '{name}' not found")

    try:
        with open(config_path, "w") as f:
            json.dump(raw_config, f, indent=2)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to write config: {e}")

    try:
        new_config = reload_config()
        tracker = SubscriptionTracker(
            subscriptions=new_config.subscriptions,
            cooldown_seconds=new_config.rate_limit.cooldown_seconds,
        )
        config = new_config
        logger.info(f"Updated subscription '{name}'")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to reload: {e}")

    return {"status": "updated", "name": name}


@app.delete("/admin/subscriptions/{name}")
async def admin_delete_subscription(request: Request, name: str):
    """Delete a subscription. Local network only."""
    global config, tracker

    if not is_local_network(request):
        raise HTTPException(status_code=403, detail="Admin endpoints are localhost only")

    logger = logging.getLogger(__name__)

    try:
        with open(config_path) as f:
            raw_config = json.load(f)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to read config: {e}")

    original_count = len(raw_config.get("subscriptions", []))
    raw_config["subscriptions"] = [
        s for s in raw_config.get("subscriptions", []) if s.get("name") != name
    ]

    if len(raw_config["subscriptions"]) == original_count:
        raise HTTPException(status_code=404, detail=f"Subscription '{name}' not found")

    if len(raw_config["subscriptions"]) == 0:
        raise HTTPException(status_code=400, detail="Cannot delete last subscription")

    try:
        with open(config_path, "w") as f:
            json.dump(raw_config, f, indent=2)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to write config: {e}")

    try:
        new_config = reload_config()
        tracker = SubscriptionTracker(
            subscriptions=new_config.subscriptions,
            cooldown_seconds=new_config.rate_limit.cooldown_seconds,
        )
        config = new_config
        logger.info(f"Deleted subscription '{name}'")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to reload: {e}")

    return {"status": "deleted", "name": name}



@app.get("/admin/dashboard")
async def admin_dashboard(request: Request):
    """Serve the dashboard UI. Localhost only."""
    if not is_local_network(request):
        raise HTTPException(status_code=403, detail="Admin endpoints are localhost only")
    
    dashboard_path = Path(__file__).parent / "static" / "dashboard.html"
    if not dashboard_path.exists():
        raise HTTPException(status_code=404, detail="Dashboard not found")
    
    return FileResponse(dashboard_path, media_type="text/html")


@app.get("/")
async def root():
    """Root endpoint with basic info."""
    return {
        "service": "Anthropic Load Balancer",
        "version": "1.0.0",
        "endpoints": {
            "health": "/health",
            "status": "/status (localhost only)",
            "dashboard": "/admin/dashboard (localhost only)",
            "clients": "/admin/clients (localhost only)",
            "usage": "/admin/usage?period=day|week|month (localhost only)",
            "tokens": "/admin/tokens (localhost only)",
            "oauth_start": "/admin/oauth/start?sub=NAME (localhost only)",
            "oauth_callback": "/admin/oauth/callback?sub=NAME&code=CODE (localhost only)",
            "reload": "/admin/reload (localhost only)",
            "proxy": "/v1/*",
        },
    }


# ============================================================================
# Proxy Endpoints
# ============================================================================

def check_external_access(request: Request) -> tuple[bool, str]:
    """
    Check if request is allowed based on external access config.
    
    Returns:
        (allowed, error_message)
    """
    # Local network always allowed
    if is_local_network(request):
        return True, ""
    
    # Check if external access is enabled
    if config is None or not config.external.enabled:
        return False, "External access not enabled"
    
    # Check API token
    token = request.headers.get("x-api-token", "")
    if not token or token != config.external.api_token:
        return False, "Invalid or missing API token"
    
    # Check client whitelist (if configured)
    if config.external.allowed_clients:
        client_id = request.headers.get("x-client-id", "")
        if client_id not in config.external.allowed_clients:
            return False, f"Client '{client_id}' not in allowed list"
    
    return True, ""


@app.api_route("/v1/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH"])
async def proxy_v1(request: Request, path: str):
    """Proxy all /v1/* requests to Anthropic API."""
    # Check external access
    allowed, error = check_external_access(request)
    if not allowed:
        return JSONResponse(
            {"error": {"type": "unauthorized", "message": error}},
            status_code=401,
        )
    
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
