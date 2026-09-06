"""
terabox_api.py
FastAPI wrapper around terabox_client.TeraBoxClient.

Run:
    uvicorn terabox_api:app --host 0.0.0.0 --port 8000

Requires terabox_client.py and cookies.json in the same directory
(or set TERABOX_COOKIES_PATH env var to point elsewhere).

Endpoints:
    GET  /health
    GET  /list?path=/
    GET  /search?keyword=foo&path=/
    GET  /download-link?path=/some/file.pdf
    GET  /download?path=/some/file.pdf        (proxies the actual file bytes)
    POST /upload (multipart form, field name "file"; optional "remote_dir")
"""

import os
import shutil
import tempfile

from fastapi import FastAPI, HTTPException, UploadFile, File, Form, Query
from fastapi.responses import StreamingResponse
from fastapi.middleware.cors import CORSMiddleware

from terabox_client import TeraBoxClient, TeraBoxError

COOKIES_PATH = os.environ.get("TERABOX_COOKIES_PATH", "cookies.json")
COOKIES_JSON = os.environ.get("TERABOX_COOKIES_JSON")  # set this on Vercel instead of a file
IS_SERVERLESS = os.environ.get("VERCEL") == "1"

app = FastAPI(title="TeraBox API Wrapper", version="1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# Single shared client instance + lazy init, since jsToken/host detection
# takes a couple of real HTTP round trips — we don't want to redo that
# on every single request.
_client: TeraBoxClient | None = None


def get_client() -> TeraBoxClient:
    global _client
    if _client is None:
        if COOKIES_JSON:
            _client = TeraBoxClient(cookies_json=COOKIES_JSON)
        else:
            _client = TeraBoxClient(cookies_path=COOKIES_PATH)
        _client.init()
    return _client


def _reset_client_and_reraise(exc: Exception):
    """If a call fails, drop the cached client so the next request re-inits
    (fresh jsToken/host) instead of repeatedly failing with a stale session."""
    global _client
    _client = None
    raise HTTPException(status_code=502, detail=str(exc))


@app.get("/health")
def health():
    try:
        client = get_client()
        return {"status": "ok", "host": client.host}
    except TeraBoxError as e:
        _reset_client_and_reraise(e)


@app.get("/list")
def list_dir(path: str = Query("/", description="Directory path to list")):
    try:
        client = get_client()
        files = client.list_dir(path)
        return {"path": path, "count": len(files), "items": files}
    except TeraBoxError as e:
        _reset_client_and_reraise(e)


@app.get("/search")
def search(keyword: str, path: str = Query("/", description="Directory to search under")):
    try:
        client = get_client()
        results = client.search(keyword, path)
        return {"keyword": keyword, "path": path, "count": len(results), "items": results}
    except TeraBoxError as e:
        _reset_client_and_reraise(e)


@app.get("/download-link")
def download_link(path: str = Query(..., description="Full file path, e.g. /folder/file.pdf")):
    try:
        client = get_client()
        link = client.get_download_link(path)
        return {"path": path, "download_url": link}
    except TeraBoxError as e:
        _reset_client_and_reraise(e)


@app.get("/download")
def download(path: str = Query(..., description="Full file path, e.g. /folder/file.pdf")):
    """Proxies the actual file bytes back to the caller. NOTE: on Vercel,
    serverless function execution limits (10s Hobby / 60s Pro) make this
    unsuitable for large files — prefer /download-link and let the client
    download directly from TeraBox's CDN instead."""
    if IS_SERVERLESS:
        raise HTTPException(
            status_code=400,
            detail="Byte-proxying is disabled on serverless deployments due to "
                   "execution time limits. Use /download-link instead and download "
                   "directly from the returned URL.",
        )
    try:
        client = get_client()
        link = client.get_download_link(path)
    except TeraBoxError as e:
        _reset_client_and_reraise(e)
        return  # unreachable, keeps type checkers happy

    filename = os.path.basename(path)

    def stream():
        with client.session.get(link, stream=True, timeout=60) as r:
            r.raise_for_status()
            for chunk in r.iter_content(chunk_size=1024 * 1024):
                if chunk:
                    yield chunk

    return StreamingResponse(
        stream(),
        media_type="application/octet-stream",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.post("/upload")
async def upload(file: UploadFile = File(...), remote_dir: str = Form("/")):
    """Accepts a multipart file upload, writes it to a temp file, then
    hands it to TeraBoxClient.upload_file() for the precreate/chunk/finalize flow.
    NOTE: on Vercel, large uploads will likely exceed the execution time limit
    (10s Hobby / 60s Pro) since each 4MB chunk is a separate round trip."""
    try:
        client = get_client()
    except TeraBoxError as e:
        _reset_client_and_reraise(e)
        return

    tmp_dir = tempfile.mkdtemp()
    tmp_path = os.path.join(tmp_dir, file.filename)
    try:
        with open(tmp_path, "wb") as f:
            shutil.copyfileobj(file.file, f)
        result = client.upload_file(tmp_path, remote_dir=remote_dir, progress=False)
        return {"filename": file.filename, "remote_dir": remote_dir, "result": result}
    except TeraBoxError as e:
        _reset_client_and_reraise(e)
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


@app.get("/share/resolve")
def share_resolve(url: str = Query(..., description="Any TeraBox share URL, e.g. https://terabox.app/s/xxxxx"),
                   password: str | None = Query(None, description="Extraction code, if the share is password-protected"),
                   dir_path: str | None = Query(None, description="Subfolder path inside the share, to drill into folders")):
    try:
        client = get_client()
        items = client.resolve_share(url, password=password, dir_path=dir_path)
        return {"share_url": url, "count": len(items), "items": items}
    except TeraBoxError as e:
        _reset_client_and_reraise(e)


@app.get("/share/download-link")
def share_download_link(url: str = Query(..., description="Any TeraBox share URL"),
                         password: str | None = Query(None),
                         index: int = Query(0, description="Which file to pick if the share has multiple")):
    try:
        client = get_client()
        dlink, file_meta = client.get_share_download_link(url, password=password, index=index)
        return {"share_url": url, "download_url": dlink, "file": file_meta}
    except TeraBoxError as e:
        _reset_client_and_reraise(e)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
