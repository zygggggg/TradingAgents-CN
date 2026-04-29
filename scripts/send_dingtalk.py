#!/usr/bin/env python3
"""Send Markdown messages to a DingTalk custom robot."""

import argparse
import base64
import hashlib
import hmac
import json
import os
import time
import urllib.parse
import urllib.request
from typing import Optional


def signed_webhook(webhook, secret):
    # type: (str, Optional[str]) -> str
    if not secret:
        return webhook
    timestamp = str(round(time.time() * 1000))
    string_to_sign = (timestamp + "\n" + secret).encode("utf-8")
    digest = hmac.new(secret.encode("utf-8"), string_to_sign, hashlib.sha256).digest()
    sign = urllib.parse.quote_plus(base64.b64encode(digest))
    separator = "&" if "?" in webhook else "?"
    return "%s%stimestamp=%s&sign=%s" % (webhook, separator, timestamp, sign)


def send_markdown(webhook, title, text, secret=None, timeout=10):
    # type: (str, str, str, Optional[str], int) -> dict
    url = signed_webhook(webhook, secret)
    payload = {
        "msgtype": "markdown",
        "markdown": {"title": title, "text": text},
    }
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(request, timeout=timeout) as response:
        body = response.read().decode("utf-8", errors="ignore")
    try:
        return json.loads(body)
    except ValueError:
        return {"raw": body}


def main():
    parser = argparse.ArgumentParser(description="Send a DingTalk markdown message.")
    parser.add_argument("--title", default="股票监控提醒")
    parser.add_argument("--text", required=True)
    parser.add_argument("--webhook", default=os.getenv("DINGTALK_WEBHOOK", ""))
    parser.add_argument("--secret", default=os.getenv("DINGTALK_SECRET", ""))
    args = parser.parse_args()
    if not args.webhook:
        raise SystemExit("DINGTALK_WEBHOOK is not configured")
    print(send_markdown(args.webhook, args.title, args.text, args.secret or None))


if __name__ == "__main__":
    main()
