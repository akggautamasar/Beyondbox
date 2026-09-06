"""
make_env_cookie.py
Filters a full browser cookie export down to just the TeraBox cookies TeraBoxClient
actually needs, and prints a compact single-line JSON string ready to paste
into Vercel's TERABOX_COOKIES_JSON environment variable.

Usage:
    python3 make_env_cookie.py cookies.json > terabox_cookies_compact.json
    # then paste the contents of terabox_cookies_compact.json into Vercel
"""
import json
import sys

if len(sys.argv) != 2:
    print("Usage: python3 make_env_cookie.py <full_cookies.json>", file=sys.stderr)
    sys.exit(1)

with open(sys.argv[1], "r", encoding="utf-8") as f:
    raw = json.load(f)

filtered = [
    {"domain": c["domain"], "name": c["name"], "value": c["value"], "path": c.get("path", "/")}
    for c in raw
    if "terabox" in c.get("domain", "")
]

if not filtered:
    print("No terabox cookies found in this file.", file=sys.stderr)
    sys.exit(1)

print(json.dumps(filtered, separators=(",", ":")))
print(f"\n{len(filtered)} terabox cookies extracted.", file=sys.stderr)
