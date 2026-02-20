# Anthropic Load Balancer

A reverse proxy that load balances requests across multiple Anthropic API subscriptions. Designed for environments with multiple OpenClaw instances that need to share API capacity without hitting rate limits.

## Features

- **Smart routing** — Routes requests to the subscription with most available capacity
- **Real-time tracking** — Monitors active connections per subscription
- **SSE streaming** — Full support for streaming responses
- **Auto-failover** — Retries with different subscription on 429 errors
- **Quota awareness** — Integrates with existing usage tracker for daily quota data
- **Zero client changes** — Just set `ANTHROPIC_BASE_URL` to point here

## Architecture

```
┌─────────────┐     ┌─────────────────┐     ┌──────────────────┐
│  OpenClaw   │────▶│  Load Balancer  │────▶│  Anthropic API   │
│  instances  │◀────│   (FastAPI)     │◀────│  (multi-key)     │
└─────────────┘     └─────────────────┘     └──────────────────┘
                           │
                    ┌──────┴──────┐
                    │   Tracker   │
                    │  (in-mem)   │
                    └─────────────┘
```

## Requirements

- Python 3.11+
- FastAPI
- httpx
- uvicorn

## Installation

```bash
# Clone the repo
git clone https://github.com/RotorSync/anthropic-load-balancer.git
cd anthropic-load-balancer

# Create virtual environment
python3 -m venv venv
source venv/bin/activate

# Install dependencies
pip install -r requirements.txt

# Copy and configure
cp config.example.json config.json
# Edit config.json with your subscription details
```

## Authentication

The load balancer supports both authentication methods:

- **OAuth tokens** — Used with `Authorization: Bearer <token>` header (auto-detected)
- **API keys** — Keys starting with `sk-ant-` use `x-api-key` header

OAuth tokens are auto-detected: if the key doesn't start with `sk-ant-`, it's treated as an OAuth token.

## Configuration

Edit `config.json`:

```json
{
  "subscriptions": [
    {
      "name": "hh",
      "api_key": "sk-ant-...",
      "max_concurrent": 5,
      "priority": 1
    },
    {
      "name": "cam",
      "api_key": "sk-ant-...",
      "max_concurrent": 5,
      "priority": 2
    }
  ],
  "server": {
    "host": "0.0.0.0",
    "port": 8080
  },
  "rate_limit": {
    "cooldown_seconds": 60,
    "burst_limit": 10
  }
}
```

### Configuration Options

| Field | Description |
|-------|-------------|
| `subscriptions[].name` | Human-readable name for logging |
| `subscriptions[].api_key` | Anthropic OAuth token or API key (auto-detected) |
| `subscriptions[].max_concurrent` | Max simultaneous requests for this subscription |
| `subscriptions[].priority` | Tiebreaker (lower = preferred) |
| `server.host` | Bind address |
| `server.port` | Bind port |
| `rate_limit.cooldown_seconds` | How long to avoid a subscription after 429 |
| `rate_limit.burst_limit` | Max requests per second per subscription |

## Usage

### Start the server

```bash
# Development
uvicorn src.main:app --reload --host 0.0.0.0 --port 8080

# Production
uvicorn src.main:app --host 0.0.0.0 --port 8080 --workers 1
```

> **Note:** Use `--workers 1` to ensure in-memory state is consistent. For multi-worker setups, Redis would be needed.

### Configure OpenClaw

OpenClaw needs two things: the load balancer URL and a client ID for tracking.

#### Basic Setup (LAN)

Edit `~/.openclaw/openclaw.json`:

```json
{
  "anthropic": {
    "baseUrl": "http://192.168.68.181:8080",
    "headers": {
      "X-Client-ID": "your-bot-name"
    }
  }
}
```

The `X-Client-ID` header identifies your bot in the dashboard. Use a short, descriptive name like `echo`, `forge`, `jarvis`, etc.

#### External Access (via Cloudflare Tunnel)

For bots outside your LAN (e.g., remote deployments), use the tunnel endpoint with Cloudflare Access authentication:

```json
{
  "anthropic": {
    "baseUrl": "https://api.rotorsync.com",
    "headers": {
      "X-Client-ID": "conrad",
      "CF-Access-Client-Id": "<service-token-id>",
      "CF-Access-Client-Secret": "<service-token-secret>"
    }
  }
}
```

To get Cloudflare Service Token credentials:
1. Go to Cloudflare Zero Trust → Access → Service Auth
2. Create a new Service Token
3. Copy the Client ID and Client Secret

#### Environment Variable Alternative

You can also set the base URL via environment variable:

```bash
export ANTHROPIC_BASE_URL=http://192.168.68.181:8080
```

Note: Headers must be configured in `openclaw.json` — they can't be set via environment variables.

#### Verifying Your Setup

After configuring, check that your bot appears in the dashboard:

1. Send a message to your bot (trigger an API call)
2. Open http://192.168.68.181:8080/admin/dashboard
3. Your bot should appear in the "Clients" table with request/token counts

#### Multiple Bots Example

Here's a complete setup for multiple OpenClaw instances:

| Bot | Config Location | X-Client-ID |
|-----|-----------------|-------------|
| Echo | `austin-laptop:~/.openclaw/openclaw.json` | `echo` |
| Forge | `aliyan-mac:~/.openclaw/openclaw.json` | `forge` |
| Jarvis | `norman-pi:~/.openclaw/openclaw.json` | `jarvis` |
| Conrad | Remote via tunnel | `conrad` |

Each bot uses the same load balancer but is tracked separately in the dashboard.

## API Endpoints

### Proxy Endpoints

All standard Anthropic API endpoints are proxied:

- `POST /v1/messages` — Chat completions (streaming supported)
- `POST /v1/complete` — Legacy completions

### Admin Endpoints

- `GET /health` — Health check
- `GET /status` — Current load balancer status (connections per subscription)
- `GET /metrics` — Prometheus-compatible metrics (future)

## Load Balancing Algorithm

1. **Filter** — Exclude subscriptions that are:
   - At max concurrent connections
   - In cooldown (recent 429)
   - Disabled

2. **Score** — Remaining subscriptions scored by:
   - Available capacity (max_concurrent - active_connections)
   - Daily quota remaining (if tracker integration enabled)

3. **Select** — Highest scoring subscription wins. Ties broken by priority.

## Monitoring

### Status endpoint

```bash
curl http://localhost:8080/status
```

```json
{
  "subscriptions": [
    {
      "name": "hh",
      "active_connections": 2,
      "max_concurrent": 5,
      "available": 3,
      "in_cooldown": false,
      "total_requests": 1523,
      "total_errors": 3
    }
  ],
  "total_active": 5,
  "total_capacity": 20
}
```

### Logs

Logs include:
- Request routing decisions
- Subscription selection reasons
- 429 errors and cooldown triggers
- Connection lifecycle events

## Deployment

### Systemd Service

```bash
sudo cp systemd/anthropic-lb.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable anthropic-lb
sudo systemctl start anthropic-lb
```

### Running alongside anthropic-tracker

This service is designed to run on `192.168.68.181` alongside the existing `anthropic-tracker` service. They use different ports and can share quota data.

## Troubleshooting

### All requests failing

1. Check subscription API keys are valid
2. Verify Anthropic API is reachable from the server
3. Check `/status` for cooldown states

### Uneven distribution

- Check `max_concurrent` settings match actual subscription limits
- Review `/status` for connection counts
- Check logs for 429 patterns

### High latency

- The proxy adds minimal overhead (<10ms typically)
- Check network latency to Anthropic
- Ensure not running in debug mode in production

## Development

```bash
# Run tests
pytest

# Format code
black src/

# Type check
mypy src/
```

## License

Internal use — Headings Helicopters

## Authors

- Anvil 🔨 (implementation)
- ECHO 🔊 (design review)
- Austin (requirements)
