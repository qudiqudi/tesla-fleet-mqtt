#!/usr/bin/env python3
"""
Privacy / data-leak scan for this public repo.

Two layers:
  1. Generic patterns that should never be in a public infra repo — private-key blocks,
     JWT/OAuth tokens, Tesla VINs, IP addresses, e-mail addresses.
  2. An optional, repo-specific denylist supplied via the PRIVACY_DENYLIST env var (wired
     to a GitHub Actions secret). Put your own personal strings there — domain, VIN, LAN/
     public IPs, host paths, display name, e-mail — so the literals live in the secret, not
     in the repo, and any accidental commit of them fails the build.

CI logs on a public repo are public, so findings are reported as `path:line: reason` only —
never the matched text.
"""
import os
import re
import subprocess
import sys

ALLOW_IPS = {"0.0.0.0", "127.0.0.1", "255.255.255.255", "1.1.1.1"}
ALLOW_EMAIL_DOMAINS = ("example.com", "example.org", "example.net", "anthropic.com")

PATTERNS = [
    ("private key block", re.compile(r"-----BEGIN (?:[A-Z0-9 ]+ )?PRIVATE KEY-----")),
    ("auth/JWT token", re.compile(r"eyJ[A-Za-z0-9_-]{8,}\.eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]+")),
    ("Tesla VIN", re.compile(r"\b(?:5YJ|7SA|7G2|LRW|XP7)[A-HJ-NPR-Z0-9]{14}\b")),
]
IPV4 = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
EMAIL = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")


def tracked_files():
    out = subprocess.run(["git", "ls-files"], capture_output=True, text=True).stdout.split()
    # don't scan the CI machinery itself (it contains the patterns + would self-match)
    return [f for f in out if not f.startswith(".github/")]


def denylist():
    raw = os.environ.get("PRIVACY_DENYLIST", "")
    return [p.strip().lower() for p in re.split(r"[,\n]", raw) if len(p.strip()) >= 3]


def main():
    deny = denylist()
    findings = 0
    for path in tracked_files():
        try:
            with open(path, encoding="utf-8", errors="ignore") as fh:
                lines = fh.readlines()
        except (OSError, IsADirectoryError):
            continue
        for i, line in enumerate(lines, 1):
            for name, rx in PATTERNS:
                if rx.search(line):
                    print(f"{path}:{i}: {name}"); findings += 1
            for ip in IPV4.findall(line):
                if all(0 <= int(o) <= 255 for o in ip.split(".")) and ip not in ALLOW_IPS:
                    print(f"{path}:{i}: IP address"); findings += 1; break
            for em in EMAIL.findall(line):
                if not em.lower().endswith(ALLOW_EMAIL_DOMAINS):
                    print(f"{path}:{i}: e-mail address"); findings += 1; break
            low = line.lower()
            if any(d in low for d in deny):
                print(f"{path}:{i}: matches private denylist"); findings += 1

    if findings:
        print(f"\nPRIVACY SCAN FAILED: {findings} finding(s) above (content redacted).")
        print("If a finding is a false positive, allowlist it in .github/scripts/privacy_scan.py.")
        sys.exit(1)
    print("privacy scan clean" + (f" ({len(deny)} denylist entries)" if deny else " (no denylist set)"))


if __name__ == "__main__":
    main()
