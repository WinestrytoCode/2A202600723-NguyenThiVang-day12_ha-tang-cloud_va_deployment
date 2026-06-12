"""
Production AI Agent — Kết hợp tất cả Day 12 concepts
Checklist:
  ✅ Config từ environment (12-factor)
  ✅ Structured JSON logging
  ✅ API Key authentication
  ✅ Rate limiting (Stateless Redis with fallback)
  ✅ Cost guard (Stateless Redis with fallback)
  ✅ Input validation (Pydantic)
  ✅ Health check + Readiness probe (checks Redis)
  ✅ Graceful shutdown (SIGTERM handler)
  ✅ Security headers
  ✅ CORS
  ✅ Error handling
"""
import os
import time
import signal
import logging
import json
from datetime import datetime, timezone
from collections import defaultdict, deque
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Security, Depends, Request, Response
from fastapi.security.api_key import APIKeyHeader
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
import uvicorn

from app.config import settings

# Mock LLM (thay bằng OpenAI/Anthropic khi có API key)
from utils.mock_llm import ask as llm_ask

# ─────────────────────────────────────────────────────────
# Logging — JSON structured
# ─────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.DEBUG if settings.debug else logging.INFO,
    format='{"ts":"%(asctime)s","lvl":"%(levelname)s","msg":"%(message)s"}',
)
logger = logging.getLogger(__name__)

START_TIME = time.time()
_is_ready = False
_request_count = 0
_error_count = 0

# ─────────────────────────────────────────────────────────
# Redis Connection / Stateless Setup
# ─────────────────────────────────────────────────────────
USE_REDIS = False
_redis = None
_memory_store = {}

if settings.redis_url:
    try:
        import redis
        _redis = redis.from_url(settings.redis_url, decode_responses=True)
        _redis.ping()
        USE_REDIS = True
        logger.info(json.dumps({"event": "redis_connected", "url": settings.redis_url}))
    except Exception as e:
        logger.warning(json.dumps({"event": "redis_connection_failed", "error": str(e)}))

# ─────────────────────────────────────────────────────────
# Rate Limiter — Stateless Redis with sliding window fallback
# ─────────────────────────────────────────────────────────
_rate_windows: dict[str, deque] = defaultdict(deque)

def check_rate_limit(key: str):
    now = time.time()
    if USE_REDIS:
        redis_key = f"rate_limit:{key}"
        try:
            pipe = _redis.pipeline()
            pipe.zremrangebyscore(redis_key, 0, now - 60)
            pipe.zcard(redis_key)
            pipe.zadd(redis_key, {str(now): now})
            pipe.expire(redis_key, 65)
            _, current_count, _, _ = pipe.execute()
            
            if current_count >= settings.rate_limit_per_minute:
                _redis.zrem(redis_key, str(now))
                raise HTTPException(
                    status_code=429,
                    detail=f"Rate limit exceeded: {settings.rate_limit_per_minute} req/min",
                    headers={"Retry-After": "60"},
                )
        except HTTPException:
            raise
        except Exception as e:
            logger.error(json.dumps({"event": "redis_rate_limit_error", "error": str(e)}))
            # Fallback to in-memory if Redis error occurs
            _check_rate_limit_memory(key, now)
    else:
        _check_rate_limit_memory(key, now)

def _check_rate_limit_memory(key: str, now: float):
    window = _rate_windows[key]
    while window and window[0] < now - 60:
        window.popleft()
    if len(window) >= settings.rate_limit_per_minute:
        raise HTTPException(
            status_code=429,
            detail=f"Rate limit exceeded: {settings.rate_limit_per_minute} req/min",
            headers={"Retry-After": "60"},
        )
    window.append(now)

# ─────────────────────────────────────────────────────────
# Cost Guard — Stateless Redis with daily budget reset fallback
# ─────────────────────────────────────────────────────────
_daily_cost = 0.0
_cost_reset_day = time.strftime("%Y-%m-%d")

def check_and_record_cost(input_tokens: int, output_tokens: int):
    global _daily_cost, _cost_reset_day
    today = time.strftime("%Y-%m-%d")
    cost = (input_tokens / 1000) * 0.00015 + (output_tokens / 1000) * 0.0006

    if USE_REDIS:
        redis_key = f"budget:daily:{today}"
        try:
            current_cost = float(_redis.get(redis_key) or 0.0)
            if current_cost >= settings.daily_budget_usd:
                raise HTTPException(
                    status_code=402, 
                    detail="Daily budget exhausted. Try tomorrow."
                )
            
            pipe = _redis.pipeline()
            pipe.incrbyfloat(redis_key, cost)
            pipe.expire(redis_key, 2 * 24 * 3600)  # 2 days TTL
            pipe.execute()
        except HTTPException:
            raise
        except Exception as e:
            logger.error(json.dumps({"event": "redis_budget_error", "error": str(e)}))
            _check_and_record_cost_memory(cost, today)
    else:
        _check_and_record_cost_memory(cost, today)

def _check_and_record_cost_memory(cost: float, today: str):
    global _daily_cost, _cost_reset_day
    if today != _cost_reset_day:
        _daily_cost = 0.0
        _cost_reset_day = today
    if _daily_cost >= settings.daily_budget_usd:
        raise HTTPException(
            status_code=402, 
            detail="Daily budget exhausted. Try tomorrow."
        )
    _daily_cost += cost

# ─────────────────────────────────────────────────────────
# Auth
# ─────────────────────────────────────────────────────────
api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)

def verify_api_key(api_key: str = Security(api_key_header)) -> str:
    if not api_key or api_key != settings.agent_api_key:
        raise HTTPException(
            status_code=401,
            detail="Invalid or missing API key. Include header: X-API-Key: <key>",
        )
    return api_key

# ─────────────────────────────────────────────────────────
# Lifespan
# ─────────────────────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    global _is_ready
    logger.info(json.dumps({
        "event": "startup",
        "app": settings.app_name,
        "version": settings.app_version,
        "environment": settings.environment,
    }))
    time.sleep(0.1)  # simulate init
    _is_ready = True
    logger.info(json.dumps({"event": "ready"}))

    yield

    _is_ready = False
    logger.info(json.dumps({"event": "shutdown"}))

# ─────────────────────────────────────────────────────────
# App
# ─────────────────────────────────────────────────────────
app = FastAPI(
    title=settings.app_name,
    version=settings.app_version,
    lifespan=lifespan,
    docs_url="/docs" if settings.environment != "production" else None,
    redoc_url=None,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.allowed_origins,
    allow_methods=["GET", "POST"],
    allow_headers=["Authorization", "Content-Type", "X-API-Key"],
)

@app.middleware("http")
async def request_middleware(request: Request, call_next):
    global _request_count, _error_count
    start = time.time()
    _request_count += 1
    try:
        response: Response = await call_next(request)
        # Security headers
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers.pop("server", None)
        duration = round((time.time() - start) * 1000, 1)
        logger.info(json.dumps({
            "event": "request",
            "method": request.method,
            "path": request.url.path,
            "status": response.status_code,
            "ms": duration,
        }))
        return response
    except Exception as e:
        _error_count += 1
        raise

# ─────────────────────────────────────────────────────────
# Models
# ─────────────────────────────────────────────────────────
class AskRequest(BaseModel):
    question: str = Field(
        ..., 
        min_length=1, 
        max_length=2000,
        description="Your question for the agent"
    )
    session_id: str | None = Field(
        None, 
        description="Optional session ID for conversation history"
    )

class AskResponse(BaseModel):
    question: str
    answer: str
    model: str
    timestamp: str
    session_id: str | None = None

# ─────────────────────────────────────────────────────────
# Endpoints
# ─────────────────────────────────────────────────────────

@app.get("/", tags=["Info"])
def root():
    return {
        "app": settings.app_name,
        "version": settings.app_version,
        "environment": settings.environment,
        "endpoints": {
            "ask": "POST /ask (requires X-API-Key)",
            "health": "GET /health",
            "ready": "GET /ready",
        },
    }


@app.post("/ask", response_model=AskResponse, tags=["Agent"])
async def ask_agent(
    body: AskRequest,
    request: Request,
    _key: str = Depends(verify_api_key),
):
    """
    Send a question to the AI agent.
    **Authentication:** Include header `X-API-Key: <your-key>`
    """
    # Rate limit per API key
    check_rate_limit(_key[:8])  # use first 8 chars as key bucket

    # Load conversation history from Redis context if session_id is provided
    session_id = body.session_id
    history_context = ""
    if session_id and USE_REDIS:
        try:
            history_key = f"history:{session_id}"
            history_list = _redis.lrange(history_key, 0, -1)
            parsed_history = []
            for msg_str in history_list:
                try:
                    parsed_history.append(json.loads(msg_str))
                except Exception:
                    pass
            if parsed_history:
                history_context = "\n".join([f"{m['role']}: {m['content']}" for m in parsed_history])
        except Exception as e:
            logger.error(json.dumps({"event": "redis_load_history_failed", "error": str(e)}))

    # Calculate input tokens (rough estimation)
    input_tokens = len(body.question.split()) * 2
    if history_context:
        input_tokens += len(history_context.split()) * 2

    # Check budget
    check_and_record_cost(input_tokens, 0)

    logger.info(json.dumps({
        "event": "agent_call",
        "q_len": len(body.question),
        "has_history": bool(history_context),
        "client": str(request.client.host) if request.client else "unknown",
    }))

    # Get final prompt (combining context if any)
    prompt = body.question
    if history_context:
        prompt = f"Previous conversation:\n{history_context}\n\nUser: {body.question}"

    # Call mock/real LLM
    answer = llm_ask(prompt)

    output_tokens = len(answer.split()) * 2
    check_and_record_cost(0, output_tokens)

    # Save conversation turn to Redis if session_id is provided
    if session_id and USE_REDIS:
        try:
            history_key = f"history:{session_id}"
            _redis.rpush(history_key, json.dumps({
                "role": "user", 
                "content": body.question, 
                "ts": datetime.now(timezone.utc).isoformat()
            }))
            _redis.rpush(history_key, json.dumps({
                "role": "assistant", 
                "content": answer, 
                "ts": datetime.now(timezone.utc).isoformat()
            }))
            _redis.ltrim(history_key, -20, -1)  # Keep last 10 turns (20 msgs)
            _redis.expire(history_key, 3600)    # TTL 1 hour
        except Exception as e:
            logger.error(json.dumps({"event": "redis_save_history_failed", "error": str(e)}))

    return AskResponse(
        question=body.question,
        answer=answer,
        model=settings.llm_model,
        timestamp=datetime.now(timezone.utc).isoformat(),
        session_id=session_id
    )


@app.get("/health", tags=["Operations"])
def health():
    """Liveness probe. Platform restarts container if this fails."""
    status = "ok"
    redis_ok = True
    if USE_REDIS:
        try:
            _redis.ping()
        except Exception:
            redis_ok = False
            status = "degraded"
            
    checks = {
        "llm": "mock" if not settings.openai_api_key else "openai",
        "redis": "connected" if redis_ok else ("disconnected" if settings.redis_url else "disabled")
    }
    return {
        "status": status,
        "version": settings.app_version,
        "environment": settings.environment,
        "uptime_seconds": round(time.time() - START_TIME, 1),
        "total_requests": _request_count,
        "checks": checks,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


@app.get("/ready", tags=["Operations"])
def ready():
    """Readiness probe. Load balancer stops routing here if not ready."""
    if not _is_ready:
        raise HTTPException(503, "Not ready")
    if USE_REDIS:
        try:
            _redis.ping()
        except Exception:
            raise HTTPException(503, "Redis connection lost")
    return {"ready": True}


@app.get("/metrics", tags=["Operations"])
def metrics(_key: str = Depends(verify_api_key)):
    """Basic metrics (protected)."""
    current_cost = _daily_cost
    if USE_REDIS:
        try:
            today = time.strftime("%Y-%m-%d")
            redis_key = f"budget:daily:{today}"
            current_cost = float(_redis.get(redis_key) or 0.0)
        except Exception as e:
            logger.error(json.dumps({"event": "redis_get_cost_failed", "error": str(e)}))

    return {
        "uptime_seconds": round(time.time() - START_TIME, 1),
        "total_requests": _request_count,
        "error_count": _error_count,
        "daily_cost_usd": round(current_cost, 4),
        "daily_budget_usd": settings.daily_budget_usd,
        "budget_used_pct": round(current_cost / settings.daily_budget_usd * 100, 1),
        "storage": "redis" if USE_REDIS else "in-memory",
    }


# ─────────────────────────────────────────────────────────
# Graceful Shutdown
# ─────────────────────────────────────────────────────────
def _handle_signal(signum, _frame):
    global _is_ready
    _is_ready = False
    logger.info(json.dumps({"event": "signal", "signum": signum, "msg": "Initiating graceful shutdown"}))

signal.signal(signal.SIGTERM, _handle_signal)


if __name__ == "__main__":
    logger.info(f"Starting {settings.app_name} on {settings.host}:{settings.port}")
    logger.info(f"API Key: {settings.agent_api_key[:4]}****")
    uvicorn.run(
        "app.main:app",
        host=settings.host,
        port=settings.port,
        reload=settings.debug,
        timeout_graceful_shutdown=30,
    )
