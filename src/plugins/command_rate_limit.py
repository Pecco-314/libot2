from collections import deque


class CommandRateLimiter:
    """按群、规范命令名和用户维护滑动时间窗；每次连续超限只提醒一次。"""

    def __init__(self):
        self.uses: dict[tuple[int, str, int], deque[float]] = {}
        self.warned: set[tuple[int, str, int]] = set()

    def check(self, key, seconds: float, count: int, now: float) -> tuple[bool, bool]:
        uses = self.uses.setdefault(key, deque())
        while uses and uses[0] <= now - seconds:
            uses.popleft()
        if len(uses) >= count:
            notify = key not in self.warned
            self.warned.add(key)
            return False, notify
        uses.append(now)
        self.warned.discard(key)
        return True, False

    def reset(self, group_id: int, command: str) -> None:
        for key in list(self.uses):
            if key[:2] == (group_id, command):
                del self.uses[key]
                self.warned.discard(key)
