# HTTP Metadata Inventory Service

A small FastAPI service that collects and stores HTTP metadata (headers, cookies, page source) for any URL you give it. MongoDB is used for storage and everything runs via Docker Compose.

---

## How to run

You just need Docker installed.

```bash
docker-compose up --build
```

That starts two things — the API on port 8000 and MongoDB. On subsequent runs you can drop `--build`.

API: http://localhost:8000  
Swagger docs: http://localhost:8000/docs

---

## Endpoints

### POST /metadata
Give it a URL, it crawls it and saves the metadata.

```bash
curl -X POST http://localhost:8000/metadata \
  -H "Content-Type: application/json" \
  -d '{"url": "https://example.com"}'
```

Returns the headers, cookies, page source and status code.

---

### GET /metadata?url=https://example.com
Returns stored metadata if it exists.

If the URL hasn't been crawled yet, it kicks off collection in the background and tells you to check back later:
```json
{
  "message": "Record doesn't exist & request has been logged to collect the metadata, please check later"
}
```

---

### GET /health
Quick check to see if the service and MongoDB are up.

---

## Security

SSRF protection is built in — if a submitted URL resolves to a private or internal IP (`127.0.0.1`, `10.x.x.x`, `192.168.x.x`) or the AWS/GCP metadata endpoint (`169.254.169.254`), the request is rejected with a 403 before any HTTP call is made.

---

## Run tests

No Docker needed for this — tests use an in-memory MongoDB mock.

```bash
pip install -r requirements.txt
python -m pytest tests/ -v
```

---

## Project structure

```
app/
  main.py       # routes
  services.py   # crawling logic + SSRF protection
  database.py   # MongoDB connection
  models.py     # request/response schemas
tests/
  test_api.py   # 10 tests covering all flows
Dockerfile
docker-compose.yml
```
