import asyncio
import httpx
import re
import random
import tempfile
import os
import execjs
from bs4 import BeautifulSoup
from typing import Optional
from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse

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

    async def search(self, query: str):
        url = f"{self.base}/api?m=search&q={query}"
        async with httpx.AsyncClient(follow_redirects=True) as client:
            r = await client.get(url, headers=self.headers)
            r.raise_for_status()
            data = r.json()
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
        async with httpx.AsyncClient(follow_redirects=True) as client:
            r = await client.get(f"{self.base}/anime/{anime_session}", headers=self.headers)
            r.raise_for_status()
            html = r.text
        soup = BeautifulSoup(html, "html.parser")
        meta = soup.find("meta", {"property": "og:url"})
        if not meta:
            raise Exception("Could not find session ID in meta tag")
        parts = meta["content"].split("/")
        temp_id = parts[-1]

        async with httpx.AsyncClient(follow_redirects=True) as client:
            r = await client.get(
                f"{self.base}/api?m=release&id={temp_id}&sort=episode_asc&page=1",
                headers=self.headers,
            )
            r.raise_for_status()
            first_page = r.json()
        episodes = first_page.get("data", [])
        last_page = first_page.get("last_page", 1)

        async def fetch_page(p):
            async with httpx.AsyncClient(follow_redirects=True) as client:
                r = await client.get(
                    f"{self.base}/api?m=release&id={temp_id}&sort=episode_asc&page={p}",
                    headers=self.headers,
                )
                r.raise_for_status()
                return r.json().get("data", [])

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
        async with httpx.AsyncClient(follow_redirects=True) as client:
            r = await client.get(url, headers=self.headers)
            r.raise_for_status()
            html = r.text
        matches = re.findall(r'data-src="([^"]+)"', html)
        unique = []
        for m in matches:
            if m not in unique:
                unique.append(m)
        if not unique:
            kwik_links = re.findall(r"https:\/\/kwik\.si\/e\/\w+", html)
            unique = kwik_links
        if not unique:
            raise Exception("No source links found on play page")
        return unique

    async def resolve_kwik_with_node(self, kwik_url: str, node_bin: str = "node") -> str:
        async with httpx.AsyncClient(follow_redirects=True) as client:
            r = await client.get(kwik_url, headers=self.headers, timeout=20.0)
            r.raise_for_status()
            html = r.text
        m3u8_direct = re.search(r"https?://[^'\"\s<>]+\.m3u8", html)
        if m3u8_direct:
            return m3u8_direct.group(0)

        script_block = None
        scripts = re.findall(r"(<script[^>]*>[\s\S]*?</script>)", html, re.IGNORECASE)
        largest_eval_script = None
        max_len = 0
        for s in scripts:
            if "eval(" in s:
                if "source" in s or ".m3u8" in s or "Plyr" in s:
                    script_block = s
                    break
                if len(s) > max_len:
                    max_len = len(s)
                    largest_eval_script = s
        if not script_block:
            script_block = largest_eval_script
        if not script_block:
            m_html = re.search(r'data-src="([^"]+\.m3u8[^"]*)"', html)
            if m_html:
                return m_html.group(1)
            raise Exception("No candidate <script> block found to evaluate with Node/ExecJS")

        inner_js = re.sub(r"^<script[^>]*>", "", script_block, flags=re.IGNORECASE).strip()
        inner_js = re.sub(r"</script>$", "", inner_js, flags=re.IGNORECASE).strip()
        transformed_node = inner_js
        transformed_node = re.sub(r"\bdocument\b", "DOC_STUB", transformed_node)
        transformed_node = re.sub(r"^(var|const|let|j)\s*q\s*=", "window.q = ", transformed_node, flags=re.MULTILINE)
        transformed_node += "\ntry { console.log(window.q); } catch(e) { console.log('Variable q not found'); }"

        with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding='utf-8') as tf:
            tmp_path = tf.name
            tf.write("globalThis.window = { location: {} };\n")
            tf.write("globalThis.document = { cookie: '' };\n")
            tf.write("const DOC_STUB = globalThis.document;\n")
            tf.write("globalThis.navigator = { userAgent: 'mozilla' };\n")
            tf.write(transformed_node)
            tf.flush()

        proc = await asyncio.create_subprocess_exec(
            node_bin, tmp_path,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        stdout_data, stderr_data = await proc.communicate()
        try:
            os.unlink(tmp_path)
        except Exception:
            pass
        out = ""
        if stdout_data:
            out += stdout_data.decode(errors="ignore")
        if stderr_data:
            out += "\n[stderr]\n" + stderr_data.decode(errors="ignore")
        m = re.search(r"https?://[^'\"\s]+\.m3u8[^\s'\"\)]*", out)
        if m:
            return m.group(0)
        try:
            js_code = (
                "var window = { location: {} };"
                "var document = { cookie: '' };"
                "var navigator = { userAgent: 'mozilla' };"
                "var q; "
                f"var code = `{inner_js.replace('`', '\\`')}`.trim();"
                f"code = code.replace(/^(var|const|let|j)\\s*q\\s*=/, 'q=');"
                "eval(code); return q;"
            )
            js_m3u8 = execjs.exec_(js_code)
            if js_m3u8 and isinstance(js_m3u8, str) and '.m3u8' in js_m3u8:
                return js_m3u8
        except Exception:
            pass
        m_html = re.search(r'data-src="([^"]+\.m3u8[^"]*)"', html)
        if m_html:
            return m_html.group(1)
        raise Exception(f"Could not resolve to .m3u8. Node output (first 2000 chars):\n{out[:2000]}")

# -------------------- FastAPI --------------------
app = FastAPI()
pahe = AnimePahe()

@app.get("/search")
async def api_search(q: str):
    try:
        results = await pahe.search(q)
        return JSONResponse(content=results)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/episodes")
async def api_episodes(session: str):
    try:
        eps = await pahe.get_episodes(session)
        return JSONResponse(content=eps)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/sources")
async def api_sources(anime_session: str, episode_session: str):
    try:
        srcs = await pahe.get_sources(anime_session, episode_session)
        return JSONResponse(content=srcs)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/m3u8")
async def api_resolve_kwik(url: str):
    try:
        m3u8 = await pahe.resolve_kwik_with_node(url)
        return JSONResponse(content={"m3u8": m3u8})
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
