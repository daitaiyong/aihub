# AI Hub

AI Hub is a Flask-based AI information workspace that aggregates AI news, technical papers, and learning resources.

## Features

- AI news, technical papers, and learning resources in one dashboard
- RSS feed aggregation with manual refresh support
- Daily DeepSeek content update at `00:00:00` using the server's local timezone
- Search and category filtering
- Pagination for category pages, with 12 articles per page
- User registration, login, logout, and three-day sessions
- Strong password validation
- Per-user article favorites
- SQLite persistence with automatic database initialization
- Refresh and DeepSeek task status endpoints

## Project Structure

```text
.
├── app.py
├── aihub.db                 # Created and updated at runtime
├── templates/
│   └── index.html
└── README.md
```

## Requirements

- Python 3.10+
- Flask
- feedparser
- A DeepSeek API key for the daily AI-generated update

Install the Python dependencies:

```bash
python3 -m pip install flask feedparser
```

## Run Locally

From the project directory:

```bash
python3 app.py
```

Open:

```text
http://127.0.0.1:5001
```

If port `5001` is already in use, stop the existing Flask process before starting a new one.

## Configuration

Set environment variables before starting the application:

```bash
export DEEPSEEK_API_KEY="your-api-key"
export DEEPSEEK_MODEL="deepseek-chat"
export DEEPSEEK_TIMEOUT_SECONDS=90
export AIHUB_REFRESH_INTERVAL_MINUTES=30
export AIHUB_FEED_TIMEOUT_SECONDS=12
```

Configuration defaults:

| Variable | Default | Purpose |
| --- | ---: | --- |
| `DEEPSEEK_API_KEY` | empty | Enables the daily DeepSeek update |
| `DEEPSEEK_API_URL` | `https://api.deepseek.com/chat/completions` | DeepSeek-compatible chat endpoint |
| `DEEPSEEK_MODEL` | `deepseek-chat` | Model used for content curation |
| `DEEPSEEK_TIMEOUT_SECONDS` | `90` | DeepSeek request timeout |
| `AIHUB_REFRESH_INTERVAL_MINUTES` | `30` | RSS background refresh interval |
| `AIHUB_FEED_TIMEOUT_SECONDS` | `12` | Per-feed RSS timeout |

Never commit API keys or other secrets to the repository.

## Scheduled Updates

The application starts two background schedulers when run with `python3 app.py`:

1. RSS refresh runs at the configured interval, defaulting to every 30 minutes.
2. DeepSeek content generation runs once every day at `00:00:00` in the server's local timezone.

The DeepSeek task uses current RSS entries as context, generates structured JSON for all three categories, validates the result, and then saves valid articles to SQLite. If the API key is missing or the request fails, the existing content remains available.

## API Endpoints

### Articles and content

```text
GET    /api/articles
GET    /api/articles/<id>
GET    /api/favorites
GET    /api/sources
GET    /api/stats
```

Article query parameters include:

```text
category=ai_news|papers|learning
q=<search text>
source=<source name>
page=<page number>
limit=<1-100>
favorites=true|false
```

Category pages use 12 articles per page in the web interface:

```text
/api/articles?category=ai_news&page=1&limit=12
```

### Favorites

```text
POST   /api/articles/<id>/favorite
DELETE /api/articles/<id>/favorite
```

Favorites require an authenticated user.

### Authentication

```text
POST /api/auth/register
POST /api/auth/login
GET  /api/auth/me
POST /api/auth/logout
```

Registration fields:

```json
{
  "username": "demo_user",
  "email": "demo@example.com",
  "phone": "13800138000",
  "password": "StrongPass1!"
}
```

The phone number is optional. Passwords must contain at least 8 characters, including uppercase and lowercase letters, a number, and a special character. Sessions expire after three days.

### Refresh and task status

```text
GET  /api/health
GET  /api/refresh/status
GET  /api/refresh
GET  /api/deepseek/status
POST /api/deepseek/refresh
```

`/api/refresh` starts an asynchronous RSS refresh. `/api/deepseek/refresh` manually starts the DeepSeek update and is useful for testing without waiting for midnight.

## Verification

Compile-check the backend:

```bash
python3 -m py_compile app.py
```

Check application health:

```bash
curl http://127.0.0.1:5001/api/health
```

Check DeepSeek task configuration:

```bash
curl http://127.0.0.1:5001/api/deepseek/status
```
