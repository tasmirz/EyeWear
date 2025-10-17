# for pipeline testing

import multiprocessing as mp
from multiprocessing.managers import BaseManager
import threading
import uuid


class QueueManager(BaseManager):
    pass


QueueManager.register("get_queue")

_queue = mp.Queue()


def _get_out_queue():
    return _queue


QueueManager.register("out_queue", callable=_get_out_queue)


def main() -> None:
    get_manager = QueueManager(address=("127.0.0.1", 50000), authkey=b"abcf")
    get_manager.connect()
    get_queue = get_manager.get_queue()

    out_manager = QueueManager(address=("127.0.0.1", 50001), authkey=b"abcfe")
    out_manager.start()
    out_queue = out_manager.out_queue()

    def _out_consumer() -> None:
        while True:
            try:
                output = out_queue.get()
                print("Processed output:", output)
            except (EOFError, KeyboardInterrupt):
                break

    consumer_thread = threading.Thread(target=_out_consumer, daemon=True)
    consumer_thread.start()

    try:
        while True:
            filename = input("Enter filename: ")

            if not filename:
                continue
            job = {
                'uuid': str(uuid.uuid4()),
                'input_file': filename,
                'public_key': 'yay',
            }
            get_queue.put(job)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
