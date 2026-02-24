# Buck ALB Routing Fix — Postmortem

**Date:** 2026-02-23
**Duration:** ~4 hours of debugging
**Status:** ✅ Resolved
**Affected Agent:** Buck (Norman's AI, Mac Mini 192.168.68.92)

---

## Summary

Buck was bypassing the Anthropic Load Balancer (ALB) at `192.168.68.181:8080` and sending API requests directly to `api.anthropic.com`. With a fake/expired API key, this resulted in 401 errors. The root cause was a validation error in `models.json` that silently discarded all provider overrides, compounded by two auth format issues.

## Architecture

All AI agents (Echo, Buck, Forge, Anvil, Scout, Jarvis) are supposed to route Anthropic API requests through our ALB proxy. The ALB:
- Strips incoming auth tokens (fake or real)
- Substitutes the correct subscription API key
- Tracks usage per-client via `X-Client-ID` header
- Load balances across multiple Anthropic subscriptions

## Root Cause — Three Cascading Issues

### Issue 1: `models.json` Validation Bomb (Primary)

Buck's `models.json` contained two provider entries:

```json
{
  "providers": {
    "anthropic-lb": {
      "baseUrl": "http://192.168.68.181:8080",
      "api": "anthropic-messages",
      "headers": { "X-Client-ID": "buck" },
      "models": [
        { "id": "claude-opus-4-6", "name": "Claude Opus 4 (LB)", ... }
      ]
    },
    "anthropic": {
      "baseUrl": "http://192.168.68.181:8080",
      "headers": { "X-Client-ID": "buck" },
      "models": []
    }
  }
}
```

The `anthropic-lb` provider defined custom models but had **no `apiKey` field**. OpenClaw's `ModelRegistry.validateConfig()` requires `apiKey` when custom models are defined:

```javascript
if (!providerConfig.apiKey) {
    throw new Error(`Provider ${providerName}: "apiKey" is required when defining custom models.`);
}
```

This error was caught by a try/catch that returned `emptyCustomModelsResult()` — **discarding ALL overrides from the entire file**, including the valid `anthropic` override-only entry that would have set `baseUrl` to the ALB.

Without the override, built-in Anthropic models kept their hardcoded `baseUrl: "https://api.anthropic.com"` from `models.generated.js` in the `@mariozechner/pi-ai` package.

### Issue 2: `auth-profiles.json` Wrong Type Format

The auth-profiles file had:
```json
{ "type": "api-key", ... }
```

OpenClaw's `coerceAuthStore()` only accepts `"api_key"` (underscore), `"oauth"`, or `"token"`:
```javascript
if (typed.type !== "api_key" && typed.type !== "oauth" && typed.type !== "token") continue;
```

The entry with `"api-key"` (hyphen) was **silently skipped**, causing "No API key found for provider anthropic" errors.

### Issue 3: OAuth Token Prefix Confusion

The original `auth.json` stored a key with prefix `sk-ant-oat01-` (OAuth format). Even though the `type` field said `"api_key"`, the Anthropic SDK detects OAuth tokens by their prefix and routes them through a different code path. This is by design — `oat01` tokens are OAuth tokens regardless of how they're labeled in config.

## Fix Applied

### `models.json` — Removed the problematic `anthropic-lb` provider
```json
{
  "providers": {
    "anthropic": {
      "baseUrl": "http://192.168.68.181:8080",
      "headers": { "X-Client-ID": "buck" }
    }
  }
}
```

This is an "override-only" config (no `models` array). It passes validation and correctly overrides the `baseUrl` and `headers` for all built-in Anthropic models.

### `auth.json` — Changed to API key prefix
```json
{
  "anthropic": {
    "type": "api_key",
    "key": "sk-ant-api03-alb-proxy-000...000-AA"
  }
}
```

The `api03` prefix tells the SDK this is a standard API key (not OAuth). The key itself is fake — the ALB strips it and substitutes the real subscription key.

### `auth-profiles.json` — Fixed type format
```json
{
  "version": 1,
  "profiles": {
    "anthropic:default": {
      "type": "api_key",
      "provider": "anthropic",
      "key": "sk-ant-api03-alb-proxy-000...000-AA"
    }
  }
}
```

## How We Found It

The debugging process involved progressively adding `console.error` debug patches to trace the model resolution pipeline:

1. **`anthropic.js` (`createClient`)** — Confirmed `model.baseUrl` was `api.anthropic.com` when it should be ALB
2. **`model-registry.js` (`loadBuiltInModels`)** — Found `overrides.get('anthropic')` returned `NONE`
3. **`model-registry.js` (`loadCustomModels`)** — Found the file existed and parsed, schema validated, but the providers loop never executed
4. **`model-registry.js` (catch block)** — Found `Error: Provider anthropic-lb: "apiKey" is required when defining custom models`

The validation error was being caught and silently swallowed, returning empty overrides. No error was logged to gateway stderr by default — the error was stored in `this.loadError` but not surfaced.

## Verification

After the fix, debug logs confirmed:
```
claude-opus-4-6 baseUrl after loadBuiltInModels: http://192.168.68.181:8080 ✅
claude-opus-4-6 baseUrl after merge+OAuth: http://192.168.68.181:8080 ✅
```

ALB database confirmed 3 successful requests from Buck at 22:53 UTC, all status 200.

## Lessons Learned

1. **One bad provider entry kills ALL overrides.** OpenClaw's `models.json` validation is all-or-nothing. If any provider fails validation, the entire file is treated as if it doesn't exist. This is a design limitation — ideally, valid providers should still load even if one fails.

2. **Silent failures are the worst kind.** The validation error was caught and stored in `loadError` but never logged to stderr. Hours of debugging could have been saved by a single warning line.

3. **Type format matters: `api_key` not `api-key`.** The auth store coercion silently skips entries with unrecognized types. No warning is emitted.

4. **Token prefix determines behavior, not config labels.** `sk-ant-oat01-` is always treated as OAuth regardless of what `type` field says. Use `sk-ant-api03-` for API key behavior.

5. **Override-only providers don't need `apiKey` or `models`.** A provider entry with just `baseUrl` (and optionally `headers`) acts as an overlay on built-in models. Don't add a `models` array unless you're defining entirely new/custom models with their own `apiKey`.

## Configuration Template for Other Agents

For any agent that needs to route through the ALB:

**`models.json`** (in agent dir: `~/.openclaw/agents/main/agent/models.json`):
```json
{
  "providers": {
    "anthropic": {
      "baseUrl": "http://192.168.68.181:8080",
      "headers": {
        "X-Client-ID": "<agent-name>"
      }
    }
  }
}
```

**`auth.json`** (same directory):
```json
{
  "anthropic": {
    "type": "api_key",
    "key": "sk-ant-api03-alb-proxy-00000000000000000000000000000000000000000000000000-000000000000000000000000-AA"
  }
}
```

The key is fake — ALB strips and replaces it. The `api03` prefix ensures the SDK treats it as a standard API key.

## Cleanup Remaining

- [ ] Restore `anthropic.js.bak` and `model-registry.js.bak` on Mac Mini (SSH key was removed — need Austin or Norman to re-add)
- [x] Restored Echo's local `anthropic.js` from backup
- [x] Updated daily log and memory

## Files Modified on Mac Mini

| File | Change | Backup |
|------|--------|--------|
| `~jarvis/.node/.../pi-ai/dist/providers/anthropic.js` | Debug logging in `createClient` | `.bak` exists |
| `~jarvis/.node/.../pi-coding-agent/dist/core/model-registry.js` | Debug logging in `loadModels`/`loadCustomModels` | `.bak` exists |
| `~jarvis/buck-home/.openclaw/agents/main/agent/models.json` | Removed `anthropic-lb`, kept `anthropic` override | N/A (was the fix) |
| `~jarvis/buck-home/.openclaw/agents/main/agent/auth.json` | Changed `oat01` → `api03` key | `.bak` exists |
| `~jarvis/buck-home/.openclaw/agents/main/agent/auth-profiles.json` | Fixed `type` format, changed key prefix | N/A |
