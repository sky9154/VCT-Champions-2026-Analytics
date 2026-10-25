import email.utils
import logging
import time
from collections.abc import Callable
from datetime import datetime, timezone
from typing import TypeVar

import httpx

LOGGER = logging.getLogger(__name__)
MAX_RETRY_COUNT = 3
MAX_RETRY_WAIT_SECONDS = 60
RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}
RETRYABLE_EXCEPTIONS = (httpx.TimeoutException, httpx.NetworkError)
T = TypeVar("T")


class RetryLimitExceeded(Exception):
  pass


def _retry_after_seconds(value: str | None) -> float | None:
  if not value:
    return None

  try:
    return max(0, float(value))
  except ValueError:
    try:
      retry_at = email.utils.parsedate_to_datetime(value)
    except (TypeError, ValueError, OverflowError):
      return None

    if retry_at.tzinfo is None:
      retry_at = retry_at.replace(tzinfo=timezone.utc)

    return max(0, (retry_at - datetime.now(timezone.utc)).total_seconds())


def request_with_retry(request: Callable[[], httpx.Response],
                       before_attempt: Callable[[], None],
                       sleep: Callable[[float], None] = time.sleep,
                       event: str = "unknown",
                       match_identity: str = "page") -> httpx.Response:
  last_error: Exception | None = None

  for attempt in range(1, MAX_RETRY_COUNT + 1):
    attempt_started = time.monotonic()
    before_attempt()

    try:
      response = request()
    except RETRYABLE_EXCEPTIONS as error:
      last_error = error

      if attempt == MAX_RETRY_COUNT:
        break

      delay = min(2**(attempt - 1), MAX_RETRY_WAIT_SECONDS)
      LOGGER.warning(
          "provider=liquipedia stage=fetch event=%s match_identity=%s elapsed_seconds=%.3f result=retry retry_count=%s",
          event, match_identity,
          time.monotonic() - attempt_started, attempt)
      sleep(delay)

      continue

    if response.status_code not in RETRYABLE_STATUS_CODES:
      try:
        response.raise_for_status()
      except httpx.HTTPStatusError:
        response.close()

        raise

      return response

    if attempt == MAX_RETRY_COUNT:
      try:
        response.raise_for_status()
      finally:
        response.close()

    retry_after = _retry_after_seconds(response.headers.get("Retry-After"))
    delay = retry_after if response.status_code == 429 and retry_after is not None else min(
        2**(attempt - 1), MAX_RETRY_WAIT_SECONDS)

    if delay > MAX_RETRY_WAIT_SECONDS:
      raise RetryLimitExceeded(
          "Liquipedia Retry-After exceeds the configured maximum wait.")

    LOGGER.warning(
        "provider=liquipedia stage=fetch event=%s match_identity=%s elapsed_seconds=%.3f result=retry retry_count=%s",
        event, match_identity,
        time.monotonic() - attempt_started, attempt)

    response.close()
    sleep(delay)

  raise RetryLimitExceeded(
      "Liquipedia request failed after the maximum retry count."
  ) from last_error
