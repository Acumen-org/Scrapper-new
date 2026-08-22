"""Rate limited HTTP with a declared contact.

The SEC requires a User-Agent carrying a real contact address and publishes a
limit of roughly ten requests per second. We stay under it, back off on 403,
and never retry silently past the configured ceiling.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Iterator

import requests


class FetchError(RuntimeError):
    pass


class Fetcher:
    def __init__(self, http_cfg: dict):
        self.min_interval = 1.0 / float(http_cfg.get("max_requests_per_second", 8))
        self.timeout = int(http_cfg.get("timeout_seconds", 60))
        self.retries = int(http_cfg.get("retries", 3))
        self.backoff = float(http_cfg.get("backoff_seconds", 5))
        self._last = 0.0
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": http_cfg["user_agent"],
            "Accept-Encoding": "gzip, deflate",
        })

    def _throttle(self) -> None:
        gap = time.monotonic() - self._last
        if gap < self.min_interval:
            time.sleep(self.min_interval - gap)
        self._last = time.monotonic()

    def _request(self, url: str, stream: bool) -> requests.Response:
        last_exc: Exception | None = None
        for attempt in range(1, self.retries + 1):
            self._throttle()
            try:
                resp = self.session.get(url, timeout=self.timeout, stream=stream)
            except requests.RequestException as exc:
                last_exc = exc
                time.sleep(self.backoff * attempt)
                continue
            if resp.status_code == 200:
                return resp
            # 403 from the SEC means rate limiting or a missing contact header.
            # Back off hard rather than hammering.
            if resp.status_code in (403, 429, 503):
                resp.close()
                time.sleep(self.backoff * attempt * 2)
                last_exc = FetchError(f"HTTP {resp.status_code} from {url}")
                continue
            resp.close()
            raise FetchError(f"HTTP {resp.status_code} from {url}")
        raise FetchError(f"giving up on {url} after {self.retries} attempts: {last_exc}")

    def json(self, url: str):
        resp = self._request(url, stream=False)
        try:
            return resp.json()
        finally:
            resp.close()

    def download(self, url: str, dest: Path, chunk: int = 1 << 20) -> int:
        """Stream to disk. Returns bytes written.

        Writes to a .part file and renames on completion so an interrupted run
        never leaves a truncated artefact that looks like a good snapshot.
        """
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_suffix(dest.suffix + ".part")
        resp = self._request(url, stream=True)
        declared = resp.headers.get("Content-Length")
        written = 0
        try:
            with open(tmp, "wb") as fh:
                for block in resp.iter_content(chunk_size=chunk):
                    if block:
                        fh.write(block)
                        written += len(block)
        finally:
            resp.close()
        if declared is not None and int(declared) != written:
            tmp.unlink(missing_ok=True)
            raise FetchError(
                f"truncated download of {url}: expected {declared} bytes, got {written}"
            )
        tmp.replace(dest)
        return written
