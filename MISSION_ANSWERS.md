# Day 12 Lab - Mission Answers

## Part 1: Localhost vs Production

### Exercise 1.1: Anti-patterns found
In `01-localhost-vs-production/develop/app.py`, the following 5 anti-patterns were identified:
1. **Hardcoded Secrets**: The API key (`OPENAI_API_KEY = "sk-hardcoded-fake-key-never-do-this"`) and the database connection URL (`DATABASE_URL = "postgresql://admin:password123@localhost:5432/mydb"`) are hardcoded directly in the source file. This is highly insecure as they can be leaked if pushed to a public repository.
2. **Lack of Configuration Management**: The configuration values (`DEBUG = True`, `MAX_TOKENS = 500`) are defined as hardcoded constants inside python code rather than loading dynamically from environment variables or a settings module.
3. **Insecure and Unstructured Logging**: The application logs messages using python's built-in `print()` statement (which prints straight to `stdout` in an unstructured format instead of proper levels like `INFO`/`ERROR`) and explicitly prints the sensitive API Key (`Using key: ...`).
4. **No Health / Readiness Endpoints**: There are no health check endpoints defined (such as `/health` or `/ready`). Cloud orchestration platforms cannot detect if the service has crashed or if it is ready to receive requests.
5. **Hardcoded Server Binding**: The server runs uvicorn on `host="localhost"` and `port=8000` hardcoded. When deployed inside a Docker container or cloud platform (like Railway/Render), it won't be accessible from outside the container, and platforms cannot dynamically assign ports via the `PORT` environment variable. Additionally, `reload=True` is enabled by default which eats memory and degrades performance in production.

### Exercise 1.3: Comparison table

| Feature | Develop | Production | Why Important? |
|:---|:---|:---|:---|
| **Config & Secrets** | Hardcoded inside python files. | Loaded dynamically from environment variables using a centralized `Settings` class. | Keeps API keys and DB credentials out of version control and lets us run the same code in dev/staging/prod without recompiling. |
| **Health Probes** | None. | `/health` (liveness probe) and `/ready` (readiness probe). | Allows orchestration platforms to check if the instance is alive (restart if dead) and if it is ready to handle traffic (don't route to it yet if loading). |
| **Logging** | Raw `print()` statements outputting straight to console. | Structured JSON logging containing timestamps, levels, and key-value fields. | Enables log management aggregators (Datadog, Loki) to index, query, parse, and search application logs efficiently while avoiding leaks. |
| **Shutdown** | Process is terminated abruptly, immediately killing connections. | Graceful shutdown catches `SIGTERM`/`SIGINT`, sets `_is_ready = False` to reject new traffic, and waits for in-flight requests to complete. | Prevents request drops, data loss, incomplete database transactions, and 502 Bad Gateway responses when instances scale down or redeploy. |

---

## Part 2: Docker

### Exercise 2.1: Dockerfile questions
1. **Base image**: The base image used is `python:3.11` (in basic build) or `python:3.11-slim` (in production/advanced build).
2. **Working directory**: The working directory inside the container is set to `/app` via `WORKDIR /app`.
3. **Why COPY requirements.txt first?**: Docker builds images using layered caching. By copying only `requirements.txt` first and running `pip install`, Docker caches the installed packages layer. As long as `requirements.txt` does not change, subsequent rebuilds will bypass package installation completely, resulting in much faster builds.
4. **CMD vs ENTRYPOINT**: `CMD` sets a default command or argument list that can be easily overridden from the command line when running the container (e.g. `docker run <image> python check.py`). `ENTRYPOINT` configures a container to run as a dedicated executable, making the base command harder to override while allowing arguments to be appended. When combined, `CMD` acts as default arguments passed to the `ENTRYPOINT` command.

### Exercise 2.3: Image size comparison
- **Develop (Basic)**: `1.01 GB`
- **Production (Advanced)**: `148 MB`
- **Difference**: `~85.3%` reduction in image size.
*Note: The massive size reduction is achieved by using the `slim` python image instead of the full version, and employing a multi-stage build where compile-time packages, gcc, caches, and intermediate headers are kept only in the temporary `builder` stage and excluded from the final `runtime` stage.*

---

## Part 3: Cloud Deployment

### Exercise 3.1: Railway deployment
- **URL**: `https://production-ready-agent.up.railway.app`
- **Screenshot**: See screenshots in [screenshots/dashboard.png](file:///home/winie/2A202600723-NguyenThiVang-day12_ha-tang-cloud_va_deployment/screenshots/dashboard.png)

---

## Part 4: API Security

### Exercise 4.1-4.3: Test results
The test suite verify the API gateway security checks in the following order:
1. **Authentication (X-API-Key)**:
   - Requesting `POST /ask` without key returns `401 Unauthorized`.
   - Requesting `POST /ask` with incorrect key returns `401 Unauthorized`.
   - Requesting `POST /ask` with valid `X-API-Key` returns `200 OK`.
2. **Rate Limiting**:
   - Sending requests sequentially within the limit returns `200 OK`.
   - Sending more than 10 requests within a single minute returns `429 Too Many Requests` with `Retry-After` header.
3. **Cost Guard**:
   - Sending requests consuming tokens updates the daily/monthly budget in Redis.
   - When the cumulative daily budget exceeds the threshold (e.g. `$5.00`), the agent blocks further calls, returning `402 Payment Required` (or `503 Service Unavailable` depending on config) with `"Daily budget exhausted. Try tomorrow."`.

### Exercise 4.4: Cost Guard implementation
- **Approach**: The cost guard tracks token consumption stateless using Redis keys formatted as `budget:daily:<YYYY-MM-DD>`. 
- **Method**: Every prompt has its token usage estimated (based on word count proxy: `words * 2`). Before sending the prompt to the LLM, the system reads the current daily cost. If the cost has met or exceeded the daily budget (read from `Settings`), it immediately raises an `HTTPException(status_code=402, detail="Daily budget exhausted. Try tomorrow.")`. Otherwise, once the LLM returns the response, the actual input and output tokens are recorded, and the Redis key is incremented using `INCRBYFLOAT` with a TTL of 48 hours.

---

## Part 5: Scaling & Reliability

### Exercise 5.1-5.5: Implementation notes
- **Liveness & Readiness**: We implemented separate `/health` (checks if the server is up and responsive) and `/ready` (checks if dependencies like Redis are fully connected). If Redis drops, `/ready` returns `503 Service Unavailable`, forcing load balancers to route traffic away from the instance until connection recovers.
- **Stateless design**: All conversation histories are stored in Redis under the key `history:<session_id>` using list storage (`RPUSH` / `LTRIM` / `LRANGE`) with a TTL of 1 hour. This ensures that no chat history is stored in local process memory, allowing Nginx load balancers to freely distribute requests round-robin across multiple scaled instances without breaking chat sessions.
- **Graceful Shutdown**: Catches `SIGTERM` (sent by platforms like Kubernetes/Railway/Render when scaling down or deploying) and updates `_is_ready = False`. Probes will instantly start failing to prevent load balancers from routing new requests here, while existing requests in uvicorn finish processing cleanly within a 30-second window.
