from dataclasses import dataclass
from typing import Optional

import requests


@dataclass
class ServiceProbeResult:
    name: str
    url: str
    ok: bool
    status_code: Optional[int] = None
    detail: Optional[str] = None


def probe_http_service(
    name: str,
    url: str,
    path: str,
    timeout_seconds: float = 3.0,
    verify_ssl: bool = True,
    headers: Optional[dict] = None,
) -> ServiceProbeResult:
    target_url = f"{url.rstrip('/')}/{path.lstrip('/')}"
    try:
        response = requests.get(target_url, timeout=timeout_seconds, verify=verify_ssl, headers=headers)
        ok = 200 <= response.status_code < 300
        return ServiceProbeResult(
            name=name,
            url=target_url,
            ok=ok,
            status_code=response.status_code,
            detail=None if ok else response.text[:200],
        )
    except requests.RequestException as exc:
        return ServiceProbeResult(name=name, url=target_url, ok=False, detail=str(exc))
