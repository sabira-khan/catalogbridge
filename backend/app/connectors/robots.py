from __future__ import annotations

from urllib.parse import urlparse
from urllib.robotparser import RobotFileParser

import httpx

from app.logging import get_logger, log_event

ROBOTS_PATH = "/robots.txt"
logger = get_logger(__name__)


class RobotsPolicy:
    def __init__(self, base_url: str, user_agent: str) -> None:
        self.base_url = base_url
        self.user_agent = user_agent
        self._parser: RobotFileParser | None = None
        self._loaded = False
        self._crawl_delay: float | None = None

    async def load(self, client: httpx.AsyncClient) -> None:
        if self._loaded:
            return
        self._loaded = True
        url = f"{self.base_url}{ROBOTS_PATH}"
        try:
            response = await client.get(url)
        except httpx.HTTPError:
            log_event(logger, "robots.txt unreachable, assuming allowed", url=url)
            return
        if response.status_code != httpx.codes.OK:
            log_event(logger, "no robots.txt, assuming allowed", url=url)
            return
        parser = RobotFileParser()
        parser.parse(response.text.splitlines())
        self._parser = parser
        delay = parser.crawl_delay(self.user_agent)
        if delay is not None:
            self._crawl_delay = float(delay)
            log_event(logger, "robots.txt sets a crawl delay", seconds=self._crawl_delay)

    def allows(self, url: str) -> bool:
        if self._parser is None:
            return True
        path = urlparse(url).path or "/"
        return self._parser.can_fetch(self.user_agent, path)

    @property
    def crawl_delay(self) -> float | None:
        return self._crawl_delay
