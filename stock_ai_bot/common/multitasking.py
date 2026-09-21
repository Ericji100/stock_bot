import threading
import multiprocessing

def cpu_count():
    try:
        return multiprocessing.cpu_count()
    except:
        return 1

def set_max_threads(threads):
    pass

class Hub:
    def __init__(self):
        self.threads = []
hub = Hub()

def task(func):
    def wrapper(*args, **kwargs):
        t = threading.Thread(target=func, args=args, kwargs=kwargs)
        t.start()
        return t
    return wrapper