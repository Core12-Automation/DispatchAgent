# DEPRECATED — kept in place so agent_runtime.py imports continue to work.
# All new code should use:
#
#     from src.clients.connectwise import CWManageClient, CWConfig
#
# The canonical client in src/clients/connectwise.py merges this module with
# app/core/connectwise.py into a single, unified class.

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Any, Dict, Optional
from urllib.parse import urljoin

import requests
from dotenv import find_dotenv, load_dotenv
from requests.adapters import HTTPAdapter
from requests.auth import HTTPBasicAuth
from urllib3.util.retry import Retry


load_dotenv(find_dotenv())


@dataclass(slots=True)
class CWConfig:
    site: str
    company_id: str
    public_key: str
    private_key: str
    client_id: Optional[str] = None
    request_timeout: int = 20
    retry_total: int = 3

    @classmethod
    def from_env(cls) -> "CWConfig":
        return cls(
            site=os.getenv("CWM_SITE", ""),
            company_id=os.getenv("CWM_COMPANY_ID", ""),
            public_key=os.getenv("CWM_PUBLIC_KEY", ""),
            private_key=os.getenv("CWM_PRIVATE_KEY", ""),
            client_id=os.getenv("CLIENT_ID") or None,
            request_timeout=int(os.getenv("CWM_REQUEST_TIMEOUT", "20")),
            retry_total=int(os.getenv("CWM_RETRY_TOTAL", "3")),
        )

    def validate(self) -> None:
        missing = [
            name
            for name, value in {
                "CWM_SITE": self.site,
                "CWM_COMPANY_ID": self.company_id,
                "CWM_PUBLIC_KEY": self.public_key,
                "CWM_PRIVATE_KEY": self.private_key,
            }.items()
            if not value
        ]
        if missing:
            raise RuntimeError(f"Missing required ConnectWise environment variables: {', '.join(missing)}")


class CWManageClient:
    def __init__(self, config: Optional[CWConfig] = None) -> None:
        self.config = config or CWConfig.from_env()
        self.config.validate()
        self.session = self._create_session()

    @property
    def headers(self) -> Dict[str, str]:
        headers = {"Accept": "application/json", "Content-Type": "application/json"}
        if self.config.client_id:
            headers["ClientID"] = self.config.client_id
        return headers

    def _create_session(self) -> requests.Session:
        session = requests.Session()
        session.auth = HTTPBasicAuth(
            f"{self.config.company_id}+{self.config.public_key}",
            self.config.private_key,
        )
        session.headers.update(self.headers)
        retry = Retry(
            total=self.config.retry_total,
            connect=self.config.retry_total,
            read=self.config.retry_total,
            status=self.config.retry_total,
            backoff_factor=0.8,
            status_forcelist=(429, 500, 502, 503, 504),
            allowed_methods=("GET", "POST", "PUT", "DELETE", "PATCH"),
            raise_on_status=False,
            respect_retry_after_header=True,
        )
        adapter = HTTPAdapter(max_retries=retry, pool_connections=10, pool_maxsize=10)
        session.mount("https://", adapter)
        session.mount("http://", adapter)
        return session

    def api_url(self, path: str) -> str:
        return urljoin(self.config.site.rstrip("/") + "/", str(path).lstrip("/"))

    def request(self, method: str, path: str, **kwargs: Any) -> requests.Response:
        url = self.api_url(path)
        response = self.session.request(method, url, timeout=self.config.request_timeout, **kwargs)
        if response.status_code >= 400:
            preview = (response.text or "")[:4000]
            raise requests.HTTPError(
                f"[HTTP {response.status_code}] {method} {url}\n{preview}",
                response=response,
            )
        return response

    def get(self, path: str, **kwargs: Any) -> Any:
        response = self.request("GET", path, **kwargs)
        return response.json() if response.text.strip() else None

    def post(self, path: str, **kwargs: Any) -> Any:
        response = self.request("POST", path, **kwargs)
        return response.json() if response.text.strip() else None

    def patch(self, path: str, ops: list[dict[str, Any]]) -> Any:
        response = self.request(
            "PATCH",
            path,
            json=ops,
            headers={**self.headers, "Content-Type": "application/json"},
        )
        return response.json() if response.text.strip() else None

    def get_ticket(self, ticket_id: int) -> dict[str, Any]:
        return self.get(f"service/tickets/{int(ticket_id)}")

    def list_tickets(self, *, conditions: Optional[str] = None, order_by: Optional[str] = None, page_size: int = 100) -> list[dict[str, Any]]:
        params: Dict[str, Any] = {"pageSize": page_size}
        if conditions:
            params["conditions"] = conditions
        if order_by:
            params["orderBy"] = order_by
        return self.get("service/tickets", params=params) or []

    def patch_ticket(self, ticket_id: int, ops: list[dict[str, Any]]) -> Any:
        if not ops:
            return None
        return self.patch(f"service/tickets/{int(ticket_id)}", ops)

    def add_ticket_note(
        self,
        ticket_id: int,
        text: str,
        *,
        internal_analysis_flag: bool = True,
        detail_description_flag: bool = False,
        resolution_flag: bool = False,
        process_notifications: bool | None = None,
    ) -> Any:
        payload = {
            "text": text,
            "detailDescriptionFlag": detail_description_flag,
            "internalAnalysisFlag": internal_analysis_flag,
            "resolutionFlag": resolution_flag,
        }
        if process_notifications is not None:
            payload["processNotifications"] = bool(process_notifications)
        return self.post(f"service/tickets/{int(ticket_id)}/notes", json=payload)

    def fetch_absolute_url(self, href: str) -> Any:
        response = self.session.get(href, timeout=self.config.request_timeout)
        if response.status_code >= 400:
            preview = (response.text or "")[:4000]
            raise requests.HTTPError(f"[HTTP {response.status_code}] GET {href}\n{preview}", response=response)
        return response.json() if response.text.strip() else None

    def dump_json(self, payload: Any) -> str:
        return json.dumps(payload, indent=2, ensure_ascii=False)
