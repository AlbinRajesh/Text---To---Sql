# Local suggestion fast path

The suggestion button always submits the corrected question immediately. When
Redis is available, the backend redeems a short-lived validated query plan;
otherwise, the frontend automatically uses the normal query pipeline.

For local fast-path testing, start Redis from the repository root:

```powershell
docker compose up -d redis
```

The backend defaults to `redis://localhost:6379/0`. Override it with
`SUGGESTION_REDIS_URL` when using a different Redis instance. The Redis
container binds only to localhost and stores no persistent data. The backend
health response reports `suggestion_fast_path` as `available` or `unavailable`.