"""Load compiled modules full of constants while subinterpreters hash and
compare their own tuples and floats."""

import importlib
import sys
from concurrent.futures import InterpreterPoolExecutor

import constant_modules
import workers

with InterpreterPoolExecutor(max_workers=4) as pool:
    # Every worker started, and ready, before the checks begin.
    for started in [pool.submit(workers.hash_and_compare, 0.0) for _ in range(4)]:
        started.result()
    checks = [pool.submit(workers.hash_and_compare, 3.0) for _ in range(4)]

    # A compiled module loads its constants each time it is imported: drop
    # them and import them again for as long as the workers check.
    constant_modules.load_all()
    names = [name for name in sys.modules if name.startswith("constant_module_")]
    loads = 0
    while not all(check.done() for check in checks):
        for name in names:
            del sys.modules[name]
            importlib.import_module(name)
        loads += 1
    results = [check.result() for check in checks]

failures = sum(failed for failed, _ in results)
rounds = sum(done for _, done in results)
print(f"failures={failures} rounds={rounds} loads={loads}")
sys.exit(1 if failures else 0)
