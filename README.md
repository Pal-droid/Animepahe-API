# AnimePahe API Scraper

🚀 **Been trying to find an actually working AnimePahe API that can bypass Cloudflare? Well, this repo has got your back!**  

This project provides an **async Python FastAPI backend** to search for anime, fetch episodes, and resolve streaming sources (including `.m3u8` links) from [AnimePahe](https://animepahe.si). It uses `cloudscraper` to bypass Cloudflare IUAM protection and optionally `Node.js` or `PyExecJS` to evaluate obfuscated JavaScript when needed.

---

## Features

- Search anime by query
- Get all episodes for a given anime session
- Retrieve source links for a specific episode
- Resolve `.m3u8` URLs from Kwik or embedded players
- FastAPI backend for easy integration with frontends or other tools
- Async, efficient, and capable of bypassing Cloudflare restrictions

---

## Installation

1. **Clone the repository:**

```bash
git clone https://github.com/yourusername/animepahe-api-scraper.git
cd animepahe-api-scraper
```

2. **Install dependencies:**

```bash
pip install -r requirements.txt
```

**Dependencies include:**

- `fastapi`
- `cloudscraper`
- `httpx`
- `beautifulsoup4`
- `execjs`
- `uvicorn` (for running FastAPI)

3. **(Optional) Install Node.js**  
Needed if you want `.m3u8` resolution using Node.js:

```bash
# macOS
brew install node

# Ubuntu/Debian
sudo apt install nodejs npm
```

---

## Usage

### Run FastAPI server:

```bash
uvicorn main:app --reload
```

### API Endpoints

| Endpoint | Method | Query Params | Description |
|----------|--------|--------------|-------------|
| `/search` | GET | `q` | Search for anime by query |
| `/episodes` | GET | `session` | Get all episodes for an anime session |
| `/sources` | GET | `anime_session`, `episode_session` | Get all source links for an episode |
| `/m3u8` | GET | `url` | Resolve a Kwik link to direct `.m3u8` URL |

**Example:**

```bash
curl "http://127.0.0.1:8000/search?q=naruto"
curl "http://127.0.0.1:8000/episodes?session=abc123"
curl "http://127.0.0.1:8000/sources?anime_session=abc123&episode_session=ep1"
curl "http://127.0.0.1:8000/m3u8?url=https://kwik.si/e/xyz123"
```

---

## Notes

- `cloudscraper` handles basic Cloudflare IUAM pages. Some advanced protections may still fail.
- `.m3u8` resolution can fall back to `execjs` if Node.js is unavailable.
- Always respect the website's terms of service and do not abuse the API.