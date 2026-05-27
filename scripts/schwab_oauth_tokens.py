#!/usr/bin/env python3
"""
Schwab OAuth token helper for NandaEdge Advisor.

This helper never asks for Schwab/thinkorswim username or password. You log in
only on Schwab's OAuth page, then paste the returned authorization code or full
callback URL here to exchange it for tokens.
"""
from __future__ import annotations

import argparse
import base64
import gzip
import json
import os
import sys
import urllib.parse
import urllib.request
from pathlib import Path


AUTH_URL = "https://api.schwabapi.com/v1/oauth/authorize"
TOKEN_URL = "https://api.schwabapi.com/v1/oauth/token"


def load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, val = line.split("=", 1)
        key = key.strip()
        val = val.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = val


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate Schwab OAuth URL and exchange auth codes for tokens.")
    parser.add_argument("--env", default=".env", help="Path to local .env file with SCHWAB_APP_KEY/SECRET/CALLBACK_URL.")
    parser.add_argument("--auth-url", action="store_true", help="Print the Schwab OAuth login URL.")
    parser.add_argument("--code", default="", help="Authorization code returned by Schwab.")
    parser.add_argument("--callback-url", default="", help="Full callback URL returned by Schwab; code will be extracted.")
    parser.add_argument("--refresh", action="store_true", help="Refresh access token using SCHWAB_REFRESH_TOKEN.")
    parser.add_argument("--write-env", action="store_true", help="Update SCHWAB_ACCESS_TOKEN and SCHWAB_REFRESH_TOKEN in .env.")
    return parser.parse_args()


def required(name: str) -> str:
    value = (os.environ.get(name, "") or "").strip()
    if not value:
        raise SystemExit(f"Missing {name}. Add it to .env or environment first.")
    return value


def auth_header(app_key: str, app_secret: str) -> str:
    raw = f"{app_key}:{app_secret}".encode("utf-8")
    return "Basic " + base64.b64encode(raw).decode("ascii")


def build_auth_url(app_key: str, callback_url: str) -> str:
    params = {
        "response_type": "code",
        "client_id": app_key,
        "redirect_uri": callback_url,
    }
    return AUTH_URL + "?" + urllib.parse.urlencode(params)


def code_from_callback(callback_url: str) -> str:
    parsed = urllib.parse.urlparse(callback_url)
    qs = urllib.parse.parse_qs(parsed.query)
    code = (qs.get("code") or [""])[0]
    return urllib.parse.unquote(code)


def redirect_uri_from_callback(callback_url: str) -> str:
    parsed = urllib.parse.urlparse(callback_url)
    if not parsed.scheme or not parsed.netloc:
        return ""
    return urllib.parse.urlunparse((parsed.scheme, parsed.netloc, parsed.path, "", "", ""))


def decode_error_body(err: urllib.error.HTTPError) -> str:
    raw = err.read()
    encoding = (err.headers.get("Content-Encoding", "") or "").lower()
    if encoding == "gzip" or raw[:2] == b"\x1f\x8b":
        try:
            raw = gzip.decompress(raw)
        except Exception:
            pass
    return raw.decode("utf-8", errors="replace")


def post_token(app_key: str, app_secret: str, data: dict) -> dict:
    body = urllib.parse.urlencode(data).encode("utf-8")
    req = urllib.request.Request(
        TOKEN_URL,
        data=body,
        headers={
            "Authorization": auth_header(app_key, app_secret),
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
            "Accept-Encoding": "identity",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        detail = decode_error_body(e)
        raise SystemExit(f"Schwab token request failed: HTTP {e.code}\n{detail}")


def update_env_tokens(path: Path, token_payload: dict) -> None:
    access = token_payload.get("access_token", "")
    refresh = token_payload.get("refresh_token", "")
    if not access:
        raise SystemExit("No access_token in Schwab response; not updating .env.")
    existing = path.read_text(encoding="utf-8", errors="replace").splitlines() if path.exists() else []
    values = {"SCHWAB_ACCESS_TOKEN": access}
    if refresh:
        values["SCHWAB_REFRESH_TOKEN"] = refresh
    seen = set()
    out = []
    for line in existing:
        key = line.split("=", 1)[0].strip() if "=" in line else ""
        if key in values:
            out.append(f"{key}={values[key]}")
            seen.add(key)
        else:
            out.append(line)
    for key, val in values.items():
        if key not in seen:
            out.append(f"{key}={val}")
    path.write_text("\n".join(out).rstrip() + "\n", encoding="utf-8")


def print_env_lines(token_payload: dict) -> None:
    print("\nAdd/update these in your local .env:\n")
    print(f"SCHWAB_ACCESS_TOKEN={token_payload.get('access_token', '')}")
    if token_payload.get("refresh_token"):
        print(f"SCHWAB_REFRESH_TOKEN={token_payload.get('refresh_token', '')}")
    print("\nDo not commit .env.\n")


def main() -> int:
    args = parse_args()
    env_path = Path(args.env).expanduser()
    load_dotenv(env_path)

    app_key = required("SCHWAB_APP_KEY")
    app_secret = required("SCHWAB_APP_SECRET")
    callback_url = required("SCHWAB_CALLBACK_URL")

    if args.auth_url:
        print(build_auth_url(app_key, callback_url))
        return 0

    token_payload = None
    if args.refresh:
        refresh_token = required("SCHWAB_REFRESH_TOKEN")
        token_payload = post_token(app_key, app_secret, {
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
        })
    else:
        code = args.code.strip()
        exchange_redirect_uri = callback_url
        if args.callback_url:
            code = code_from_callback(args.callback_url)
            exchange_redirect_uri = redirect_uri_from_callback(args.callback_url) or callback_url
        if not code:
            raise SystemExit("Provide --auth-url, --code, --callback-url, or --refresh.")
        token_payload = post_token(app_key, app_secret, {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": exchange_redirect_uri,
        })

    print(json.dumps({k: v for k, v in token_payload.items() if k not in {"access_token", "refresh_token", "id_token"}}, indent=2))
    print_env_lines(token_payload)
    if args.write_env:
        update_env_tokens(env_path, token_payload)
        print(f"Updated {env_path} with Schwab token values.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
