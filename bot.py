import os
import asyncio
import signal
import re
import io
import ipaddress
import socket
import zipfile
from collections import deque
from urllib.parse import urljoin, urlparse, urldefrag, unquote
from html.parser import HTMLParser

import requests
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, ContextTypes, filters

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
MAX_PAGES = 20
MAX_FILES = 250
MAX_TOTAL_BYTES = 35 * 1024 * 1024
MAX_FILE_BYTES = 8 * 1024 * 1024
TIMEOUT = 15
UA = "PublicSiteAssetCollector/1.0"

class AssetParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.links, self.assets = [], []

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "a" and a.get("href"):
            self.links.append(a["href"])
        if tag in ("script", "img", "source", "iframe", "video", "audio") and a.get("src"):
            self.assets.append(a["src"])
        if tag == "link" and a.get("href"):
            rel = (a.get("rel") or "").lower()
            if any(x in rel for x in ("stylesheet", "icon", "preload", "modulepreload")):
                self.assets.append(a["href"])
        if tag == "video" and a.get("poster"):
            self.assets.append(a["poster"])

def public_url(url):
    p = urlparse(url)
    if p.scheme not in ("http", "https") or not p.hostname or p.username or p.password:
        return False
    host = p.hostname.lower()
    if host == "localhost" or host.endswith((".localhost", ".local")):
        return False
    try:
        infos = socket.getaddrinfo(host, p.port or (443 if p.scheme == "https" else 80))
        return all(ipaddress.ip_address(item[4][0]).is_global for item in infos)
    except Exception:
        return False

def fetch(url, session):
    if not public_url(url):
        raise ValueError("Only public HTTP(S) URLs are allowed.")
    response = session.get(url, timeout=TIMEOUT, allow_redirects=False, stream=True)
    if response.is_redirect:
        target = urljoin(url, response.headers.get("Location", ""))
        response.close()
        if not public_url(target):
            raise ValueError("Redirect target is not public.")
        response = session.get(target, timeout=TIMEOUT, allow_redirects=False, stream=True)
    response.raise_for_status()
    chunks, size = [], 0
    for chunk in response.iter_content(65536):
        if not chunk:
            continue
        size += len(chunk)
        if size > MAX_FILE_BYTES:
            raise ValueError("File too large.")
        chunks.append(chunk)
    return response.url, response.headers.get("Content-Type", ""), b"".join(chunks)

def filename_for(url, content_type):
    parsed = urlparse(url)
    host = re.sub(r"[^A-Za-z0-9_-]", "_", parsed.hostname or "site")
    path = unquote(parsed.path).strip("/") or "index.html"
    path = re.sub(r"[^A-Za-z0-9._/-]", "_", path)
    if "." not in path.rsplit("/", 1)[-1]:
        ctype = content_type.lower()
        ext = ".css" if "css" in ctype else ".js" if "javascript" in ctype else ".html" if "html" in ctype else ".bin"
        path += ext
    return f"{host}/{path}"

def collect(start_url):
    if not public_url(start_url):
        raise ValueError("Send a public http:// or https:// website URL.")
    start_url = urldefrag(start_url)[0]
    origin = (urlparse(start_url).scheme, urlparse(start_url).netloc.lower())
    session = requests.Session()
    session.headers.update({"User-Agent": UA})
    page_queue = deque([start_url])
    pages_seen, assets_seen, saved = set(), set(), {}
    total = 0

    while page_queue and len(pages_seen) < MAX_PAGES and len(saved) < MAX_FILES:
        page = urldefrag(page_queue.popleft())[0]
        parsed = urlparse(page)
        if page in pages_seen or (parsed.scheme, parsed.netloc.lower()) != origin:
            continue
        pages_seen.add(page)
        try:
            final_url, ctype, data = fetch(page, session)
            if "html" not in ctype.lower() and not data.lstrip().lower().startswith((b"<!doctype html", b"<html")):
                continue
            name = filename_for(final_url, "text/html")
            saved[name] = data
            total += len(data)
            parser = AssetParser()
            parser.feed(data.decode("utf-8", errors="replace"))
            for ref in parser.assets:
                assets_seen.add(urldefrag(urljoin(final_url, ref.strip()))[0])
            for ref in parser.links:
                nxt = urldefrag(urljoin(final_url, ref.strip()))[0]
                np = urlparse(nxt)
                if (np.scheme, np.netloc.lower()) == origin and nxt not in pages_seen:
                    page_queue.append(nxt)
        except Exception:
            continue

    pending = list(assets_seen)
    processed = set()
    while pending and len(saved) < MAX_FILES and total < MAX_TOTAL_BYTES:
        asset = pending.pop(0)
        if asset in processed:
            continue
        processed.add(asset)
        try:
            final_url, ctype, data = fetch(asset, session)
            if total + len(data) > MAX_TOTAL_BYTES:
                continue
            name = "assets/" + filename_for(final_url, ctype).replace("/", "_")
            stem, dot, suffix = name.rpartition(".")
            candidate, counter = name, 1
            while candidate in saved:
                candidate = f"{stem}_{counter}.{suffix}" if dot else f"{name}_{counter}"
                counter += 1
            saved[candidate] = data
            total += len(data)

            if "css" in ctype.lower() or final_url.lower().endswith(".css"):
                css = data.decode("utf-8", errors="replace")
                # Extract CSS url(...) and @import references safely.
                for match in re.findall(r"""url\(\s*['"]?([^)'"]+)""", css, flags=re.I):
                    if not match.strip().startswith("data:"):
                        pending.append(urldefrag(urljoin(final_url, match.strip()))[0])
                for match in re.findall(r"""@import\s+['"]([^'"]+)['"]""", css, flags=re.I):
                    pending.append(urldefrag(urljoin(final_url, match.strip()))[0])
        except Exception:
            continue

    return saved, len(pages_seen), total

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Send a public website URL. I’ll ZIP publicly served HTML pages and linked CSS, "
        "JavaScript, images, fonts and other static assets.\\n\\n"
        "Limits: 20 pages, 250 files, 35 MB total. Server-side code, private files, "
        "protected content and login-only resources cannot be collected."
    )

async def handle_url(update: Update, context: ContextTypes.DEFAULT_TYPE):
    match = re.search(r"https?://\S+", (update.message.text or "").strip())
    if not match:
        await update.message.reply_text("Send a full URL starting with http:// or https://")
        return
    url = match.group(0).rstrip(".,)")
    status = await update.message.reply_text("Collecting public website files…")
    try:
        files, pages, total = collect(url)
        if not files:
            await status.edit_text("No public HTML/assets could be collected from this URL.")
            return
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
            for name, content in files.items():
                archive.writestr(name, content)
            archive.writestr(
                "COLLECTION_NOTES.txt",
                "Public browser-delivered files only; not the original server project.\\n"
                f"Pages visited: {pages}\\nFiles saved: {len(files)}\\nBytes: {total}\\n"
                "No backend source, database, secrets, private or protected files included.\\n"
            )
        buffer.seek(0)
        host = (urlparse(url).hostname or "website").replace(".", "_")
        await update.message.reply_document(
            document=buffer,
            filename=f"{host}_public_assets.zip",
            caption=f"Collected {len(files)} public files from {pages} page(s)."
        )
        await status.delete()
    except Exception as error:
        await status.edit_text(f"Collection failed: {str(error)[:250]}")

async def run_bot():
    if not BOT_TOKEN:
        raise SystemExit("Missing BOT_TOKEN environment variable.")

    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_url))

    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()

    # Render sends SIGTERM when stopping/restarting a worker.
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop_event.set)
        except (NotImplementedError, RuntimeError):
            pass

    await app.initialize()
    try:
        await app.start()
        await app.updater.start_polling(drop_pending_updates=True)
        print("Telegram bot is running and polling.", flush=True)
        await stop_event.wait()
    finally:
        if app.updater and app.updater.running:
            await app.updater.stop()
        if app.running:
            await app.stop()
        await app.shutdown()

def main():
    asyncio.run(run_bot())

if __name__ == "__main__":
    main()
