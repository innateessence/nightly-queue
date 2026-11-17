"""Reserve control threads before task code can exhaust the slot PID budget."""

import queue
import threading

from .server import Server


class SlotServer(Server):
    def __init__(self, *args, **kwargs):
        self.requests = queue.Queue(maxsize=16)
        super().__init__(*args, **kwargs)
        self.timeout = 0.2
        for _ in range(8):
            threading.Thread(target=self.worker, daemon=True).start()

    def process_request(self, request, address):
        try:
            self.requests.put_nowait((request, address))
        except queue.Full:
            self.shutdown_request(request)

    def worker(self):
        while True:
            request, address = self.requests.get()
            try:
                self.finish_request(request, address)
            except Exception:
                self.handle_error(request, address)
            finally:
                self.shutdown_request(request)
                self.requests.task_done()
