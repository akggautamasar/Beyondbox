"""
terabox_client.py
Unofficial TeraBox API client using exported browser cookies.

Pattern:
  - Loads cookies from a browser-exported cookies.json (EditThisCookie / Cookie-Editor format)
  - Filters to terabox.app / 1024terabox.com / terabox.com domains only
  - Fetches a fresh jsToken (required for every API call, expires with session)
  - Wraps list / file info / download-link / upload endpoints

Usage:
    from terabox_client import TeraBoxClient

    client = TeraBoxClient("cookies.json")
    client.init()                     # fetches jsToken, verifies session
    files = client.list_dir("/")
    for f in files:
        print(f["server_filename"], f["size"])

    link = client.get_download_link(files[0]["path"])
    client.download_file(link, "output.mp4")
"""

import json
import re
import time
import urllib.parse
import requests


class TeraBoxError(Exception):
    pass


class TeraBoxClient:
    # Common web client app_id used by most public TeraBox tools.
    APP_ID = "250528"

    # Try these hosts in order if one doesn't work for your account/region.
    CANDIDATE_HOSTS = [
        "www.terabox.app",
        "www.1024terabox.com",
        "www.terabox.com",
    ]

    def __init__(self, cookies_path: str = None, cookies_json: str = None, host: str = None):
        """Provide either cookies_path (a file, for local/Termux use) or
        cookies_json (a raw JSON string, for serverless deployments like
        Vercel where you store the cookie export as an env var)."""
        if not cookies_path and not cookies_json:
            raise TeraBoxError("Provide either cookies_path or cookies_json.")
        self.cookies_path = cookies_path
        self.cookies_json = cookies_json
        self.host = host  # if None, auto-detected in init()
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": (
                "Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/124.0.0.0 Mobile Safari/537.36"
            ),
            "Accept": "application/json, text/plain, */*",
        })
        self.js_token = None

    # ------------------------------------------------------------------ #
    # Setup
    # ------------------------------------------------------------------ #

    def _load_cookies(self):
        if self.cookies_json:
            raw = json.loads(self.cookies_json)
        else:
            with open(self.cookies_path, "r", encoding="utf-8") as f:
                raw = json.load(f)

        loaded = 0
        domains_seen = set()
        for c in raw:
            domain = c.get("domain", "")
            if "terabox" not in domain and "1024terabox" not in domain:
                continue
            name = c.get("name")
            value = c.get("value")
            if not name or value is None:
                continue
            # requests cookie jar wants domain without leading dot stripped is fine,
            # but we set it explicitly per-cookie so it matches all terabox subdomains.
            self.session.cookies.set(name, value, domain=domain.lstrip("."))
            domains_seen.add(domain)
            loaded += 1

        if loaded == 0:
            raise TeraBoxError(
                "No terabox cookies found in the given file. "
                "Make sure you exported cookies while logged into terabox.app."
            )

        has_ndus = any(c.name == "ndus" for c in self.session.cookies)
        if not has_ndus:
            raise TeraBoxError(
                "Loaded terabox cookies but 'ndus' (auth cookie) is missing. "
                "Re-export cookies while logged in."
            )

        return loaded, domains_seen

    def _detect_host(self):
        if self.host:
            return self.host
        for host in self.CANDIDATE_HOSTS:
            try:
                r = self.session.get(f"https://{host}/api/user/getinfo",
                                      params={"app_id": self.APP_ID}, timeout=10)
                if r.status_code == 200 and "errno" in r.text:
                    self.host = host
                    return host
            except requests.RequestException:
                continue
        # Fall back to first candidate even if detection was inconclusive.
        self.host = self.CANDIDATE_HOSTS[0]
        return self.host

    def _fetch_js_token(self):
        """jsToken is embedded in an obfuscated <script> on the main page and
        expires with the session, so it must be refreshed periodically."""
        url = f"https://{self.host}/main"
        r = self.session.get(url, timeout=15)
        r.raise_for_status()
        match = re.search(r'fn%28%22(.*?)%22%29', r.text)
        if not match:
            # Some layouts embed it differently; try the raw variant.
            match = re.search(r'"jsToken"\s*:\s*"([^"]+)"', r.text)
        if not match:
            raise TeraBoxError(
                "Could not extract jsToken from page. TeraBox may have changed "
                "its page structure, or the session cookies are expired/invalid."
            )
        self.js_token = match.group(1)
        return self.js_token

    def init(self):
        """Call once after construction: loads cookies, detects host, gets jsToken."""
        loaded, domains = self._load_cookies()
        self._detect_host()
        self._fetch_js_token()
        return {
            "cookies_loaded": loaded,
            "host": self.host,
            "js_token_acquired": bool(self.js_token),
        }

    # ------------------------------------------------------------------ #
    # Internal helpers
    # ------------------------------------------------------------------ #

    def _params(self, extra=None):
        p = {"app_id": self.APP_ID, "jsToken": self.js_token, "clienttype": 0}
        if extra:
            p.update(extra)
        return p

    def _request(self, method, path, params=None, data=None, files=None,
                 host_override=None, _retried=False):
        if not self.js_token:
            raise TeraBoxError("Client not initialized. Call init() first.")
        host = host_override or self.host
        url = f"https://{host}{path}"
        try:
            r = self.session.request(
                method, url, params=self._params(params),
                data=data, files=files, timeout=30,
            )
        except requests.exceptions.ConnectTimeout as e:
            raise TeraBoxError(
                f"Could not connect to {host} (timed out). This host may not "
                f"be reachable from your network/region, or it's not a real API "
                f"endpoint. Try setting client.host manually to a known-good value "
                f"like 'www.terabox.app' or 'www.1024terabox.com'."
            ) from e
        r.raise_for_status()
        data_json = r.json()
        errno = data_json.get("errno")

        if errno == -6 and not _retried:
            # TeraBox is telling us to switch to a regional API host.
            # Preserve the current top domain (terabox.app vs terabox.com etc.)
            # since our cookies are only valid for that domain — don't hardcode it.
            prefix = r.headers.get("Url-Domain-Prefix")
            if prefix:
                host_parts = host.split(".")
                base_domain = ".".join(host_parts[-2:])  # e.g. "terabox.app"
                new_host = f"{prefix}.{base_domain}"
                if host_override:
                    # Caller passed an explicit host (e.g. upload host) — just
                    # retry against the corrected one, don't touch self.host.
                    return self._request(method, path, params, data, files,
                                          host_override=new_host, _retried=True)
                self.host = new_host
                return self._request(method, path, params, data, files,
                                      _retried=True)
            raise TeraBoxError(
                "errno -6 (auth/session rejected) and no Url-Domain-Prefix header "
                "was returned to redirect to. Cookies may be stale — re-export them "
                "from a fresh logged-in browser session."
            )

        if errno in (4000023, 450016, 4500016) and not _retried:
            # jsToken expired mid-session; refresh and retry once.
            self._fetch_js_token()
            return self._request(method, path, params, data, files,
                                  host_override=host_override, _retried=True)

        if errno not in (0, None):
            raise TeraBoxError(f"TeraBox API error {errno}: {data_json}")
        return data_json

    def _get(self, path, params=None):
        return self._request("GET", path, params=params)

    def _post(self, path, params=None, data=None, files=None, host_override=None):
        return self._request("POST", path, params=params, data=data,
                              files=files, host_override=host_override)

    # ------------------------------------------------------------------ #
    # Public API
    # ------------------------------------------------------------------ #

    def list_dir(self, path="/", order="name", desc=0):
        """List files/folders in a directory."""
        data = self._get("/api/list", {
            "dir": path,
            "order": order,
            "desc": desc,
            "num": 1000,
            "page": 1,
        })
        return data.get("list", [])

    def file_info(self, path):
        """Get metadata (including dlink) for a file by its full path.
        TeraBox's filemetas endpoint keys on path + origin=dlna, not fs_id."""
        data = self._get("/api/filemetas", {
            "target": json.dumps([path]),
            "dlink": 1,
            "origin": "dlna",
        })
        info_list = data.get("info", [])
        if not info_list:
            raise TeraBoxError(f"No metadata found for path {path}")
        return info_list[0]

    def get_download_link(self, path):
        """Resolve a direct download URL for a file, given its full path
        (as returned in list_dir()'s 'path' field). The dlink from file_info
        redirects and needs to be followed with the same session/cookies."""
        info = self.file_info(path)
        dlink = info.get("dlink")
        if not dlink:
            raise TeraBoxError("No dlink returned; file may be unavailable or account lacks access.")
        # dlink often redirects once more to the actual CDN URL.
        r = self.session.get(dlink, allow_redirects=False, timeout=15)
        return r.headers.get("Location", dlink)

    def download_file(self, url, out_path, chunk_size=1024 * 1024):
        """Stream a resolved download URL to disk using the same authenticated session."""
        with self.session.get(url, stream=True, timeout=60) as r:
            r.raise_for_status()
            with open(out_path, "wb") as f:
                for chunk in r.iter_content(chunk_size=chunk_size):
                    if chunk:
                        f.write(chunk)
        return out_path

    def search(self, keyword, path="/"):
        data = self._get("/api/search", {"key": keyword, "dir": path, "recursion": 1})
        return data.get("list", [])

    # ------------------------------------------------------------------ #
    # Public share links (any TeraBox /s/... URL, not just your own files)
    # ------------------------------------------------------------------ #

    def _extract_share_code(self, share_url):
        """Follow redirects, pull the share code (surl) out of the final URL,
        and return it along with the page HTML (used to mine a fresh jsToken
        scoped to that share) and the resolved URL."""
        r = self.session.get(share_url, allow_redirects=True, timeout=15)
        final_url = r.url
        parsed = urllib.parse.urlparse(final_url)
        qs = urllib.parse.parse_qs(parsed.query)

        if "surl" in qs:
            surl = qs["surl"][0]
        else:
            m = re.search(r"/s/([\w-]+)", parsed.path)
            if not m:
                raise TeraBoxError(
                    f"Could not find a share code in this URL: {final_url}. "
                    f"Make sure it's a real TeraBox share link (contains /s/... or ?surl=...)."
                )
            surl = m.group(1)

        # TeraBox/Baidu-style short links show a leading '1' in the friendly
        # URL that must be stripped before it's used as the API 'surl' param.
        if surl.startswith("1"):
            surl = surl[1:]

        return surl, r.text, final_url

    def _js_token_from_html(self, html):
        match = re.search(r'fn%28%22(.*?)%22%29', html)
        if not match:
            match = re.search(r'"jsToken"\s*:\s*"([^"]+)"', html)
        return match.group(1) if match else None

    def resolve_share(self, share_url, password=None, dir_path=None):
        """Resolve a public TeraBox share URL into its file list, each item
        including a ready-to-use 'dlink' download URL.

        share_url: any terabox.app / 1024terabox.com / teraboxapp.com /s/... link
        password:  if the share is password-protected, pass its extraction code
        dir_path:  pass a subfolder path to list a folder inside the share
                   (root share listing gives you these paths to drill into)

        Returns: list of file/folder dicts as returned by TeraBox's API.
        """
        surl, html, final_url = self._extract_share_code(share_url)
        js_token = self._js_token_from_html(html)
        if not js_token:
            raise TeraBoxError(
                "Could not extract jsToken from the share page. The link may be "
                "invalid, expired, or TeraBox changed its page structure."
            )

        parsed = urllib.parse.urlparse(final_url)
        share_host = parsed.netloc or self.host

        if password:
            # Password-protected shares need a /share/verify call first; it
            # sets a 'randsk' cookie that authorizes the subsequent list call.
            verify_url = f"https://{share_host}/share/verify"
            r = self.session.post(verify_url, params={
                "surl": surl,
                "web": 1,
                "app_id": self.APP_ID,
                "channel": "dubox",
                "clienttype": 0,
            }, data={"pwd": password, "vcode": "", "vcode_str": ""}, timeout=15)
            vdata = r.json()
            if vdata.get("errno") not in (0, None):
                raise TeraBoxError(f"Share password verification failed: {vdata}")

        params = {
            "app_id": self.APP_ID,
            "web": 1,
            "channel": "dubox",
            "clienttype": 0,
            "jsToken": js_token,
            "dp-logid": int(time.time() * 1000),
            "page": 1,
            "num": 1000,
            "by": "name",
            "order": "asc",
            "shorturl": surl,
            "root": 1,
            "dlink": 1,
        }
        if dir_path:
            params["dir"] = dir_path
            params["root"] = 0

        list_url = f"https://{share_host}/share/list"
        r = self.session.get(list_url, params=params, timeout=20)
        r.raise_for_status()
        data = r.json()

        errno = data.get("errno")
        if errno == -9:
            raise TeraBoxError("This share requires a password (pass password=...).")
        if errno not in (0, None):
            raise TeraBoxError(f"share/list error {errno}: {data}")

        return data.get("list", [])

    def get_share_download_link(self, share_url, password=None, index=0):
        """Convenience: resolve a share URL and return the dlink of the
        first file (or the file at `index`) found at the share's root."""
        items = self.resolve_share(share_url, password=password)
        files = [f for f in items if not f.get("isdir")]
        if not files:
            raise TeraBoxError(
                "No files found at the root of this share (it may be a "
                "folder-only share — call resolve_share() and drill into "
                "subfolders with dir_path=... instead)."
            )
        if index >= len(files):
            raise TeraBoxError(f"Index {index} out of range; only {len(files)} files found.")
        dlink = files[index].get("dlink")
        if not dlink:
            raise TeraBoxError(f"No dlink present for file: {files[index].get('server_filename')}")
        return dlink, files[index]

    # ------------------------------------------------------------------ #
    # Upload
    # ------------------------------------------------------------------ #

    UPLOAD_CHUNK_SIZE = 4 * 1024 * 1024  # 4 MiB, TeraBox's standard block size

    def _md5_hex(self, data_bytes):
        import hashlib
        return hashlib.md5(data_bytes).hexdigest()

    def _iter_chunks(self, local_path):
        with open(local_path, "rb") as f:
            while True:
                chunk = f.read(self.UPLOAD_CHUNK_SIZE)
                if not chunk:
                    break
                yield chunk

    def _precreate(self, remote_path, size, block_list):
        data = self._post("/api/precreate", data={
            "path": remote_path,
            "size": size,
            "isdir": 0,
            "autoinit": 1,
            "rtype": 3,  # overwrite-safe: auto-rename on conflict
            "block_list": json.dumps(block_list),
        })
        return data  # contains uploadid, plus which block indices actually need upload

    def _upload_chunk(self, remote_path, uploadid, partseq, chunk_bytes):
        # Upload host is usually a "c-" prefixed subdomain; if the default host
        # rejects it, the -6 redirect logic in _request will correct it.
        upload_host = self.host
        params = {
            "method": "upload",
            "path": remote_path,
            "uploadid": uploadid,
            "partseq": partseq,
        }
        files = {"file": (f"blob_{partseq}", chunk_bytes)}
        return self._post("/rest/2.0/pcs/superfile2", params=params,
                           files=files, host_override=upload_host)

    def _create_finalize(self, remote_path, size, block_list, uploadid):
        data = self._post("/api/create", data={
            "path": remote_path,
            "size": size,
            "isdir": 0,
            "rtype": 3,
            "uploadid": uploadid,
            "block_list": json.dumps(block_list),
        })
        return data

    def upload_file(self, local_path, remote_dir="/", progress=True):
        """Upload a local file to a TeraBox directory.
        Splits into 4MB chunks, computes md5 per chunk, precreates, uploads
        each chunk, then finalizes. Returns the /api/create response."""
        import os

        filename = os.path.basename(local_path)
        remote_path = remote_dir.rstrip("/") + "/" + filename
        size = os.path.getsize(local_path)

        block_list = [self._md5_hex(chunk) for chunk in self._iter_chunks(local_path)]
        if not block_list:
            raise TeraBoxError("File is empty; nothing to upload.")

        pre = self._precreate(remote_path, size, block_list)
        uploadid = pre.get("uploadid")
        if not uploadid:
            raise TeraBoxError(f"precreate did not return an uploadid: {pre}")

        for partseq, chunk in enumerate(self._iter_chunks(local_path)):
            if progress:
                print(f"  uploading part {partseq + 1}/{len(block_list)}...")
            self._upload_chunk(remote_path, uploadid, partseq, chunk)

        result = self._create_finalize(remote_path, size, block_list, uploadid)
        return result


if __name__ == "__main__":
    # Quick smoke test: list root, then download the first file found.
    client = TeraBoxClient("cookies.json")
    info = client.init()
    print("Init result:", info)

    files = client.list_dir("/")
    print(f"Found {len(files)} items in root:")
    for f in files[:10]:
        kind = "DIR" if f.get("isdir") else "FILE"
        size = f.get("size", 0)
        print(f"  [{kind}] {f.get('server_filename')}  ({size} bytes)")

    # Try downloading the first actual file (skip folders).
    first_file = next((f for f in files if not f.get("isdir")), None)
    if first_file:
        print(f"\nResolving download link for: {first_file['server_filename']}")
        link = client.get_download_link(first_file["path"])
        out_name = first_file["server_filename"]
        print(f"Downloading to ./{out_name} ...")
        client.download_file(link, out_name)
        print("Done. Saved:", out_name)
    else:
        print("\nNo files (only folders) at root — nothing to download in this test.")

    # Uncomment to test upload:
    # result = client.upload_file("/path/to/local/file.txt", remote_dir="/")
    # print("Upload result:", result)
