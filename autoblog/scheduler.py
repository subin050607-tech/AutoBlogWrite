"""발행 스케줄러: 활동 시간대 / 하루 한도 / 최소 간격(+랜덤 지터)을 지킨다."""
from __future__ import annotations

import logging
import random
import time
from datetime import datetime, timedelta

from .db import DB
from . import workflow
from .pipeline import process_topic

log = logging.getLogger("autoblog")


class Scheduler:
    def __init__(self, cfg: dict, db: DB, generator, publisher, rng: random.Random | None = None):
        self.cfg, self.db, self.generator, self.publisher = cfg, db, generator, publisher
        self.rng = rng or random.Random()
        self._next_gap: timedelta | None = None

    def _gap(self) -> timedelta:
        """간격은 한 번 뽑아 고정(폴링마다 다시 뽑으면 지터가 무의미해진다)."""
        if self._next_gap is None:
            s = self.cfg["schedule"]
            minutes = s["min_interval_minutes"] + self.rng.uniform(0, s["jitter_minutes"])
            self._next_gap = timedelta(minutes=max(0, minutes))
        return self._next_gap

    def can_post_now(self, now: datetime) -> tuple[bool, str]:
        s = self.cfg["schedule"]
        start, end = s["active_hours"]
        if not (start <= now.hour < end):
            return False, f"활동 시간대({start}~{end}시) 아님"
        today = now.replace(hour=0, minute=0, second=0, microsecond=0).isoformat(timespec="seconds")
        done_today = len(self.db.posts_since(today))
        if done_today >= s["daily_limit"]:
            return False, f"오늘 한도 도달({done_today}/{s['daily_limit']})"
        last = self.db.last_post_time()
        if last and now - last < self._gap():
            return False, f"최소 간격 대기 중(마지막 발행 {last:%H:%M})"
        return True, "ok"

    def tick(self, now: datetime | None = None) -> bool:
        """한 번 점검하고 발행 조건이면 다음 주제 1건을 처리. 발행했으면 True."""
        now = now or datetime.now()
        # 사용자가 시각을 지정해 예약한 글은 활동시간/한도 규칙과 무관하게 그 시각에 발행
        if workflow.publish_due(self.db, self.cfg, lambda: self.publisher, now):
            return True
        ok, why = self.can_post_now(now)
        if not ok:
            log.debug("대기: %s", why)
            return False
        topic = self.db.next_topic()
        if not topic:
            log.info("대기 중인 주제가 없습니다.")
            return False
        try:
            process_topic(topic, self.cfg, self.db, self.generator, self.publisher)
        except Exception as e:
            log.error("주제 '%s' 실패: %s", topic["keyword"], e)
            return False
        self._next_gap = None  # 다음 간격 새로 추첨
        return True

    def run_forever(self) -> None:
        poll = self.cfg["schedule"]["poll_seconds"]
        log.info("스케줄러 시작 (Ctrl+C 로 종료)")
        while True:
            try:
                self.tick()
            except KeyboardInterrupt:
                raise
            except Exception:
                log.exception("tick 오류")
            time.sleep(poll)
