#!/usr/bin/env python3
"""
api_client.py — a defensive REST API client.

Wraps `requests` with the behavior a real integration needs and a
tutorial snippet never bothers with: bounded retries with exponential
backoff, honoring `Retry-After` on 429, distinguishing "the server is
having a bad day" from "your request is wrong", and never letting a
malformed JSON body turn into an unhandled exception deep in caller code.

Usage:
    from api_client import APIClient

    client = APIClient("https://api.example.com", timeout=5, max_retries=3)
    data = client.get("/users/1")

CLI:
    ./api_client.py https://api.example.com/users/1
    ./api_client.py https://api.example.com/users/1 --retries 5 --timeout 3
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import dataclass
from typing import Any, Optional

import requests


class APIError(Exception):
    """Base class for all client-raised errors. Callers can catch this alone."""


class APIConnectionError(APIError):
    """Raised when the server could not be reached at all (DNS, refused, offline)."""


class APITimeoutError(APIError):
    """Raised when a request did not complete within the configured timeout."""


class APIDecodeError(APIError):
    """Raised when a response claims JSON but the body doesn't parse as JSON."""

    def __init__(self, message: str, raw_body: str):
        super().__init__(message)
        self.raw_body = raw_body


class ClientError(APIError):
    """4xx other than 429 — the request itself is wrong. Retrying won't help."""

    def __init__(self, status_code: int, message: str):
        super().__init__(message)
        self.status_code = status_code


class ServerError(APIError):
    """5xx that persisted after all retries were exhausted."""

    def __init__(self, status_code: int, message: str):
        super().__init__(message)
        self.status_code = status_code


class RateLimitExceeded(APIError):
    """429 that persisted after all retries were exhausted."""

    def __init__(self, retry_after: Optional[float]):
        self.retry_after = retry_after
        super().__init__(f"rate limit exceeded, retry_after={retry_after}")


@dataclass
class RetryPolicy:
    max_retries: int = 3
    base_delay: float = 0.5
    max_delay: float = 8.0

    def delay_for_attempt(self, attempt: int) -> float:
        """Exponential backoff: base_delay * 2^attempt, capped at max_delay."""
        return min(self.base_delay * (2 ** attempt), self.max_delay)


class APIClient:
    """
    A small, defensive HTTP JSON client.

    Retries on connection errors, timeouts, 429, and 5xx. Does NOT retry
    on other 4xx — a 404 or 400 won't fix itself by trying again.
    """

    RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}

    def __init__(
        self,
        base_url: str,
        timeout: float = 10.0,
        max_retries: int = 3,
        session: Optional[requests.Session] = None,
        sleep_fn=time.sleep,
    ):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.retry_policy = RetryPolicy(max_retries=max_retries)
        self.session = session or requests.Session()
        self._sleep = sleep_fn  # injectable for fast tests

    def get(self, path: str, params: Optional[dict] = None) -> Any:
        return self._request("GET", path, params=params)

    def post(self, path: str, json_body: Optional[dict] = None) -> Any:
        return self._request("POST", path, json_body=json_body)

    def _full_url(self, path: str) -> str:
        return f"{self.base_url}/{path.lstrip('/')}"

    def _request(
        self,
        method: str,
        path: str,
        params: Optional[dict] = None,
        json_body: Optional[dict] = None,
    ) -> Any:
        url = self._full_url(path)
        last_exception: Optional[Exception] = None

        for attempt in range(self.retry_policy.max_retries + 1):
            try:
                response = self.session.request(
                    method,
                    url,
                    params=params,
                    json=json_body,
                    timeout=self.timeout,
                )
            except requests.exceptions.Timeout as e:
                last_exception = APITimeoutError(f"request to {url} timed out: {e}")
            except requests.exceptions.ConnectionError as e:
                last_exception = APIConnectionError(f"could not reach {url}: {e}")
            else:
                result = self._handle_response(response, attempt)
                if result is not _RETRY_SENTINEL:
                    return result
                last_exception = None  # handled retry already slept
                continue

            if attempt < self.retry_policy.max_retries:
                self._sleep(self.retry_policy.delay_for_attempt(attempt))
                continue
            raise last_exception

        # Should be unreachable, but never let the loop fall through silently.
        raise last_exception or APIError("request failed for an unknown reason")

    def _handle_response(self, response: requests.Response, attempt: int):
        status = response.status_code

        if status == 429:
            retry_after = self._parse_retry_after(response)
            if attempt < self.retry_policy.max_retries:
                self._sleep(retry_after or self.retry_policy.delay_for_attempt(attempt))
                return _RETRY_SENTINEL
            raise RateLimitExceeded(retry_after)

        if status in (500, 502, 503, 504):
            if attempt < self.retry_policy.max_retries:
                self._sleep(self.retry_policy.delay_for_attempt(attempt))
                return _RETRY_SENTINEL
            raise ServerError(status, f"server error {status} after retries: {response.text[:200]}")

        if 400 <= status < 500:
            raise ClientError(status, f"client error {status}: {response.text[:200]}")

        return self._parse_json(response)

    @staticmethod
    def _parse_retry_after(response: requests.Response) -> Optional[float]:
        header = response.headers.get("Retry-After")
        if header is None:
            return None
        try:
            return float(header)
        except ValueError:
            return None

    @staticmethod
    def _parse_json(response: requests.Response) -> Any:
        if not response.content:
            return None
        try:
            return response.json()
        except (json.JSONDecodeError, ValueError) as e:
            raise APIDecodeError(
                f"response was not valid JSON: {e}", raw_body=response.text[:500]
            )


_RETRY_SENTINEL = object()


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Defensive REST API GET client")
    parser.add_argument("url", help="Full URL to GET")
    parser.add_argument("--timeout", type=float, default=10.0)
    parser.add_argument("--retries", type=int, default=3)
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    args = build_arg_parser().parse_args(argv)
    # Split into base_url + path so APIClient's URL joining is exercised
    # the same way it would be from library code.
    if "/" in args.url.split("://", 1)[-1]:
        scheme_and_host, _, rest = args.url.partition("://")
        host, _, path = rest.partition("/")
        base_url = f"{scheme_and_host}://{host}"
        path = "/" + path
    else:
        base_url, path = args.url, "/"

    client = APIClient(base_url, timeout=args.timeout, max_retries=args.retries)
    try:
        data = client.get(path)
    except RateLimitExceeded as e:
        print(f"error: rate limited, retry after {e.retry_after}s", file=sys.stderr)
        return 1
    except ClientError as e:
        print(f"error: client error {e.status_code}", file=sys.stderr)
        return 1
    except ServerError as e:
        print(f"error: server error {e.status_code} (retries exhausted)", file=sys.stderr)
        return 1
    except APITimeoutError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    except APIConnectionError as e:
        print(f"error: {e} (offline or host unreachable)", file=sys.stderr)
        return 1
    except APIDecodeError as e:
        print(f"error: {e}\nraw body: {e.raw_body[:200]}", file=sys.stderr)
        return 1

    print(json.dumps(data, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
