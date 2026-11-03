"""Bounded decoded-frame handoff; never block a GStreamer streaming callback."""

from collections import OrderedDict
import logging
import threading
import time


class FrameDelivery:
    def __init__(self, publisher, metadata, *, capacity=3, wait_sec=.08):
        self.publisher = publisher
        self.metadata = metadata
        self.capacity = max(1, capacity)
        self.wait_sec = max(0, wait_sec)
        self.pending = OrderedDict()
        self.condition = threading.Condition()
        self.epoch = 0
        self.publisher.active_delivery_epoch = 0
        self.closed = False
        self.replaced = self.expired = self.failures = self.rejected = 0
        self.thread = threading.Thread(target=self._run, name="camera-ros-delivery", daemon=True)
        self.thread.start()

    def submit(self, sample, epoch=None):
        with self.condition:
            if self.closed or epoch is not None and epoch != self.epoch:
                return False
            pts = int(sample.get_buffer().pts)
            self.pending[pts] = (time.monotonic() + self.wait_sec, sample, self.epoch)
            while len(self.pending) > self.capacity:
                self.pending.popitem(last=False)
                self.replaced += 1
            self.condition.notify()
            return True

    def wake(self):
        with self.condition:
            self.condition.notify()

    def reset(self):
        with self.condition:
            self.epoch += 1
            self.publisher.active_delivery_epoch = self.epoch
            self.pending.clear()
            self.condition.notify()

    def close(self):
        with self.condition:
            self.closed = True
            self.pending.clear()
            self.condition.notify()
        self.thread.join(timeout=2)
        if self.thread.is_alive():
            logging.getLogger(__name__).warning("ROS camera worker still stopping; cleanup deferred to worker")
        return not self.thread.is_alive()

    def _run(self):
        try:
            while True:
                with self.condition:
                    if self.closed:
                        return
                    if not self.pending:
                        self.condition.wait(.1)
                        continue
                    pts, (deadline, sample, epoch) = next(iter(self.pending.items()))
                    metadata = self.metadata(pts)
                    needs_metadata = self.publisher.preserve_source_header or bool(self.publisher.camera_info_topic)
                    remaining = deadline - time.monotonic()
                    if needs_metadata and metadata is None and remaining > 0:
                        self.condition.wait(min(remaining, .01))
                        continue
                    self.pending.pop(pts)
                    if epoch != self.epoch:
                        continue
                    if needs_metadata and metadata is None:
                        self.expired += 1
                        if self.publisher.preserve_source_header:
                            continue
                try:
                    if epoch == self.epoch and not self.closed:
                        if self.publisher.publish_sample(sample, metadata, delivery_epoch=epoch) is False:
                            self.rejected += 1
                except Exception:
                    self.failures += 1
                    logging.getLogger(__name__).exception("ROS camera publication failed")
        finally:
            self.publisher.close()
