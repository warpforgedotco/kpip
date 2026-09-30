"""What compiled code shares between threads, and what it holds references to.

Run compiled, by ``test_nuitka_threads``. Each threaded case runs its body on
many threads at once and checks an invariant a race breaks, which matters
most without the GIL (patch 0015); the others check exact results and that
references are released. ``case_walrus_genexpr_scope`` fails with the GIL as
well without patch 0016. Exits non-zero and names each case that failed;
``THREADS_CASE`` runs one case only.
"""

import gc
import os
import sys
import threading

THREADS = 16
ROUNDS = int(sys.argv[1]) if len(sys.argv) > 1 else 2000


def run(body, *args):
    barrier = threading.Barrier(THREADS)
    errors = []

    def worker(index):
        try:
            barrier.wait()
            body(index, *args)
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(THREADS)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    if errors:
        raise errors[0]


# -- lists: appends, inserts, extends, removes, clears on one shared list ----


def case_list_append():
    shared = []

    def body(index):
        for i in range(ROUNDS):
            shared.append((index, i))

    run(body)
    assert len(shared) == THREADS * ROUNDS, len(shared)
    assert len(set(shared)) == THREADS * ROUNDS


def case_list_insert_extend():
    shared = []

    def body(index):
        nonlocal shared
        for i in range(ROUNDS // 4):
            shared.insert(0, index)
            shared.extend((index, index))
            shared += [index]

    run(body)
    assert len(shared) == THREADS * (ROUNDS // 4) * 4, len(shared)
    for index in range(THREADS):
        assert shared.count(index) == (ROUNDS // 4) * 4


def case_list_remove_reverse_clear():
    shared = []

    def body(index):
        for i in range(ROUNDS // 4):
            token = (index, i)
            shared.append(token)
            shared.reverse()
            shared.remove(token)
            assert token not in shared

    run(body)
    assert shared == [], shared
    shared.extend(range(100))

    def clear(index):
        for i in range(ROUNDS // 4):
            shared.clear()
            shared.extend(range(10))

    run(clear)
    assert len(shared) % 10 == 0, len(shared)


def case_list_item_replace():
    # Readers take items while writers replace them: a read that does not
    # hold the list may use an item already freed.
    shared = [object() for _ in range(64)]

    def body(index):
        for i in range(ROUNDS * 4):
            slot = i % 64
            if index % 2:
                shared[slot] = [index, i]
            else:
                item = shared[slot]
                repr(item)
                len(shared[slot : slot + 2])

    run(body)


# -- dicts: copies of a dict being changed -----------------------------------


def case_dict_copy():
    shared = {i: i for i in range(256)}

    def body(index):
        for i in range(ROUNDS):
            if index % 2:
                key = 1000 + index * ROUNDS + i
                shared[key] = i
                del shared[key]
            else:
                copied = dict(shared)
                copied2 = shared.copy()
                for key in range(256):
                    assert copied[key] == key and copied2[key] == key

    run(body)


# -- module globals: read while other threads rebind them --------------------

VALUE = 0
OTHER = "other"


def read_globals():
    return VALUE, OTHER


def case_module_globals():
    def body(index):
        global VALUE
        for i in range(ROUNDS * 2):
            if index % 4 == 0:
                VALUE = i
            else:
                value, other = read_globals()
                assert type(value) is int, value
                assert other == "other", other

    run(body)


# -- frames: exceptions and generators on many threads -----------------------


def raising(depth, token):
    marker = token
    if depth == 0:
        raise ValueError(marker)
    return raising(depth - 1, token)


def generator(token):
    for i in range(3):
        try:
            raising(2, token)
        except ValueError as exc:
            yield exc.args[0], exc.__traceback__


def case_frames():
    def body(index):
        for i in range(ROUNDS // 4):
            token = (index, i)
            try:
                raising(3, token)
            except ValueError as exc:
                assert exc.args[0] == token
                frame = exc.__traceback__.tb_next.tb_frame
                assert frame.f_locals["token"] == token, frame.f_locals
            for value, traceback in generator(token):
                assert value == token
                assert traceback.tb_next.tb_frame.f_locals["token"] == token

    run(body)


# -- objects: allocation churn with a collector running ----------------------


class WithDict:
    def __init__(self, value):
        self.value = value
        self.cycle = self


class WithSlots:
    __slots__ = ("value", "other")

    def __init__(self, value):
        self.value = value
        self.other = [value]


def case_allocation_and_gc():
    stop = threading.Event()

    def collector():
        while not stop.is_set():
            gc.collect()

    thread = threading.Thread(target=collector)
    thread.start()
    try:

        def body(index):
            kept = []
            for i in range(ROUNDS):
                obj = WithDict((index, i))
                slot = WithSlots(i)
                pair = (float(i), [obj], {"k": slot})
                if i % 64 == 0:
                    kept.append(pair)
            for pair in kept:
                obj = pair[1][0]
                assert obj.cycle is obj
                assert obj.value[0] == index
                assert pair[2]["k"].other == [pair[2]["k"].value]

        run(body)
    finally:
        stop.set()
        thread.join()


# -- single-threaded exactness: results and references ----------------------


def case_list_tuple_arguments():
    items = [(1,), 1, (1, 2), 1]
    assert items.count((1,)) == 1, items.count((1,))
    assert items.count(1) == 2
    items.remove((1, 2))
    items.remove((1,))
    assert items == [1, 1], items
    assert [(3,), 3].index((3,)) == 0


def case_list_index_bad_stop():
    start = 2**70 + 1  # a heap integer, freed early by a double release
    items = list(range(10))
    for _ in range(1000):
        try:
            items.index(1, start, "stop")
        except TypeError:
            pass
    assert start == 2**70 + 1


def recursive(depth):
    if depth:
        return recursive(depth - 1)
    return depth


def case_frames_released():
    for _ in range(1000):
        recursive(5)
    gc.collect()
    before = len(gc.get_objects())
    for _ in range(20000):
        recursive(5)
        try:
            raising(2, None)
        except ValueError:
            pass
    gc.collect()
    grown = len(gc.get_objects()) - before
    assert grown < 1000, grown


class Box:
    pass


class Holder:
    def __init__(self):
        self.value = Box()


def case_attribute_reads_release():
    import weakref

    for materialize in (False, True):
        holder = Holder()
        if materialize:
            holder.__dict__  # attributes then live in a real dictionary
        value_ref = weakref.ref(holder.value)
        for _ in range(10000):
            holder.value
        del holder
        assert value_ref() is None, ("attribute value kept alive by reads", materialize)

    mapping = {"key": Box(), 2: Box()}
    refs = [weakref.ref(mapping["key"]), weakref.ref(mapping[2])]
    for _ in range(10000):
        mapping["key"]
        mapping.get("key")
        mapping[2]
        mapping.get(2, None)
        "key" in mapping
    del mapping
    assert [ref() for ref in refs] == [None, None], (
        "dictionary value kept alive by reads"
    )


# -- lists combined and compared while being appended to ----------------------


def case_list_concat_compare():
    shared = [0] * 64

    def body(index):
        for i in range(ROUNDS):
            if index % 2:
                shared.append(i)
                if len(shared) > 4096:
                    del shared[64:]
            else:
                combined = shared + [index]
                assert combined[-1] == index
                copy = list(shared)
                assert (copy == shared) in (True, False)
                assert (copy < shared) in (True, False)

    run(body)


# -- class attributes rebound while instances read them -----------------------


class Rebound:
    attr = [0]

    def method(self):
        return self.attr


def case_class_attribute_rebind():
    def body(index):
        for i in range(ROUNDS * 2):
            if index % 4 == 0:
                Rebound.attr = [i]
                Rebound.method = lambda self, i=i: [i]
            else:
                instance = Rebound()
                assert type(instance.attr) is list
                assert type(instance.method()) is list

    run(body)


# -- module variables in class bodies and in-place, closure cells -------------


def case_class_body_globals():
    # A class body reads names from its namespace, falling back to globals.
    def body(index):
        global VALUE
        for i in range(ROUNDS // 2):
            if index % 4 == 0:
                VALUE = i + 1000
            else:

                class Local:
                    seen = VALUE
                    other = OTHER

                assert type(Local.seen) is int and Local.other == "other"

    run(body)


COUNTER = 2**70
TUPLE = (1,)
LISTED = []


def bump():
    global COUNTER, TUPLE, LISTED
    COUNTER += 1
    TUPLE += (2,)
    LISTED += [3]


def case_global_inplace():
    global COUNTER, TUPLE, LISTED
    for _ in range(1000):
        bump()
    assert COUNTER == 2**70 + 1000, COUNTER
    assert len(TUPLE) == 1001 and len(LISTED) == 1000
    TUPLE = (1,)
    LISTED = []

    def body(index):
        global TUPLE
        for i in range(ROUNDS // 4):
            bump()
            if index == 0 and i % 64 == 0:
                TUPLE = (1,)

    run(body)
    assert type(COUNTER) is int and type(TUPLE) is tuple and type(LISTED) is list


def case_closure_cells():
    counter = 2**70
    value = [0]

    def bump():
        nonlocal counter
        counter += 1

    for _ in range(1000):
        bump()
    assert counter == 2**70 + 1000, counter

    def body(index):
        nonlocal value
        for i in range(ROUNDS * 2):
            if index % 4 == 0:
                value = [i, index]
            else:
                current = value
                assert type(current) is list and len(current) in (1, 2)

    run(body)

    def reader():
        return value

    contents = reader.__closure__[0].cell_contents
    assert type(contents) is list


SHARED_INT = 2**70
SHARED_STR = "x" * 100


def case_inplace_shared_values():
    # An in-place operation may reuse an object only no one else can see:
    # a value other threads hold must never change underneath them.
    def body(index):
        global SHARED_INT, SHARED_STR
        for i in range(ROUNDS * 2):
            if index % 2:
                SHARED_INT += 1
                SHARED_STR += "y"
                if len(SHARED_STR) > 400:
                    SHARED_STR = "x" * 100
            else:
                held_int = SHARED_INT
                held_str = SHARED_STR
                int_copy = int(str(held_int))
                str_copy = "".join(held_str)
                for _ in range(20):
                    pass
                assert held_int == int_copy, (held_int, int_copy)
                assert held_str == str_copy

    run(body)


# -- assignment expressions in generator expressions --------------------------


def walrus_dependencies(values):
    # The shape of kpip's metadata dependency tuple: the assignment expression
    # binds in this function, not the module, as CPython does.
    return tuple(
        requirement
        for value in values
        if (requirement := value.strip()) is not None
        if not requirement.startswith("x")
    )


def case_walrus_genexpr_scope():
    values = ["a%d" % i for i in range(4)] + ["x%d" % i for i in range(20)]
    expected = tuple("a%d" % i for i in range(4))
    assert walrus_dependencies(values) == expected
    assert "requirement" not in globals(), "assignment expression target leaked"

    def body(index):
        mine = ["t%d-%d" % (index, i) for i in range(4)] + [
            "x%d" % i for i in range(20)
        ]
        wanted = tuple(mine[:4])
        for _ in range(ROUNDS):
            got = walrus_dependencies(mine)
            assert got == wanted, (got, wanted)

    run(body)


CASES = [
    case_list_append,
    case_list_insert_extend,
    case_list_remove_reverse_clear,
    case_list_item_replace,
    case_dict_copy,
    case_module_globals,
    case_frames,
    case_allocation_and_gc,
    case_list_tuple_arguments,
    case_list_index_bad_stop,
    case_frames_released,
    case_attribute_reads_release,
    case_list_concat_compare,
    case_class_attribute_rebind,
    case_class_body_globals,
    case_global_inplace,
    case_closure_cells,
    case_inplace_shared_values,
    case_walrus_genexpr_scope,
]


def main():
    gil = getattr(sys, "_is_gil_enabled", lambda: True)()
    print(f"compiled={'__compiled__' in globals()} gil={gil}")
    failed = 0
    only = os.environ.get("THREADS_CASE")
    for case in CASES:
        if only and case.__name__ != only:
            continue
        sys.stdout.flush()
        try:
            case()
        except BaseException as exc:  # noqa: BLE001
            failed += 1
            print(f"FAIL {case.__name__}: {type(exc).__name__}: {exc!r:.200}")
        else:
            print(f"ok   {case.__name__}")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
