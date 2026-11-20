"""Скользящее окно в памяти процесса. Для нескольких воркеров/инстансов замените на Redis."""
from __future__ import annotations

import time
from collections import defaultdict, deque


class SlidingWindowLimiter:
    def __init__(self, limit: int, window: float = 60.0) -> None:
        self.limit = limit
        self.window = window
        self._hits: dict[int, deque[float]] = defaultdict(deque)

    def hit(self, key: int) -> float:
        """Регистрирует обращение. 0 — можно; иначе сколько секунд ждать."""
        now = time.monotonic()
        q = self._hits[key]
        while q and now - q[0] >= self.window:
            q.popleft()
        if len(q) >= self.limit:
            return self.window - (now - q[0])
        q.append(now)
        if len(self._hits) > 20000:  # защита от разрастания словаря
            self._purge(now)
        return 0.0

    def _purge(self, now: float) -> None:
        for k in [k for k, q in self._hits.items() if not q or now - q[-1] >= self.window]:
            del self._hits[k]
