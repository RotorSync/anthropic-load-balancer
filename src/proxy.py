"""
HTTP proxy logic for forwarding requests to Anthropic API.
"""
import logging
from typing import AsyncIterator

import httpx
from fastapi import Request, Response
from fastapi.responses import StreamingResponse

from .tracker import SubscriptionTracker, SubscriptionState

logger = logging.getLogger(__name__)

ANTHROPIC_BASE_URL = "https://api.anthropic.com"

# Headers to pass through (not modify)
PASSTHROUGH_HEADERS = {
    "anthropic-version",
    "content-type",
    "accept",
    "anthropic-beta",
}

# Headers we add/modify
SKIP_REQUEST_HEADERS = {
    "host",
    "authorization",
    "x-api-key",
    "content-length",  # httpx handles this
    "transfer-encoding",
}

# Response headers to skip (hop-by-hop or handled by FastAPI)
SKIP_RESPONSE_HEADERS = {
    "content-encoding",
    "content-length",
    "transfer-encoding",
    "connection",
}


class AnthropicProxy:
    """
    Proxies requests to Anthropic API with load balancing.
    """
    
    def __init__(self, tracker: SubscriptionTracker, timeout: float = 300.0):
        self.tracker = tracker
        self.timeout = timeout
        self._client: httpx.AsyncClient | None = None
    
    async def startup(self):
        """Initialize the HTTP client."""
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(self.timeout, connect=10.0),
            limits=httpx.Limits(max_keepalive_connections=20, max_connections=100),
        )
        logger.info("Proxy HTTP client initialized")
    
    async def shutdown(self):
        """Close the HTTP client."""
        if self._client:
            await self._client.aclose()
            logger.info("Proxy HTTP client closed")
    
    @property
    def client(self) -> httpx.AsyncClient:
        if self._client is None:
            raise RuntimeError("Proxy not initialized. Call startup() first.")
        return self._client
    
    def _build_headers(self, request: Request, subscription: SubscriptionState) -> dict:
        """Build headers for the upstream request."""
        headers = {}
        
        # Pass through relevant headers
        for key, value in request.headers.items():
            if key.lower() not in SKIP_REQUEST_HEADERS:
                headers[key] = value
        
        # Set the API key for the selected subscription
        headers["x-api-key"] = subscription.api_key
        
        # Remove any existing authorization header
        headers.pop("authorization", None)
        
        return headers
    
    def _filter_response_headers(self, headers: httpx.Headers) -> dict:
        """Filter response headers for the client."""
        return {
            key: value
            for key, value in headers.items()
            if key.lower() not in SKIP_RESPONSE_HEADERS
        }
    
    async def _stream_response(
        self,
        response: httpx.Response,
        subscription: SubscriptionState,
    ) -> AsyncIterator[bytes]:
        """Stream response chunks to the client."""
        try:
            async for chunk in response.aiter_bytes():
                yield chunk
        except Exception as e:
            logger.error(f"Error streaming response from {subscription.name}: {e}")
            raise
        finally:
            await response.aclose()
    
    async def proxy_request(self, request: Request, path: str) -> Response:
        """
        Proxy a request to Anthropic API.
        
        Args:
            request: The incoming FastAPI request.
            path: The API path (e.g., "/v1/messages").
            
        Returns:
            Response to send to the client.
        """
        # Select a subscription
        subscription = await self.tracker.select_subscription()
        
        if subscription is None:
            logger.error("No subscriptions available for request")
            return Response(
                content='{"error": {"type": "overloaded", "message": "All API subscriptions are currently at capacity. Please retry."}}',
                status_code=503,
                media_type="application/json",
            )
        
        # Build the upstream URL
        url = f"{ANTHROPIC_BASE_URL}{path}"
        if request.url.query:
            url = f"{url}?{request.url.query}"
        
        # Get request body
        body = await request.body()
        
        # Build headers with the selected subscription's API key
        headers = self._build_headers(request, subscription)
        
        # Check if this is a streaming request
        is_streaming = b'"stream":true' in body or b'"stream": true' in body
        
        logger.info(
            f"Routing request to {subscription.name}: {request.method} {path} "
            f"(streaming: {is_streaming})"
        )
        
        # Make the request within the connection tracking context
        async with self.tracker.acquire_connection(subscription):
            try:
                if is_streaming:
                    # For streaming, we need to return a StreamingResponse
                    response = await self.client.send(
                        self.client.build_request(
                            method=request.method,
                            url=url,
                            headers=headers,
                            content=body,
                        ),
                        stream=True,
                    )
                    
                    # Check for rate limit
                    if response.status_code == 429:
                        await response.aclose()
                        await self.tracker.record_429(subscription)
                        # For 429, return error (could implement retry logic here)
                        return Response(
                            content='{"error": {"type": "rate_limit", "message": "Rate limited. Please retry."}}',
                            status_code=429,
                            media_type="application/json",
                        )
                    
                    # Return streaming response
                    return StreamingResponse(
                        self._stream_response(response, subscription),
                        status_code=response.status_code,
                        headers=self._filter_response_headers(response.headers),
                        media_type=response.headers.get("content-type", "text/event-stream"),
                    )
                else:
                    # Non-streaming request
                    response = await self.client.request(
                        method=request.method,
                        url=url,
                        headers=headers,
                        content=body,
                    )
                    
                    # Check for rate limit
                    if response.status_code == 429:
                        await self.tracker.record_429(subscription)
                    elif response.status_code >= 500:
                        await self.tracker.record_error(subscription)
                    
                    return Response(
                        content=response.content,
                        status_code=response.status_code,
                        headers=self._filter_response_headers(response.headers),
                        media_type=response.headers.get("content-type", "application/json"),
                    )
                    
            except httpx.TimeoutException as e:
                logger.error(f"Timeout proxying to {subscription.name}: {e}")
                await self.tracker.record_error(subscription)
                return Response(
                    content='{"error": {"type": "timeout", "message": "Request timed out."}}',
                    status_code=504,
                    media_type="application/json",
                )
            except httpx.RequestError as e:
                logger.error(f"Error proxying to {subscription.name}: {e}")
                await self.tracker.record_error(subscription)
                return Response(
                    content='{"error": {"type": "proxy_error", "message": "Failed to connect to upstream."}}',
                    status_code=502,
                    media_type="application/json",
                )
