"""Test-only TCP bandwidth sharing; never changes host routes, qdiscs or MTU."""

import asyncio
import socket
import threading
import time


class ConstrainedProxy:
    def __init__(self, destination, mbps, destinations=None):
        self.destination = destination
        self.destinations = destinations or [destination]
        self.bytes_per_second = mbps * 1_000_000 / 8
        if self.bytes_per_second <= 0:
            raise ValueError("positive bandwidth required")
        self.loop = asyncio.new_event_loop()
        self.ready = threading.Event()
        self.thread = threading.Thread(target=self.run, daemon=True)
        self.connections = 0
        self.transferred = 0
        self.tasks = set()
        self.writers = set()
        self.lock = None
        self.next_byte_time = 0

    async def copy(self, reader, writer, paced):
        while data := await reader.read(4096):
            if paced:
                async with self.lock:
                    now = time.monotonic()
                    self.next_byte_time = max(now, self.next_byte_time)
                    delay = self.next_byte_time - now
                    self.next_byte_time += len(data) / self.bytes_per_second
                    if delay:
                        await asyncio.sleep(delay)
                    writer.write(data)
                    await writer.drain()
                    self.transferred += len(data)
            else:
                writer.write(data)
                await writer.drain()

    async def accept(self, reader, writer, destination=None):
        self.connections += 1
        task = asyncio.current_task()
        self.tasks.add(task)
        peers = [writer]
        copies = []
        try:
            remote_reader, remote_writer = await asyncio.open_connection(*(destination or self.destination))
            peers.append(remote_writer)
            self.writers.update(peers)
            for peer in peers:
                peer.transport.set_write_buffer_limits(high=8192, low=4096)
                peer.get_extra_info("socket").setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 16384)
            # Shared capacity in the source->operator direction, across all links.
            copies = [asyncio.create_task(self.copy(reader, remote_writer, True)),
                      asyncio.create_task(self.copy(remote_reader, writer, False))]
            await asyncio.wait(copies, return_when=asyncio.FIRST_COMPLETED)
        except (OSError, ConnectionError):
            pass
        finally:
            for copy in copies:
                copy.cancel()
            if copies:
                await asyncio.gather(*copies, return_exceptions=True)
            for peer in peers:
                peer.close()
                self.writers.discard(peer)
            self.tasks.discard(task)

    def run(self):
        asyncio.set_event_loop(self.loop)
        self.lock = asyncio.Lock()
        self.servers = [self.loop.run_until_complete(asyncio.start_server(
            lambda r, w, destination=destination: self.accept(r, w, destination), "127.0.0.1", 0))
            for destination in self.destinations]
        self.ports = [server.sockets[0].getsockname()[1] for server in self.servers]
        self.port = self.ports[0]
        self.ready.set()
        self.loop.run_forever()
        self.loop.close()

    def start(self):
        self.thread.start()
        if not self.ready.wait(5):
            raise RuntimeError("bandwidth proxy did not start")
        return self

    async def shutdown(self):
        for server in self.servers:
            server.close()
            await server.wait_closed()
        for writer in list(self.writers):
            writer.close()
        for task in list(self.tasks):
            task.cancel()
        if self.tasks:
            await asyncio.gather(*list(self.tasks), return_exceptions=True)

    def stop(self):
        asyncio.run_coroutine_threadsafe(self.shutdown(), self.loop).result(timeout=5)
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.thread.join(timeout=5)
