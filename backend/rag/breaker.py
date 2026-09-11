"""소스별 서킷브레이커 — 연속 실패하는 소스를 일정 시간 건너뛴다 (RESEARCH_PLAN 5단계, 작업 19).

ddgs 가 봇 판정으로 막히거나 네이버가 429 를 내기 시작하면, 그 뒤 모든 검색어가 같은 벽에 부딪히면서
조사 시간만 길어진다. 연속 fails 회 실패한 소스는 cooldown 초 동안 아예 호출하지 않고 다음 소스로 넘어간다.

    breaker = Breaker(fails=3, cooldown=300)
    if breaker.is_open("naver"): ...          # 지금은 건너뛴다
    breaker.record(name, ok=True/False)

프로세스 안에서만 사는 상태다(재시작하면 초기화). 소스가 살아나면 첫 성공에서 바로 닫힌다.
"""
from __future__ import annotations

import time


class Breaker:
    def __init__(self, fails: int = 3, cooldown: float = 300.0, now=time.time):
        self.fails = max(1, int(fails))
        self.cooldown = float(cooldown)
        self._now = now
        self._streak: dict[str, int] = {}
        self._open_until: dict[str, float] = {}

    def is_open(self, name: str) -> bool:
        """지금 이 소스를 건너뛰어야 하는지."""
        until = self._open_until.get(name)
        if until is None:
            return False
        if self._now() >= until:
            self._open_until.pop(name, None)      # 쉬는 시간이 끝났다 — 한 번 더 시도해 본다
            self._streak[name] = 0
            return False
        return True

    def record(self, name: str, ok: bool) -> None:
        if ok:
            self._streak[name] = 0
            self._open_until.pop(name, None)
            return
        streak = self._streak.get(name, 0) + 1
        self._streak[name] = streak
        if streak >= self.fails:
            self._open_until[name] = self._now() + self.cooldown

    def state(self) -> dict:
        """로그·진단용 — 지금 열려 있는(쉬는) 소스와 남은 시간."""
        now = self._now()
        return {name: round(until - now, 1) for name, until in self._open_until.items() if until > now}
