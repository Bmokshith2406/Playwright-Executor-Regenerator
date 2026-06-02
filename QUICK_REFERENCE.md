# Quick Reference Guide

## Start Here 👇

### For First-Time Users

1. **What is this?**  
   → Automated Playwright test repair using AI (Google Gemini LLM)

2. **How do I run it locally?**
   ```bash
   # Activate venv first
   .\venv\Scripts\activate   # Windows
   source venv/bin/activate   # Linux / macOS

   # Run with pretty colorized developer logs (default: binds to 127.0.0.1:8000)
   python run.py --mode pretty

   # Or run using Docker (app listens on port 8080 inside container)
   docker build -t repair-engine:latest .
   docker run -p 8080:8080 -e GOOGLE_API_KEY="your-key" repair-engine:latest
   ```

3. **How do I use it?**
   ```bash
   curl -X POST http://127.0.0.1:8000/repair \
     -H "X-API-Key: client_sec_key" \
     -F 'payload={
       "step_id": "test_1",
       "step_intent": "Click login",
       "original_code": "await page.click(\"#btn\")",
       "error_classification": {"type": "LOCATOR_NOT_FOUND"},
       "error_details": {"message": "Timeout"}
     }'
   ```

---

## Essential Commands

### Development

```bash
# Start locally with pretty logs (auto-reload on app/ directory)
python run.py --mode pretty

# Start without reload (cleaner for executor runs)
python run.py --no-reload

# Start with JSON logs (staging simulation)
python run.py --mode json

# Pass a custom host/port
python run.py --host 0.0.0.0 --port 8080

# Run tests
python -m pytest -q

# Run tests with coverage
pytest --cov=app --cov-report=html

# View Prometheus metrics
curl http://127.0.0.1:8000/metrics
```

### Deployment

```bash
# Build image
docker build -t repair-engine:latest .

# Run container (app binds to 0.0.0.0:8080 inside container)
docker run -p 8080:8080 \
  -e GOOGLE_API_KEY="your-key" \
  -e MONGODB_URL="mongodb+srv://..." \
  repair-engine:latest

# Deploy to Kubernetes
kubectl apply -f api-deployment.yaml

# Scale API instances
kubectl scale deployment repair-engine-api --replicas=5
```

### Database (MongoDB)

```bash
# View active indexes
mongosh "mongodb://localhost:27017/repair_engine" --eval "db.repair_records.getIndexes()"

# Indexes are auto-created on startup — no migration commands needed
```

---

## API Quick Commands

### Health Checks

```bash
# Is it alive?
curl http://127.0.0.1:8000/health/live

# Can it serve traffic?
curl http://127.0.0.1:8000/health/ready

# Full observability including LLM ping
curl http://127.0.0.1:8000/health/deep

# Show all Prometheus metrics
curl http://127.0.0.1:8000/metrics

# App metadata
curl http://127.0.0.1:8000/info
```

### Repair Endpoint

```bash
# Repair a failing step (multipart/form-data with optional screenshot)
curl -X POST http://127.0.0.1:8000/repair \
  -H "X-API-Key: client_sec_key" \
  -F 'payload={
    "step_id": "step_1",
    "step_intent": "Click Login Button",
    "original_code": "await page.click('"'"'#login-btn'"'"')",
    "error_classification": {"type": "LOCATOR_NOT_FOUND"},
    "error_details": {"message": "Timeout"}
  }'
```

### Execute Script

```bash
# Run script with self-healing executor
curl -X POST http://127.0.0.1:8000/executor/run \
  -H "X-API-Key: client_sec_key" \
  -F "script=@tests/scripts/failing_test.py"

# Get executor statistics
curl http://127.0.0.1:8000/executor/stats \
  -H "X-API-Key: client_sec_key"
```

---

## File Structure Cheat Sheet

```
app/
├── api/v1/                    → Compatibility wrappers re-exporting live routes
├── core/
│   ├── exceptions/            → Exception package (base, api, repair, executor)
│   ├── repositories/          → DB repos (base, in_memory, mongo)
│   ├── base64_utils.py        → Base64 image validators
│   ├── config.py              → Settings manager & env variable definitions
│   ├── database.py            → Motor MongoDB connection manager with timeouts
│   ├── dom_pruner.py          → Compresses HTML to an AST-style tag tree
│   ├── health.py              → System health monitors & readiness checks
│   ├── io.py                  → Atomic file writer with write-fallback logic
│   ├── llm_executor.py        → Gemini API wrapper with rate-limit retries
│   ├── llm_json.py            → Cleans & parses JSON responses from the LLM
│   ├── metrics.py             → Prometheus metric definitions
│   ├── prompts.py             → Central registry for all LLM prompt templates
│   ├── redis_state.py         → Distributed state & cache management
│   ├── resilience.py          → CircuitBreaker & exponential backoff
│   ├── security.py            → API key auth & rate limit middleware
│   ├── tracing.py             → OpenTelemetry span wrappers
│   └── utils.py               → Hashing, timers, correlation contextvars
├── executors/
│   ├── base.py                → Abstract executor base class
│   ├── models.py              → ExecutionResult models
│   ├── python.py              → Subprocess runner with secret-env stripping
│   └── sandbox.py            → AST-based security auditor (blocks forbidden ops)
├── models/
│   ├── cir.py                 → Canonical Intermediate Representation schemas
│   ├── context.py             → Runtime validation context models
│   ├── database.py            → DB persistence schemas (RepairRecord, ExecutionRecord)
│   ├── extraction.py          → Locator value models returned from extractors
│   └── step_repair.py         → Pydantic schemas for /repair endpoint
├── routes/
│   ├── executor.py            → /executor and /executor/run routes
│   ├── health.py              → /health/* routes
│   ├── metrics.py             → /metrics route
│   └── repair.py              → /repair route
├── services/
│   ├── extractors/            → Action extractor package
│   │   ├── BaseExtractor.py   → Parent class with literal-guard utility
│   │   ├── ClickExtractor.py  → Click locator extraction via LLM
│   │   ├── TypeExtractor.py   → Type/fill target & value extraction
│   │   ├── SelectExtractor.py → Dropdown & select option extraction
│   │   ├── AssertExtractor.py → Assertion & URL-contains extraction
│   │   ├── DialogExtractor.py → Runtime JS dialog interception
│   │   └── ExtractorFactory.py → Maps ActionType → extractor class
│   ├── atomic_normalizer.py   → Text normalizer and spacing standardizer
│   ├── auto_repair_trigger.py → Parses failure dirs to build RepairRequests
│   ├── cir_builder.py         → Builds CIR blocks from StepRepairRequests
│   ├── diff.py                → Unified diff utility for patched code
│   ├── execution_orchestrator.py → Manages healing loops & run directories
│   ├── generator.py           → Generates Playwright code from locators
│   ├── llm_classifier.py      → Classifies action types via LLM
│   ├── llm_fallback_repair.py → Secondary repair loop with full code context
│   ├── repair_explanation_service.py → Summarizes script modifications
│   ├── repair_pipeline.py     → Orchestrates CIR build → gen → sandbox verify
│   ├── repair_service.py      → FastAPI-level repair actions handler
│   ├── rollback.py            → Backs up and restores scripts on failure
│   ├── script_patcher.py      → Patches step body & guarded-step string args
│   ├── step_modifier.py       → Generates modified code variations
│   ├── step_verifier.py       → Validates proposals in sandboxed subprocesses
│   └── validator.py           → Pre-flight code and intent validators
├── tasks/
│   ├── celery_app.py          → Celery application configuration
│   └── repair_tasks.py        → Async Celery worker task definitions
├── main.py                    → FastAPI app entry point, logging setup, lifespan
└── middleware.py              → Audit log & request timing middleware
run.py                         → Developer startup wrapper for uvicorn
```

---

## Configuration Quick Setup

```bash
# Copy and edit the env template
cp .env.example .env   # or create .env manually

# Minimum required variables
GOOGLE_API_KEY=your-gemini-key
ALLOWED_API_KEYS=["client_sec_key"]
ENABLE_API_AUTH=true

# Add MongoDB for durable persistence
MONGODB_URL=mongodb://localhost:27017
MONGODB_DB_NAME=repair_engine

# Add Redis for distributed state and Celery
REDIS_URL=redis://localhost:6379/0

# Run locally
python run.py --mode pretty
```

> **Note:** MongoDB indexes are created automatically on startup. No migration commands (Alembic/SQL) are required.

---

## Monitoring Quick Access

```bash
# Prometheus scrape endpoint
http://127.0.0.1:8000/metrics

# Prometheus UI (if self-hosted)
http://localhost:9090

# Grafana
http://localhost:3000   (default: admin/admin)

# Jaeger Tracing (if ENABLE_TRACING=true)
http://localhost:16686

# Interactive API Docs (Swagger UI)
http://127.0.0.1:8000/docs

# Health Readiness Check
http://127.0.0.1:8000/health/ready
```

---

## Common Issues & Fixes

| Issue | Quick Fix |
|-------|-----------|
| Port already in use | Check for zombie python.exe processes: `tasklist /FI "IMAGENAME eq python.exe"` and `taskkill /PID <pid> /F` |
| `uvicorn` not found | Activate venv: `.\venv\Scripts\activate` |
| MongoDB connection error | Check `MONGODB_URL` in `.env` |
| Rate limit exceeded | Increase `RATE_LIMIT_REQUESTS_PER_MINUTE` in config |
| LLM timeout | Increase `LLM_TIMEOUT_SECONDS` in `.env` |
| Tests failing | Run `pytest tests/ -v` to see details |
| `503 Executor sandbox disabled` | Set `ENABLE_SANDBOX_EXECUTION=true` or `SANDBOX_USE_DOCKER=true` |
| `ImportError: cannot import global_exception_handler` | Delete `app/core/exceptions.py` if it exists alongside the `exceptions/` folder |

---

## Environment Variables (Essentials Only)

```bash
# ── Required ──────────────────────────────────────────────────
GOOGLE_API_KEY=...                    # Primary Gemini API key
API_SECRET_KEY=...                    # Random secret for auth
ALLOWED_API_KEYS=["client_sec_key"]   # Accepted API keys list

# ── Database ──────────────────────────────────────────────────
MONGODB_URL=mongodb://...             # MongoDB connection string
MONGODB_DB_NAME=repair_engine         # Target database name

# ── Redis & Celery ────────────────────────────────────────────
REDIS_URL=redis://localhost:6379/0    # Redis connection
CELERY_BROKER_URL=redis://localhost:6379/1
CELERY_RESULT_BACKEND=redis://localhost:6379/2

# ── Logging ───────────────────────────────────────────────────
LOG_LEVEL=INFO                        # DEBUG | INFO | WARNING | ERROR
LOG_FORMAT_MODE=PRETTY                # PRETTY | CONSOLE | JSON
ENV=development                       # development | staging | production

# ── Feature Flags ─────────────────────────────────────────────
ENABLE_API_AUTH=true                  # API key authentication
ENABLE_SELF_HEALING=true              # Auto-repair on executor failures
ENABLE_SANDBOX_EXECUTION=true         # Strict code sandboxing
ENABLE_METRICS=true                   # Prometheus /metrics endpoint
ENABLE_TRACING=false                  # OpenTelemetry tracing

# ── Optional Tuning ───────────────────────────────────────────
MAX_LLM_MODIFICATIONS=1               # Max LLM repair iterations per step
LLM_TIMEOUT_SECONDS=150               # Gemini API call timeout
RATE_LIMIT_REQUESTS_PER_MINUTE=60     # API throttle (requests/min)
SANDBOX_USE_DOCKER=false              # Use Docker for script isolation
```

---

## API Error Codes

| Code | Status | Meaning |
|------|--------|---------|
| `INVALID_API_KEY` | 401 | Wrong or missing API key |
| `RATE_LIMIT_EXCEEDED` | 429 | Too many requests |
| `INVALID_REQUEST` | 400 | Bad request format |
| `LLM_TIMEOUT` | 504 | Gemini API timed out |
| `DATABASE_ERROR` | 500 | DB connection failed |
| `CIRCUIT_BREAKER_OPEN` | 503 | Service temporarily down |
| `EXECUTOR_SANDBOX_DISABLED` | 503 | Sandbox disabled or unavailable |

---

## Response Status Codes

| Status | Meaning |
|--------|---------|
| 200 | Success (check `status` field — repair may still have failed gracefully) |
| 400 | Bad request format |
| 401 | Unauthorized (invalid API key) |
| 429 | Rate limited |
| 500 | Server error |
| 503 | Service unavailable / sandbox disabled |

---

## Database Quick Queries (MongoDB)

```javascript
// How many repairs succeeded today?
db.repair_records.countDocuments({
  outcome: "success",
  created_at: { $gt: new Date(Date.now() - 24*60*60*1000) }
})

// What errors are most common?
db.repair_records.aggregate([
  { $group: { _id: "$error_type", count: { $sum: 1 } } },
  { $sort: { count: -1 } }
])

// Average repair duration?
db.repair_records.aggregate([
  { $match: { outcome: "success" } },
  { $group: { _id: null, avg_duration: { $avg: "$duration_ms" } } }
])

// Top failing steps?
db.repair_records.aggregate([
  { $match: { outcome: "failure" } },
  { $group: { _id: "$step_id", count: { $sum: 1 } } },
  { $sort: { count: -1 } },
  { $limit: 10 }
])
```

---

## Celery Quick Commands

```bash
# Check worker status
celery -A app.tasks.celery_app inspect active

# Check pending tasks
celery -A app.tasks.celery_app inspect reserved

# Purge all tasks
celery -A app.tasks.celery_app purge

# Monitor in real-time
celery -A app.tasks.celery_app events

# View scheduled tasks
celery -A app.tasks.celery_app inspect scheduled
```

> Celery workers are **optional**. The engine runs fully synchronously without them.

---

## Redis Quick Commands

```bash
# Connect to Redis
redis-cli

# Check memory usage
redis-cli INFO memory

# View all repair-engine keys
redis-cli KEYS "repair:*"

# Monitor commands in real-time
redis-cli MONITOR

# Get specific key
redis-cli GET key-name

# Delete key
redis-cli DEL key-name

# Clear all data (CAUTION: destructive)
redis-cli FLUSHALL
```

---

## Documentation Quick Links

| Document | Purpose | Read Time |
|----------|---------|-----------| 
| README.md | Complete overview + API reference | 30–45 min |
| QUICK_REFERENCE.md | This file — commands & cheatsheet | 5 min |

---

## Troubleshooting Flowchart

```
Service down?
├─ YES → Check: python processes still running? Kill zombies with tasklist/taskkill
├─ NO → API responding?
│   ├─ NO → curl http://127.0.0.1:8000/health/live
│   ├─ YES → Health status?
│   │   ├─ Not ready → curl http://127.0.0.1:8000/health/ready (check MONGODB_URL)
│   │   ├─ Ready → Can process?
│   │   │   ├─ NO → Check logs for errors
│   │   │   ├─ YES → Results?
│   │   │   │   ├─ Wrong → Check request format / X-API-Key header
│   │   │   │   ├─ Error → Check error code mapping above
│   │   │   │   └─ OK → Success! ✓
```

---

## Performance Benchmarks

| Metric | Target | Observed |
|--------|--------|---------|
| API Response Time | < 5s | ~3.4s |
| LLM Processing | < 60s | ~34s avg |
| Database Query | < 100ms | ~5ms avg |
| Health Check | < 1s | ~0.2s |
| P95 Latency | < 10s | ~8.9s |
| Error Rate | < 1% | ~0.5% |

---

## Deployment Checklist

- [ ] MongoDB running and reachable (`MONGODB_URL` configured)
- [ ] Redis running (optional, needed for Celery & distributed state)
- [ ] `GOOGLE_API_KEY` set in `.env`
- [ ] `ALLOWED_API_KEYS` configured
- [ ] `ENABLE_API_AUTH=true` confirmed
- [ ] `ENABLE_SANDBOX_EXECUTION=true` confirmed
- [ ] Docker sandboxing enabled for production (`SANDBOX_USE_DOCKER=true`)
- [ ] MongoDB indexes auto-created on startup (check `/health/ready`)
- [ ] Tests passing (`pytest tests/ -v`)
- [ ] Health check passing (`curl /health/ready`)
- [ ] Metrics accessible (`curl /metrics`)
- [ ] Logging configured (`LOG_FORMAT_MODE`, `LOG_LEVEL`)
- [ ] Backups / data retention policy set (TTL: 30 days via MongoDB TTL index)
- [ ] Monitoring & alerting on `/health/ready`

---

## One-Liner Deployments

```bash
# Local development (default: 127.0.0.1:8000)
python run.py

# Local development exposed to network
python run.py --host 0.0.0.0

# Docker run (container listens on 8080)
docker run -p 8080:8080 \
  -e GOOGLE_API_KEY="your-key" \
  -e MONGODB_URL="mongodb+srv://..." \
  repair-engine:latest

# Kubernetes
kubectl apply -f api-deployment.yaml

# Scale Kubernetes
kubectl scale deployment repair-engine-api --replicas=5 -n repair-engine

# Update image
kubectl set image deployment/repair-engine-api repair-engine-api=myregistry/repair-engine:latest

# Port forward to local
kubectl port-forward svc/repair-engine-api 8000:80 -n repair-engine
```

---

## Support Resources

1. **Issue** → Check README.md Troubleshooting section
2. **Configuration** → Check `.env` file & README.md Configuration table
3. **API** → `http://127.0.0.1:8000/docs` (Swagger UI)
4. **Code** → Check relevant `/app/` file per the File Structure above
5. **Tests** → Check `/tests/` directory

---

## Version & Status

- **Version**: 3.0.0
- **Status**: ✅ Production Ready
- **Author**: Mokshith Balidi
- **Organization**: TW.2324
- **Created**: January 2026
- **Last Updated**: June 1, 2026
- **Python**: 3.11+
- **Rights**: All rights reserved by Mokshith Balidi

---

## Pro Tips 💡

1. **Use pretty mode**: `python run.py --mode pretty` for color-coded, emoji-enriched logs
2. **No venv activation?** → The startup guard in `run.py` will remind you with exact steps
3. **Debug logging**: Set `LOG_LEVEL=DEBUG` to see every MongoDB command and LLM call
4. **Monitor metrics**: Check `/metrics` regularly for repair success rates and LLM latencies
5. **Test before deploy**: Always run `pytest tests/ -v`
6. **No PostgreSQL**: This app uses **MongoDB only** — no SQL migrations needed
7. **MongoDB auto-indexes**: Indexes (TTL, search, unique) are created on every startup
8. **Celery is optional**: The engine runs synchronously without Redis/Celery configured
9. **Keep secrets safe**: Never commit `.env` files; use a secrets manager in production
10. **Health deep check**: Use `/health/deep` to verify LLM connectivity end-to-end

---

## Success Indicators ✓

- [ ] API responds to requests
- [ ] `/health/ready` returns `{"status": "ready"}`
- [ ] Metrics collecting at `/metrics`
- [ ] Repairs returning `status=success` in response
- [ ] MongoDB persisting `repair_records`
- [ ] Logs readable in chosen format (`PRETTY` / `CONSOLE` / `JSON`)
- [ ] No errors in monitoring tools

---

**Everything working? Great! You're ready to use the Playwright Step Repair Engine! 🚀**

For full details, refer to `README.md`.
