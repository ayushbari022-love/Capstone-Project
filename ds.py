"""
Data Structures used by EventRegister
-----------------------------------
Trie            -> instant search suggestions for event titles
Min-Heap        -> "next event" / soonest-k events (heapq)
Queue (deque)   -> FIFO waitlist with automatic promotion (per student OR per team)
Stack           -> undo for cancelled registrations (LIFO)
Hash Map (dict) -> XP scores per student (see app.xp_scores)
Max-Heap top-N  -> leaderboard (heapq.nlargest)
Binary Search   -> "show events from this date" filter (bisect)
"""
import heapq
from bisect import bisect_left
from collections import deque
from datetime import datetime, timedelta


def as_time(t):
    """MySQL TIME columns arrive as timedelta; normalise to datetime.time."""
    if isinstance(t, timedelta):
        return (datetime.min + timedelta(seconds=int(t.total_seconds()))).time()
    return t


def event_datetime(event):
    return datetime.combine(event["event_date"], as_time(event["event_time"]))


# ---------------------------------------------------------------- Trie
class TrieNode:
    __slots__ = ("children", "values")

    def __init__(self):
        self.children = {}
        self.values = set()


class Trie:
    """Prefix tree. Each key can point at one or more original titles."""

    def __init__(self):
        self.root = TrieNode()

    def insert(self, key, value):
        node = self.root
        for ch in key.lower():
            node = node.children.setdefault(ch, TrieNode())
        node.values.add(value)

    def search_prefix(self, prefix, limit=6):
        node = self.root
        for ch in prefix.lower():
            if ch not in node.children:
                return []
            node = node.children[ch]
        found, stack = [], [node]
        while stack and len(found) < limit:
            cur = stack.pop()
            for v in sorted(cur.values):
                if v not in found:
                    found.append(v)
            stack.extend(cur.children.values())
        return found[:limit]


# ---------------------------------------------------------------- Min-Heap
def next_events(events, k=3, now=None):
    """Return the k soonest events using a min-heap keyed on date+time."""
    heap = [(event_datetime(e), e["event_id"], e) for e in events]
    heapq.heapify(heap)                      # O(n)
    out = []
    while heap and len(out) < k:
        when, _, ev = heapq.heappop(heap)    # O(log n)
        if now is None or when >= now:
            out.append(ev)
    return out


# ---------------------------------------------------------------- Queue
class WaitlistQueue:
    """FIFO queue over waiting *units* - each unit is one student (solo
    events) or one team (team events), already grouped by the caller."""

    def __init__(self, items=None):
        self._q = deque(items or [])

    def enqueue(self, item):
        self._q.append(item)

    def dequeue(self):
        return self._q.popleft() if self._q else None

    def is_empty(self):
        return not self._q

    def position_of(self, key, field="student_id"):
        for i, item in enumerate(self._q, start=1):
            if item.get(field) == key:
                return i
        return None


# ---------------------------------------------------------------- Stack
class UndoStack:
    """LIFO stack (bounded) that remembers recently cancelled registrations."""

    def __init__(self, limit=5):
        self._s, self._limit = [], limit

    def push(self, item):
        self._s.append(item)
        if len(self._s) > self._limit:
            self._s.pop(0)

    def pop(self):
        return self._s.pop() if self._s else None

    def peek(self):
        return self._s[-1] if self._s else None


# ---------------------------------------------------------------- Heap top-N
def top_n(scores, n=10):
    """scores: dict {student_id: xp}. Returns the n highest as [(id, xp), ...]."""
    return heapq.nlargest(n, scores.items(), key=lambda kv: kv[1])


# ---------------------------------------------------------------- Binary search
def events_on_or_after(sorted_events, day):
    """sorted_events must be ordered by event_date. O(log n) to find the cut."""
    dates = [e["event_date"] for e in sorted_events]
    return sorted_events[bisect_left(dates, day):]
