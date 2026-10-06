#!/usr/bin/env python3
"""Week 3 · Task 3 — Beat the baseline cache.

Textbook §2.4.2 (caching) and §2.4.3 (TTL).

`BaselineCache` below works. It is also bad, in more than one way, and one of
its problems is worse than being slow. Find them, write `YourCache`, and prove
the improvement with the harness:

    python3 bench.py                 # baseline only
    python3 bench.py --yours         # baseline vs. yours, side by side

Rules
-----
* Do not change `bench.py`. If you need to change it to win, you are not
  winning. Say so in observation.md instead.
* `YourCache` must expose the same two methods as `BaselineCache`.
* Speed is not the only score. The harness also counts **stale answers** -
  times you served a record whose TTL had already run out. A cache that keeps
  everything forever is very fast and completely wrong.

Targets
-------
The baseline scores **325 upstream queries, 67.5% hit rate, 266 stale answers**.

  pass  : zero stale answers
  good  : zero stale, and no more upstream queries than the baseline
  strong: the above, plus you can say in observation.md **how few upstream
          queries a correct cache could possibly make on this workload, and
          why you cannot go below that number**

That last one is the real question. Read it before you start optimising -
it will tell you where to stop.
"""
import time


class BaselineCache:
    """A DNS cache that somebody wrote in a hurry.

    It caches. It is not correct, and it is not fast. Both are your problem.
    """

    FIXED_LIFETIME = 60          # seconds we keep anything, regardless of TTL

    def __init__(self, upstream):
        self.upstream = upstream  # upstream(name) -> (address, ttl)
        self.entries = []         # list of [name, address, stored_at]

    def lookup(self, name, now):
        """Return an address for `name`, asking upstream only if we have to."""
        for entry in self.entries:                      # linear scan
            if entry[0] == name:
                if now - entry[2] < self.FIXED_LIFETIME:
                    return entry[1]
                self.entries.remove(entry)
                break
        address, ttl = self.upstream(name)
        self.entries.append([name, address, now])
        return address

    def stats(self):
        return {"entries": len(self.entries)}


class YourCache:
    """Your cache.

    Same interface: __init__(upstream), lookup(name, now) -> address, stats().
    `upstream(name)` costs a network round trip and returns (address, ttl).
    The TTL is in seconds and it is the authoritative answer's own TTL -
    the baseline throws it away.
    """

    def __init__(self, upstream):
        self.upstream = upstream
        self.entries = {}         # name -> (address, expires_at)  (O(1) lookup)
        self.hits = 0
        self.misses = 0

    def lookup(self, name, now):
        entry = self.entries.get(name)
        # Serve only while the record's OWN ttl has not run out.
        if entry is not None and now < entry[1]:
            self.hits += 1
            return entry[0]
        # Missing or expired: this is the only time we go upstream.
        address, ttl = self.upstream(name)
        self.entries[name] = (address, now + ttl)
        self.misses += 1
        return address

    def stats(self):
        return {"entries": len(self.entries),
                "hits": self.hits, "misses": self.misses}
