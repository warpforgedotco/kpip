"""Raise and catch in the main interpreter and in subinterpreters at once."""

import sys
import threading
from concurrent.futures import InterpreterPoolExecutor, ThreadPoolExecutor

import workers

stop = threading.Event()


def churn() -> int:
    caught = 0
    while not stop.is_set():
        for index in range(200):
            try:
                {}[index]
            except KeyError:
                caught += 1
    return caught


with InterpreterPoolExecutor(max_workers=4) as pool, ThreadPoolExecutor(8) as threads:
    churners = [threads.submit(churn) for _ in range(3)]
    submitters = [
        threads.submit(
            lambda: [
                pool.submit(workers.raise_and_catch, 200).result() for _ in range(40)
            ]
        )
        for _ in range(4)
    ]
    for submitter in submitters:
        submitter.result()
    stop.set()
    for churner in churners:
        churner.result()

print("ok")
sys.exit(0)
