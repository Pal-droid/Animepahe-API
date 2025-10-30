import asyncio
import re
import random
import tempfile
import os
import execjs
from bs4 import BeautifulSoup
from typing import Optional
from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
import cloudscraper
import subprocess

# -------------------- Utility --------------------
def random_user_agent():
    agents = [
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 13_0) AppleWebKit/605.1.15 "
        "(KHTML, like Gecko) Version/16.1 Safari/605.1.15",
        "Mozilla/5.0 (Linux; Android 12; SM-G998B) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Mobile Safari/537.36",
    ]
    return random.choice(agents)

# -------------------- AnimePahe Class --------------------
class AnimePahe:
    def __init__(self):
        self.base = "https://animepahe.si"
        self.headers = {
            "User-Agent": random_user_agent(),
            "Cookie": "__ddg1_=;__ddg2_=",
            "Referer": "https://animepahe.si/",
        }
        self.scraper = cloudscraper.create_scraper()

    async def search(self, query: str):
        url = f"{self.base}/api?m=search&q={query}"
        html = await asyncio.to_thread(lambda: self.scraper.get(url, headers=self.headers).text)
        data = execjs.eval(f"JSON.parse(`{html}`)") if isinstance(html, str) else html
        results = []
        for a in data.get("data", []):
            results.append({
                "id": a["id"],
                "title": a["title"],
                "url": f"{self.base}/anime/{a['session']}",
                "year": a.get("year"),
                "poster": a.get("poster"),
                "type": a.get("type"),
                "session": a.get("session")
            })
        return results

    async def get_episodes(self, anime_session: str):
        html = await asyncio.to_thread(lambda: self.scraper.get(f"{self.base}/anime/{anime_session}", headers=self.headers).text)
        soup = BeautifulSoup(html, "html.parser")
        meta = soup.find("meta", {"property": "og:url"})
        if not meta:
            raise Exception("Could not find session ID in meta tag")
        temp_id = meta["content"].split("/")[-1]

        first_page_json = await asyncio.to_thread(
            lambda: self.scraper.get(f"{self.base}/api?m=release&id={temp_id}&sort=episode_asc&page=1", headers=self.headers).json()
        )
        episodes = first_page_json.get("data", [])
        last_page = first_page_json.get("last_page", 1)

        async def fetch_page(p):
            return await asyncio.to_thread(
                lambda: self.scraper.get(f"{self.base}/api?m=release&id={temp_id}&sort=episode_asc&page={p}", headers=self.headers).json().get("data", [])
            )

        tasks = [fetch_page(p) for p in range(2, last_page + 1)]
        for pages in await asyncio.gather(*tasks):
            episodes.extend(pages)

        return [
            {
                "id": e["id"],
                "number": e["episode"],
                "title": e.get("title") or f"Episode {e['episode']}",
                "snapshot": e.get("snapshot"),
                "session": e["session"],
            }
            for e in episodes
        ]

    async def get_sources(self, anime_session: str, episode_session: str):
        url = f"{self.base}/play/{anime_session}/{episode_session}"
        html = await asyncio.to_thread(lambda: self.scraper.get(url, headers=self.headers).text)

        buttons = re.findall(
            r'<button[^>]+data-src="([^"]+)"[^>]+data-fansub="([^"]+)"[^>]+data-resolution="([^"]+)"[^>]+data-audio="([^"]+)"[^>]*>',
            html
        )

        sources = []
        for src, fansub, resolution, audio in buttons:
            if src.startswith("https://kwik."):
                sources.append({
                    "url": src,
                    "quality": f"{resolution}p",
                    "fansub": fansub,
                    "audio": audio
                })

        if not sources:
            kwik_links = re.findall(r"https:\/\/kwik\.(si|cx|link)\/e\/\w+", html)
            sources = [{"url": link, "quality": None, "fansub": None, "audio": None} for link in kwik_links]

        unique_sources = list({s["url"]: s for s in sources}.values())

        def sort_key(s):
            try:
                return int(s["quality"].replace("p", "")) if s["quality"] else 0
            except Exception:
                return 0
        unique_sources.sort(key=sort_key, reverse=True)

        if not unique_sources:
            raise Exception("No kwik links found on play page")

        return unique_sources

    async def resolve_kwik_with_node(self, kwik_url: str, node_bin: str = "node") -> str:
        # --- Print raw HTTP response from Kwik ---
        resp = await asyncio.to_thread(lambda: self.scraper.get(kwik_url, headers=self.headers, timeout=20))
        print("\n" + "="*60)
        print(f"[DEBUG] Kwik HTTP {resp.status_code} {kwik_url}")
        print("="*60)
        print(resp.text[:5000])  # print first 5000 chars
        print("="*60 + "\n")

        html = resp.text

        m3u8_direct = re.search(r"https?://[^'\"\s<>]+\.m3u8", html)
        if m3u8_direct:
            return m3u8_direct.group(0)

        scripts = re.findall(r"(<script[^>]*>[\s\S]*?</script>)", html, re.IGNORECASE)
        script_block = next((s for s in scripts if "eval(" in s), None)
        if not script_block:
            raise Exception("No <script> block with eval() found")

        inner_js = re.sub(r"^<script[^>]*>", "", script_block, flags=re.IGNORECASE).strip()
        inner_js = re.sub(r"</script>$", "", inner_js, flags=re.IGNORECASE).strip()

        wrapper = r"""
globalThis.window = { location: {} };
globalThis.document = { cookie: '' };
globalThis.navigator = { userAgent: 'Mozilla/5.0' };
const __captured = [];
const origLog = console.log;
console.log = (...args) => { __captured.push(args.join(' ')); origLog(...args); };
(function(){
  const origEval = globalThis.eval;
  globalThis.eval = (x)=>{ __captured.push('[EVAL]' + x); return origEval(x); };
})();
"""

        final_js = wrapper + "\n" + inner_js + "\n" + (
            "setTimeout(()=>{for(const c of __captured){console.log('__CAPTURED__START__');"
            "console.log(c);console.log('__CAPTURED__END__');}process.exit(0)},300);"
        )

        with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8") as tf:
            tf.write(final_js)
            tmp_path = tf.name

        try:
            proc = await asyncio.create_subprocess_exec(
                node_bin, tmp_path,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            out, err = await proc.communicate()
            output = out.decode() + err.decode()
        finally:
            os.unlink(tmp_path)

        matches = re.findall(r"https?://[^'\"\s<>]+\.m3u8", output)
        if matches:
            return matches[0]
        raise Exception("Failed to extract m3u8 from Node output")


# -------------------- FastAPI App --------------------
app = FastAPI()
pahe = AnimePahe()

@app.get("/search")
async def search(q: str):
    return await pahe.search(q)

@app.get("/episodes/{session}")
async def episodes(session: str):
    return await pahe.get_episodes(session)

@app.get("/sources/{anime_session}/{episode_session}")
async def sources(anime_session: str, episode_session: str):
    return await pahe.get_sources(anime_session, episode_session)

@app.get("/resolve_kwik")
async def resolve_kwik(url: str):
    try:
        link = await pahe.resolve_kwik_with_node(url)
        return JSONResponse(content={"stream": link})
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

# -------------------- Run --------------------
if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000)