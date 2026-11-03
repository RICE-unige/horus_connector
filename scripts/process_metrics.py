"""Interval CPU and RSS for an owned process tree, guarded against PID reuse."""

import os
from pathlib import Path
import threading
import time


class ProcessSampler:
    def __init__(self):
        self.previous = {}
        self.cached = {}
        self.lock = threading.Lock()
        self.ticks = os.sysconf("SC_CLK_TCK")
        self.page = os.sysconf("SC_PAGE_SIZE")
        self.memory = os.sysconf("SC_PHYS_PAGES") * self.page

    def _tree(self, pid):
        pending, result = [pid], {}
        while pending:
            current = pending.pop()
            if current in result:
                continue
            try:
                fields = Path(f"/proc/{current}/stat").read_text().rsplit(") ", 1)[1].split()
                result[current] = (int(fields[19]), int(fields[11]) + int(fields[12]), int(fields[21]) * self.page)
                for path in Path(f"/proc/{current}/task").glob("*/children"):
                    try:
                        pending.extend(int(child) for child in path.read_text().split())
                    except OSError:
                        continue
            except (OSError, ValueError, IndexError):
                continue
        return result

    def sample(self, pid):
        now = time.monotonic()
        tree = self._tree(pid)
        if pid not in tree:
            return {"cpu_percent": None, "rss_bytes": 0, "memory_percent": 0, "processes": 0}
        identity = (pid, tree[pid][0])
        with self.lock:
            previous = self.previous.get(identity)
            if previous and now - previous[0] < .25:
                return dict(self.cached[identity])
            self.previous[identity] = (now, tree)
            if len(self.previous) > 512:
                self.previous = {identity: self.previous[identity]}
                self.cached.clear()
            cpu = None
            if previous and now > previous[0]:
                elapsed_ticks = sum(max(0, values[1] - previous[1][child][1]) for child, values in tree.items()
                                    if child in previous[1] and previous[1][child][0] == values[0])
                cpu = elapsed_ticks / self.ticks / (now - previous[0]) * 100
            rss = sum(value[2] for value in tree.values())
            result = {"cpu_percent": cpu, "rss_bytes": rss, "memory_percent": rss / self.memory * 100,
                      "processes": len(tree), "memory_method": "sum_process_RSS_including_shared_pages"}
            self.cached[identity] = result
            return dict(result)


sampler = ProcessSampler()
