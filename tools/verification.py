"""
tools/verification.py
---------------------
Real-lead verification utilities. These turn "the LLM said so" into
"we actually checked". Every check degrades gracefully if a dependency
or network call is unavailable.

Checks:
  - Company domain is live (DNS resolves + HTTP reachable)
  - Email is syntactically valid AND its domain has MX records
  - LinkedIn URL is a well-formed public profile URL
  - Optional: SMTP mailbox probe (off by default — slow / often blocked)
"""

from __future__ import annotations
import re
import socket
import ssl
import smtplib
from functools import lru_cache
from urllib.parse import urlparse

import httpx

try:
    import dns.resolver  # dnspython
    DNS_AVAILABLE = True
except Exception:
    DNS_AVAILABLE = False

from config.settings import settings
from utils.helpers import log_agent


EMAIL_RE = re.compile(r"^[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}$")
LINKEDIN_RE = re.compile(
    r"^https?://([a-z]{2,3}\.)?linkedin\.com/(in|company)/[A-Za-z0-9\-_%]+/?",
    re.IGNORECASE,
)
_PLACEHOLDER_DOMAINS = {
    "example.com", "example.org", "company.com", "domain.com",
    "email.com", "test.com", "acme.com", "yourcompany.com",
}


def normalize_url(url: str) -> str:
    """Ensure a URL has a scheme."""
    url = (url or "").strip()
    if not url:
        return ""
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    return url


def domain_from_url(url: str) -> str:
    url = normalize_url(url)
    try:
        netloc = urlparse(url).netloc.lower()
        return netloc[4:] if netloc.startswith("www.") else netloc
    except Exception:
        return ""


def domain_from_email(email: str) -> str:
    if "@" not in (email or ""):
        return ""
    return email.split("@", 1)[1].strip().lower()


def is_valid_linkedin(url: str) -> bool:
    return bool(LINKEDIN_RE.match((url or "").strip()))


def is_valid_email_syntax(email: str) -> bool:
    email = (email or "").strip()
    if not EMAIL_RE.match(email):
        return False
    domain = domain_from_email(email)
    return domain not in _PLACEHOLDER_DOMAINS


@lru_cache(maxsize=512)
def has_mx_record(domain: str) -> bool:
    """Return True if the domain can receive email (MX or A record)."""
    domain = (domain or "").strip().lower()
    if not domain or domain in _PLACEHOLDER_DOMAINS:
        return False

    if DNS_AVAILABLE:
        try:
            answers = dns.resolver.resolve(domain, "MX", lifetime=5.0)
            if len(answers) > 0:
                return True
        except Exception:
            pass
        # Fall back to A record — some domains accept mail without MX
        try:
            dns.resolver.resolve(domain, "A", lifetime=5.0)
            return True
        except Exception:
            return False

    # No dnspython — fall back to a basic hostname resolution
    try:
        socket.gethostbyname(domain)
        return True
    except Exception:
        return False


@lru_cache(maxsize=512)
def is_domain_live(url_or_domain: str) -> bool:
    """Check whether a company website is reachable over HTTP(S)."""
    url = normalize_url(url_or_domain)
    domain = domain_from_url(url)
    if not domain or domain in _PLACEHOLDER_DOMAINS:
        return False

    # DNS resolution first (cheap)
    try:
        socket.gethostbyname(domain)
    except Exception:
        return False

    headers = {"User-Agent": "Mozilla/5.0 (compatible; OutreachAgent/3.0)"}
    for method in ("head", "get"):
        try:
            with httpx.Client(follow_redirects=True, timeout=8.0, verify=False) as client:
                resp = getattr(client, method)(url, headers=headers)
                if resp.status_code < 500:
                    return True
        except Exception:
            continue
    return False


def smtp_probe(email: str) -> bool:
    """
    Best-effort SMTP RCPT check. Many providers block this, so it is
    OFF by default and only a positive signal (never used to reject).
    """
    domain = domain_from_email(email)
    if not domain or not DNS_AVAILABLE:
        return False
    try:
        records = dns.resolver.resolve(domain, "MX", lifetime=5.0)
        mx_host = str(sorted(records, key=lambda r: r.preference)[0].exchange).rstrip(".")
        server = smtplib.SMTP(timeout=8)
        server.connect(mx_host)
        server.helo("outreach-agent.local")
        server.mail("verify@outreach-agent.local")
        code, _ = server.rcpt(email)
        server.quit()
        return code in (250, 251)
    except Exception:
        return False


def verify_lead(lead: dict) -> dict:
    """
    Run all checks on a single lead and return a verification dict:
      { status, confidence, domain_live, email_valid, email_source,
        linkedin_valid, checks[] }
    """
    checks: list[str] = []
    website = lead.get("company_website", "")
    email = lead.get("email", "")
    linkedin = lead.get("linkedin_url", "")

    # ── LinkedIn ─────────────────────────────────────────────────────────────
    linkedin_valid = is_valid_linkedin(linkedin)
    checks.append(("✓ " if linkedin_valid else "✗ ") + "LinkedIn URL format")

    # ── Company domain live ──────────────────────────────────────────────────
    domain_live = is_domain_live(website) if website else False
    checks.append(("✓ " if domain_live else "✗ ") + "Company website reachable")

    # ── Email ────────────────────────────────────────────────────────────────
    email_syntax = is_valid_email_syntax(email)
    email_domain = domain_from_email(email)
    email_mx = has_mx_record(email_domain) if email_syntax else False
    email_valid = email_syntax and email_mx
    if email:
        checks.append(("✓ " if email_valid else "✗ ") + "Email deliverable (syntax + MX)")

    if email_valid and settings.smtp_probe:
        if smtp_probe(email):
            checks.append("✓ SMTP mailbox accepted")

    # ── Confidence + status ──────────────────────────────────────────────────
    confidence = 0
    if linkedin_valid:
        confidence += 40
    if domain_live:
        confidence += 35
    if email_valid:
        confidence += 25

    if confidence >= 65:
        status = "verified"
    elif confidence >= 35:
        status = "partial"
    else:
        status = "unverified"

    return {
        "status": status,
        "confidence": confidence,
        "domain_live": domain_live,
        "email_valid": email_valid,
        "email_source": lead.get("email_source", "provided" if email else "none"),
        "linkedin_valid": linkedin_valid,
        "checks": checks,
    }


def guess_email_patterns(first_name: str, last_name: str, domain: str) -> list[str]:
    """Generate common corporate email patterns for a real domain."""
    f = re.sub(r"[^a-z]", "", (first_name or "").lower())
    l = re.sub(r"[^a-z]", "", (last_name or "").lower())
    domain = (domain or "").lower()
    if not f or not domain:
        return []
    patterns = [f"{f}@{domain}"]
    if l:
        patterns += [
            f"{f}.{l}@{domain}",
            f"{f}{l}@{domain}",
            f"{f[0]}{l}@{domain}",
            f"{f}_{l}@{domain}",
        ]
    return patterns
