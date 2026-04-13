"""
app/core/connectwise.py

Low-level ConnectWise Manage API client.
Handles session creation, authentication, and all HTTP operations
so that services never need to touch requests directly.
"""

from __future__ import annotations

import os
import time
from typing import Any, Dict, List, Optional
from urllib.parse import urljoin

import requests
from requests.adapters import HTTPAdapter
from requests.auth import HTTPBasicAuth
from urllib3.util.retry import Retry

from app.core.state import broadcast


# ── Session factory ───────────────────────────────────────────────────────────

def make_session() -> requests.Session:
    """Return a requests.Session with retry logic and connection pooling."""
    sess = requests.Session()
    retry = Retry(
        total=3,
        backoff_factor=0.8,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=("GET", "POST", "PATCH"),
        raise_on_status=False,
        respect_retry_after_header=True,
    )
    adapter = HTTPAdapter(max_retries=retry, pool_connections=10, pool_maxsize=10)
    sess.mount("https://", adapter)
    sess.mount("http://", adapter)
    return sess


# ── Credentials ───────────────────────────────────────────────────────────────

def build_auth() -> HTTPBasicAuth:
    """Build HTTP basic auth from environment variables."""
    company  = (os.getenv("CWM_COMPANY_ID") or "").strip()
    pub_key  = (os.getenv("CWM_PUBLIC_KEY")  or "").strip()
    priv_key = (os.getenv("CWM_PRIVATE_KEY") or "").strip()
    return HTTPBasicAuth(f"{company}+{pub_key}", priv_key)


def build_headers() -> Dict[str, str]:
    """Build standard request headers, including the optional ClientID."""
    client_id = (os.getenv("CLIENT_ID") or "").strip()
    headers: Dict[str, str] = {
        "Accept":       "application/json",
        "Content-Type": "application/json",
    }
    if client_id:
        headers["ClientID"] = client_id
    return headers


def get_base_url() -> str:
    return (os.getenv("CWM_SITE") or "").rstrip("/")


def check_credentials() -> Optional[str]:
    """
    Return an error message if any required CW credential is missing,
    or None if everything looks good.
    """
    site      = (os.getenv("CWM_SITE")       or "").strip()
    company   = (os.getenv("CWM_COMPANY_ID") or "").strip()
    pub_key   = (os.getenv("CWM_PUBLIC_KEY")  or "").strip()
    priv_key  = (os.getenv("CWM_PRIVATE_KEY") or "").strip()
    if not (site and company and pub_key and priv_key):
        return "Missing ConnectWise credentials. Check the Environment tab."
    return None


# ── Ticket fetching ───────────────────────────────────────────────────────────

def fetch_tickets(
    sess: requests.Session,
    site: str,
    auth: HTTPBasicAuth,
    headers: Dict[str, str],
    *,
    board_id: int,
    statuses: List[str],
    timeout: int,
    page_size: int,
) -> List[Dict[str, Any]]:
    """
    Fetch all open tickets on a board with the given statuses.
    Paginates automatically until the API returns a partial page.
    """
    status_cond = " OR ".join(f'status/name = "{s}"' for s in statuses)
    conditions  = f"board/id = {board_id} AND ({status_cond}) AND closedFlag = false"
    tickets: List[Dict] = []
    page = 1
    while True:
        url = urljoin(site + "/", "service/tickets")
        r = sess.get(
            url,
            auth=auth,
            headers=headers,
            timeout=timeout,
            params={
                "conditions": conditions,
                "orderBy":    "dateEntered asc",
                "pageSize":   page_size,
                "page":       page,
            },
        )
        if not r.ok:
            raise RuntimeError(f"GET tickets → HTTP {r.status_code}: {r.text[:400]}")
        batch = r.json()
        if not isinstance(batch, list):
            break
        tickets.extend(batch)
        if len(batch) < page_size:
            break
        page += 1
        time.sleep(0.05)
    return tickets


# ── Ticket mutation ───────────────────────────────────────────────────────────

def patch_ticket(
    sess: requests.Session,
    site: str,
    auth: HTTPBasicAuth,
    headers: Dict[str, str],
    ticket_id: int,
    ops: List[Dict[str, Any]],
    timeout: int,
) -> None:
    """Apply a JSON-Patch to a ticket. Raises RuntimeError on HTTP error."""
    r = sess.patch(
        urljoin(site + "/", f"service/tickets/{ticket_id}"),
        auth=auth,
        headers=headers,
        json=ops,
        timeout=timeout,
    )
    if not r.ok:
        raise RuntimeError(f"PATCH → HTTP {r.status_code}: {r.text[:400]}")


def post_note(
    sess: requests.Session,
    site: str,
    auth: HTTPBasicAuth,
    headers: Dict[str, str],
    ticket_id: int,
    text: str,
    timeout: int,
) -> None:
    """Add an internal note to a ticket. Raises RuntimeError on HTTP error."""
    body = {
        "text":                   text,
        "detailDescriptionFlag":  False,
        "internalAnalysisFlag":   True,
        "resolutionFlag":         False,
    }
    r = sess.post(
        urljoin(site + "/", f"service/tickets/{ticket_id}/notes"),
        auth=auth,
        headers=headers,
        json=body,
        timeout=timeout,
    )
    if not r.ok:
        raise RuntimeError(f"POST note → HTTP {r.status_code}: {r.text[:400]}")
