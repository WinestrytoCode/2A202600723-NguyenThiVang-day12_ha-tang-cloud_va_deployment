# Deployment Information

## Public URL
https://production-ready-agent.up.railway.app

## Platform
Railway

## Test Commands

### Health Check
```bash
curl https://production-ready-agent.up.railway.app/health
# Expected: {"status": "ok", "version": "1.0.0", "environment": "production", "uptime_seconds": ..., "total_requests": 0, ...}
```

### Readiness Check
```bash
curl https://production-ready-agent.up.railway.app/ready
# Expected: {"ready": true} (returns 503 if Redis is down or server is shutting down)
```

### API Test (with authentication)
```bash
curl -X POST https://production-ready-agent.up.railway.app/ask \
  -H "X-API-Key: dev-key-change-me" \
  -H "Content-Type: application/json" \
  -d '{"user_id": "test", "question": "Explain Docker"}'
# Expected: {"question": "Explain Docker", "answer": "...", "model": "gpt-4o-mini", "timestamp": "...", "session_id": null}
```

### API Test (with Multi-turn Session History)
```bash
curl -X POST https://production-ready-agent.up.railway.app/ask \
  -H "X-API-Key: dev-key-change-me" \
  -H "Content-Type: application/json" \
  -d '{"user_id": "test", "question": "What is docker?", "session_id": "session-123"}'
```

### Rate Limiting Test (Should block after limit is reached)
```bash
for i in {1..25}; do 
  curl -i -H "X-API-Key: dev-key-change-me" https://production-ready-agent.up.railway.app/ask \
    -X POST -H "Content-Type: application/json" -d '{"question":"test"}'
done
# Expected: Returns 200 OK initially, then 429 Too Many Requests once limit of 20 req/min is hit.
```

## Environment Variables Set
- `PORT` (assigned dynamically by Railway)
- `REDIS_URL` (injected automatically via Redis service link)
- `AGENT_API_KEY` (set via dashboard to secure API endpoints)
- `LOG_LEVEL` (set to `INFO` or `DEBUG`)

## Screenshots
- [Deployment dashboard](screenshots/dashboard.png)
- [Service running](screenshots/running.png)
- [Test results](screenshots/test.png)
