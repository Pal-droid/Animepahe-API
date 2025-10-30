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

        # Extract all buttons from the resolution dropdown (quality info)
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

        # Fallback if structured dropdown not found
        if not sources:
            kwik_links = re.findall(r"https:\/\/kwik\.(si|cx|link)\/e\/\w+", html)
            sources = [{"url": link, "quality": None, "fansub": None, "audio": None} for link in kwik_links]

        # Deduplicate
        unique_sources = list({s["url"]: s for s in sources}.values())

        # Optional: sort by resolution descending
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
        """
        Resolve a kwik URL by executing its packed JS in Node with an eval/Function interceptor
        to capture any unpacked source that contains the final .m3u8 link.
        """
        html = await asyncio.to_thread(lambda: self.scraper.get(kwik_url, headers=self.headers, timeout=20).text)
        # quick direct search
        m3u8_direct = re.search(r"https?://[^'\"\s<>]+\.m3u8", html)
        if m3u8_direct:
            return m3u8_direct.group(0)

        # find candidate <script> blocks
        scripts = re.findall(r"(<script[^>]*>[\s\S]*?</script>)", html, re.IGNORECASE)
        script_block, largest_eval_script, max_len = None, None, 0
        for s in scripts:
            if "eval(" in s:
                if "Plyr" in s or ".m3u8" in s or "source" in s or "uwu" in s:
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
            raise Exception("No candidate <script> block found to evaluate")

        # extract inner JS
        inner_js = re.sub(r"^<script[^>]*>", "", script_block, flags=re.IGNORECASE).strip()
        inner_js = re.sub(r"</script>$", "", inner_js, flags=re.IGNORECASE).strip()

        # Build Node wrapper that intercepts eval() and Function() calls and captures their arguments/bodies
        wrapper = r"""
globalThis.window = { location: {} };
globalThis.document = { cookie: '' };
globalThis.navigator = { userAgent: 'mozilla' };

const __captured = [];
const origLog = console.log;
console.log = function(...args){
  try { __captured.push(args.map(a=>String(a)).join(' ')); } catch(e){}
  origLog.apply(console,args);
};

// intercept eval and Function to capture unpacked code or eval args
(function(){
  try {
    const origEval = globalThis.eval;
    globalThis.eval = function(x){
      try { __captured.push('[__EVAL_ARG__] ' + String(x)); } catch(e){}
      return origEval(x);
    };
  } catch(e) {}

  try {
    const OrigFunction = globalThis.Function;
    globalThis.Function = function(...args){
      const body = args.length ? args[args.length-1] : '';
      try { __captured.push('[__FUNCTION_BODY__] ' + String(body)); } catch(e){}
      return OrigFunction.apply(this, args);
    };
  } catch(e) {}
})();
"""

        # final runner will append inner_js and then print captured blocks
        final_js = wrapper + "\n\n" + inner_js + "\n\n" + (
            "setTimeout(()=>{ "
            "for(const c of __captured){ console.log('__CAPTURED__START__'); console.log(c); console.log('__CAPTURED__END__'); } "
            "try{ process.exit(0) } catch(e){} }, 150);"
        )

        # write to temp file
        with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8") as tf:
            tmp_path = tf.name
            tf.write(final_js)
            tf.flush()

        out = ""
        try:
            proc = await asyncio.create_subprocess_exec(
                node_bin, tmp_path,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            try:
                # keep a modest timeout so badly-behaved scripts can't hang forever
                stdout_data, stderr_data = await asyncio.wait_for(proc.communicate(), timeout=6.0)
            except asyncio.TimeoutError:
                try:
                    proc.kill()
                except Exception:
                    pass
                raise Exception("Node execution timed out")

            if stdout_data:
                out += stdout_data.decode(errors="ignore")
            if stderr_data:
                out += "\n[stderr]\n" + stderr_data.decode(errors="ignore")
        finally:
            try:
                os.unlink(tmp_path)
            except Exception:
                pass

        # 1) direct search in node output (stdout/stderr)
        m = re.search(r"https?://[^'\"\s]+\.m3u8[^\s'\"\)]*", out)
        if m:
            return m.group(0)

        # 2) search inside captured blocks printed by our wrapper
        captured_blocks = re.findall(r"__CAPTURED__START__\s*\n([\s\S]*?)\n__CAPTURED__END__", out)
        for block in captured_blocks:
            m2 = re.search(r"https?://[^'\"\s]+\.m3u8[^\s'\"\)]*", block)
            if m2:
                return m2.group(0)
            # sometimes the unpacked code contains a variable assignment or a string with the url; search for .m3u8 inside text
            if ".m3u8" in block:
                # try to pull surrounding URL-like token
                m3 = re.search(r"https?://[^\s'\"\\)]+?\.m3u8[^\s'\"\\)]*", block)
                if m3:
                    return m3.group(0)

        # 3) fallback: maybe inner code writes to window.q or similar, try execjs fallback like before
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

        # 4) final fallback: look for data-src attributes in original HTML
        m_html = re.search(r'data-src="([^"]+\.m3u8[^"]*)"', html)
        if m_html:
            return m_html.group(1)

        # nothing found
        raise Exception(f"Could not resolve .m3u8. Node output (first 2000 chars):\n{out[:2000]}")

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