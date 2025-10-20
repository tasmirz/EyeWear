
from multiprocessing.shared_memory import SharedMemory
import time

ocr_shm = shm.SharedMemory(create=True, size=4, name='ocr_signal')
call_shm = shm.SharedMemory(create=True, size=4, name='call_signal')
oqc_shm = shm.SharedMemory(create=True, size=4, name='ocr_queue_count')
oqi_shm = shm.SharedMemory(create=True, size=4, name='ocr_queue_images')

ocr_shm.buf[0] = 0
call_shm.buf[0] = 0
oqc_shm.buf[0] = 0
oqi_shm.buf[0] = 0

call_shm = SharedMemory(name='call_signal', create=True, size=4)
time.sleep(50000)  # Ensure the shared memory is ready