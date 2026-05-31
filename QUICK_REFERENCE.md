# Quick Reference Guide

## Start Here 👇

### For First-Time Users

1. **What is this?** 
   → Automated test repair using AI (Gemini LLM)

2. **How do I run it locally?**
   ```bash
   # Run with pretty colorized developer logs
   python run.py --mode pretty
   
   # Or run using Docker
   docker build -t repair-engine:latest .
   docker run -p 8000:8080 -e GOOGLE_API_KEY="your-key" repair-engine:latest
   ```

3. **How do I use it?**
   ```bash
   curl -X POST http://localhost:8000/repair \
     -H "X-API-Key: key" \
     -H "Content-Type: application/json" \
     -d '{
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
# Start locally with pretty logs (Default reload enabled on app/ directory)
python run.py --mode pretty

# Start locally in JSON logs mode (for staging simulations)
python run.py --mode json

# View metrics
curl http://localhost:8000/metrics

# Run tests
python -m pytest

# Run tests with coverage
pytest --cov=app --cov-report=html
```

### Deployment

```bash
# Build image
docker build -t repair-engine:latest .

# Deploy to Kubernetes
kubectl apply -f api-deployment.yaml

# Scale api instances
kubectl scale deployment repair-engine-api --replicas=5
```

### Database (MongoDB)

```bash
# View active database indexes
mongosh "mongodb://localhost:27017/repair_db" --eval "db.repair_records.getIndexes()"
```

---

## API Quick Commands

### Health Checks

```bash
# Is it running?
curl http://localhost:8000/health/live

# Can it serve traffic?
curl http://localhost:8000/health/ready

# Show all metrics
curl http://localhost:8000/metrics
```

### Repair Endpoint

```bash
# Simple repair request (multipart/form-data)
curl -X POST http://localhost:8000/repair \
  -H "X-API-Key: client_sec_key" \
  -F "payload={
    \"step_id\": \"step_1\",
    \"step_intent\": \"Click Login Button\",
    \"original_code\": \"await page.click('#login-btn')\",
    \"error_classification\": {\"type\": \"LOCATOR_NOT_FOUND\"},
    \"error_details\": {\"message\": \"Timeout\"}
  }"
```

### Execute Script

```bash
# Run script with self-healing (multipart/form-data)
curl -X POST http://localhost:8000/executor/run \
  -H "X-API-Key: client_sec_key" \
  -F "script=@tests/scripts/failing_test.py"
```

---

## File Structure Cheat Sheet

```
/app/
  main.py                 → App entry point & log layout setup
  core/
    config.py            → Settings manager & list validations
    database.py          → Motor MongoDB connection pool
    exceptions/          → Exceptions package (base, api, repair, executor)
    repositories/        → Database repositories (base, in_memory, mongo)
    dom_pruner.py        → HTML DOM to AST tag-tree compressor
    llm_executor.py      → Gemini API wrapper with rate limit fallback
  executors/             → Sandbox execution engine (sandbox, python, base)
  models/                → Pydantic models (cir, database schemas, step_repair)
  routes/                → FastAPI endpoints (repair, executor, health, metrics)
  services/              → Code generation & healing orchestrator
    extractors/          → Registry & Action Extractors (Base, Click, Type, etc.)
  tasks/                 → Celery workers tasks
```

---

## Configuration Quick Setup

```bash
# Copy template
cp .env.example .env

# Edit essential variables
export GOOGLE_API_KEY="your-gemini-key"
export MONGODB_URL="mongodb://localhost:27017"
export REDIS_URL="redis://localhost:6379/0"

# Run locally
python run.py --mode pretty
```

---

## Monitoring Quick Access

```bash
# Prometheus
http://localhost:9090

# Grafana
http://localhost:3000 (admin/admin)

# Jaeger Tracing
http://localhost:16686

# API Docs
http://localhost:8000/docs

# Health Check
http://localhost:8000/health/ready
```

---

## Common Issues & Fixes

| Issue | Quick Fix |
|-------|-----------|
| Port already in use | `docker-compose down` then `docker-compose up` |
| Database connection error | Check `DATABASE_URL` in `.env` |
| Rate limit exceeded | Increase `RATE_LIMIT_PER_MINUTE` in config |
| LLM timeout | Increase `LLM_TIMEOUT_SECONDS` |
| Out of memory | Scale horizontally: `docker-compose up -d --scale api=3` |
| Tests failing | Run `pytest tests/ -v` to see details |

---

## Environment Variables (Essentials Only)

```bash
# Must have
GOOGLE_API_KEY=...              # Gemini API key
API_SECRET_KEY=...              # Random secret key
DATABASE_URL=postgresql://...   # PostgreSQL connection

# Should have
REDIS_URL=redis://...           # Redis connection
ENV=production                  # Environment
LOG_LEVEL=INFO                  # Logging level

# Optional with defaults
ENABLE_MULTIMODAL=true          # LLM with images
ENABLE_AUTO_REPAIR=true         # Auto repair on fail
MAX_REPAIR_ATTEMPTS=3           # Max retries
RATE_LIMIT_PER_MINUTE=100       # API throttle
```

---

## API Error Codes

| Code | Status | Meaning |
|------|--------|---------|
| `INVALID_API_KEY` | 401 | Wrong/missing API key |
| `RATE_LIMIT_EXCEEDED` | 429 | Too many requests |
| `INVALID_REQUEST` | 400 | Bad request format |
| `LLM_TIMEOUT` | 504 | Gemini API timeout |
| `DATABASE_ERROR` | 500 | DB connection failed |
| `CIRCUIT_BREAKER_OPEN` | 503 | Service temporarily down |

---

## Response Status Codes

| Status | Meaning |
|--------|---------|
| 200 | Success (even if repair failed - check `status` field) |
| 400 | Bad request format |
| 401 | Unauthorized (invalid API key) |
| 429 | Rate limited |
| 500 | Server error |
| 503 | Service unavailable |

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

---

## Redis Quick Commands

```bash
# Connect to Redis
redis-cli

# Check memory usage
redis-cli INFO memory

# View all keys
redis-cli KEYS "*"

# Clear all data
redis-cli FLUSHALL

# Monitor commands in real-time
redis-cli MONITOR

# Get specific key
redis-cli GET key-name

# Delete key
redis-cli DEL key-name
```

---

## Documentation Quick Links

| Document | Purpose | Read Time |
|----------|---------|-----------|
| README.md | Complete overview | 30-45 min |
| ARCHITECTURE.md | System design | 25-35 min |
| API.md | API reference | 20-25 min |
| DEPLOYMENT.md | Operations guide | 30-40 min |
| DOCUMENTATION_SUMMARY.md | Navigation guide | 10-15 min |
| QUICK_REFERENCE.md | This file | 5 min |

---

## Troubleshooting Flowchart

```
Service down?
├─ YES → Check: docker-compose logs -f
├─ NO → API responding?
│   ├─ NO → Check: curl http://localhost:8000/health/live
│   ├─ YES → Check health status?
│   │   ├─ Not ready → Check: curl http://localhost:8000/health/ready
│   │   ├─ Ready → Can process?
│   │   │   ├─ NO → Check: API logs for errors
│   │   │   ├─ YES → Check results?
│   │   │   │   ├─ Wrong → Check: Request format
│   │   │   │   ├─ Error → Check: Error code mapping
│   │   │   │   └─ OK → Success! ✓
```

---

## Performance Benchmarks

| Metric | Target | Current |
|--------|--------|---------|
| API Response Time | < 5s | ~3.4s |
| LLM Processing | < 60s | ~34s avg |
| Database Query | < 100ms | ~5ms avg |
| Health Check | < 1s | ~0.2s |
| P95 Latency | < 10s | ~8.9s |
| Error Rate | < 1% | ~0.5% |

---

## Deployment Checklist

- [ ] PostgreSQL running
- [ ] Redis running
- [ ] Google API key set
- [ ] Database migrations run (`alembic upgrade head`)
- [ ] Environment variables configured
- [ ] Tests passing (`pytest tests/ -v`)
- [ ] Health check passing (`curl /health/ready`)
- [ ] Metrics accessible (`curl /metrics`)
- [ ] Logging configured
- [ ] Backups scheduled
- [ ] Monitoring setup
- [ ] Load testing done

---

## One-Liner Deployments

```bash
# Local development
docker-compose up -d && sleep 5 && curl http://localhost:8000/health

# Kubernetes
kubectl apply -f postgres.yaml && kubectl apply -f redis.yaml && kubectl apply -f api-deployment.yaml

# Scale Kubernetes
kubectl scale deployment repair-engine-api --replicas=5 -n repair-engine

# Update image in Kubernetes
kubectl set image deployment/repair-engine-api repair-engine-api=myregistry/repair-engine:latest

# View all resources
kubectl get all -n repair-engine

# Port forward to local
kubectl port-forward svc/repair-engine-api 8000:80 -n repair-engine
```

---

## Default Passwords & Keys

```bash
# Development (change in production!)
Database User: postgres
Database Password: password
Grafana Admin: admin
Grafana Password: admin
Redis: No authentication (local)

# Production (use environment variables)
All credentials: Read from vault or secrets manager
No hardcoded values
Rotate regularly
```

---

## Support Resources

1. **Issue** → Check README.md Troubleshooting
2. **Configuration** → Check .env.example
3. **API** → Check API.md
4. **Deployment** → Check DEPLOYMENT.md
5. **Architecture** → Check ARCHITECTURE.md
6. **Tests** → Check /tests/ directory
7. **Code** → Check relevant /app/ file

---

## Version & Status

- **Version**: 3.0.0
- **Status**: ✅ Production Ready
- **Author**: Mokshith Balidi
- **Organization**: TW.2324
- **Created**: January 2026
- **Last Updated**: May 31, 2026
- **Python**: 3.11+
- **Rights**: All rights reserved by Mokshith Balidi

---

## Quick Contact Info

- **Documentation**: See all .md files
- **Issues**: Check troubleshooting section
- **Questions**: Refer to relevant documentation
- **Contributions**: Follow project guidelines

---

## Pro Tips 💡

1. **Use logging**: `LOG_LEVEL=DEBUG` for troubleshooting
2. **Monitor metrics**: Check Prometheus dashboard regularly
3. **Test before deploy**: Always run `pytest tests/ -v`
4. **Scale workers**: If queue grows, scale Celery workers
5. **Monitor LLM costs**: Check `llm_tokens_used_total` metric
6. **Backup regularly**: Run backups at least daily
7. **Review logs**: Check logs for patterns in failures
8. **Keep secrets safe**: Never commit .env files
9. **Use rate limiting**: Protect against abuse
10. **Monitor health**: Set up alerts on health checks

---

## Success Indicators ✓

- [ ] API responds to requests
- [ ] Health checks pass
- [ ] Metrics are being collected
- [ ] Repairs are succeeding (status=success in response)
- [ ] Database is persisting data
- [ ] Workers are processing tasks
- [ ] Logs are being captured
- [ ] No errors in monitoring tools

---

**Everything working? Great! You're ready to use the Repair Engine! 🚀**

For detailed information, refer to the full documentation:
- README.md (Overview)
- ARCHITECTURE.md (Design)
- API.md (Integration)
- DEPLOYMENT.md (Operations)
