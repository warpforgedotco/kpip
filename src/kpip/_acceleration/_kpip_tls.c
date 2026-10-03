/* _kpip_tls: read a TLS stream into a buffer with the interpreter lock
 * released once, rather than once per record.
 *
 * ``ssl.SSLSocket.recv_into`` reads one TLS record, at most 16 KiB, per
 * call, and gives up and retakes the interpreter lock around each; a body
 * read a megabyte at a time is sixty-four handoffs of it, contended by
 * every other thread. ``fill`` waits for the first byte as ``recv`` does,
 * then goes on reading while more is already there -- in OpenSSL's buffer
 * or the socket's -- all without the lock: a thread that waited for the
 * lock while records arrived takes them all at once.
 *
 * Built into kpip's binary, where OpenSSL is linked statically beside
 * CPython (KPIP_TLS_BUILTIN). The SSL object is read from ``_ssl``'s
 * ``PySSLSocket``, whose first fields -- the socket's weak reference, then
 * the SSL pointer -- every CPython kpip is built with shares.
 */

#define PY_SSIZE_T_CLEAN
#include <Python.h>

#include <errno.h>
#include <string.h>

#ifndef _WIN32
#include <poll.h>
#else
#include <winsock2.h>
#endif

typedef struct ssl_st SSL;

extern int SSL_read_ex(SSL *ssl, void *buffer, size_t count, size_t *read);
extern int SSL_get_error(const SSL *ssl, int result);
extern int SSL_pending(const SSL *ssl);
extern void ERR_clear_error(void);

#define KPIP_SSL_ERROR_WANT_READ 2
#define KPIP_SSL_ERROR_WANT_WRITE 3
#define KPIP_SSL_ERROR_ZERO_RETURN 6

typedef struct {
    PyObject_HEAD
    PyObject *socket;
    SSL *ssl;
} SSLSocketHead;

#define FILL_TIMEOUT (-1)
#define FILL_FAILED (-2)

/* Wait for the socket to be ready, up to ``timeout_ms`` (-1: forever).
 * 1 when ready, 0 on a timeout, -1 on an error. */
static int
wait_ready(int fd, int writing, int timeout_ms)
{
#ifndef _WIN32
    struct pollfd entry = {fd, writing ? POLLOUT : POLLIN, 0};
    int ready;

    do {
        ready = poll(&entry, 1, timeout_ms);
    } while (ready < 0 && errno == EINTR);

    return ready < 0 ? -1 : ready > 0;
#else
    WSAPOLLFD entry = {(SOCKET)fd, writing ? POLLWRNORM : POLLRDNORM, 0};
    int ready = WSAPoll(&entry, 1, timeout_ms);

    return ready == SOCKET_ERROR ? -1 : ready > 0;
#endif
}

PyDoc_STRVAR(fill_doc,
"fill(sslobj, fd, buffer, count, timeout_ms) -> int\n\n"
"Read up to ``count`` bytes of ``sslobj``'s stream into ``buffer``, as\n"
"many as are there once one is: the number read, 0 at the end of the\n"
"stream. -1 when the\n"
"socket timed out before any byte, and -2 when the read failed before any:\n"
"the caller reads again through ``ssl``, for its answer and its exception.");

static PyObject *
fill(PyObject *self, PyObject *args)
{
    PyObject *sslobj;
    int fd;
    Py_buffer view;
    Py_ssize_t count;
    int timeout_ms;

    if (!PyArg_ParseTuple(args, "Oiw*ni", &sslobj, &fd, &view, &count, &timeout_ms)) {
        return NULL;
    }

    if (strcmp(Py_TYPE(sslobj)->tp_name, "_ssl._SSLSocket") != 0) {
        PyBuffer_Release(&view);
        PyErr_SetString(PyExc_TypeError, "fill needs an _ssl._SSLSocket");
        return NULL;
    }

    if (count < 0 || count > view.len) {
        count = view.len;
    }

    SSL *ssl = ((SSLSocketHead *)sslobj)->ssl;
    char *out = view.buf;
    Py_ssize_t filled = 0;
    Py_ssize_t status = 0;

    Py_BEGIN_ALLOW_THREADS
    while (filled < count) {
        size_t got = 0;

        /* Past the first record, only what is already there. */
        if (filled > 0 && SSL_pending(ssl) <= 0 && wait_ready(fd, 0, 0) <= 0) {
            break;
        }

        ERR_clear_error();

        int result = SSL_read_ex(ssl, out + filled, (size_t)(count - filled), &got);

        if (result > 0) {
            filled += (Py_ssize_t)got;
            continue;
        }

        int error = SSL_get_error(ssl, result);

        if (error == KPIP_SSL_ERROR_ZERO_RETURN) {
            break;
        }

        if (error == KPIP_SSL_ERROR_WANT_READ || error == KPIP_SSL_ERROR_WANT_WRITE) {
            if (filled > 0) {
                break;
            }

            int ready = wait_ready(fd, error == KPIP_SSL_ERROR_WANT_WRITE, timeout_ms);

            if (ready > 0) {
                continue;
            }

            status = ready == 0 ? FILL_TIMEOUT : FILL_FAILED;
            break;
        }

        status = FILL_FAILED;
        break;
    }
    ERR_clear_error();
    Py_END_ALLOW_THREADS

    PyBuffer_Release(&view);

    return PyLong_FromSsize_t(filled > 0 ? filled : status);
}

static PyMethodDef methods[] = {
    {"fill", fill, METH_VARARGS, fill_doc},
    {NULL, NULL, 0, NULL},
};

static PyModuleDef_Slot slots[] = {
    {Py_mod_multiple_interpreters, Py_MOD_PER_INTERPRETER_GIL_SUPPORTED},
    {Py_mod_gil, Py_MOD_GIL_NOT_USED},
    {0, NULL},
};

static struct PyModuleDef module = {
    PyModuleDef_HEAD_INIT,
    .m_name = "_kpip_tls",
    .m_doc = "A TLS stream read into a buffer under one release of the lock.",
    .m_size = 0,
    .m_methods = methods,
    .m_slots = slots,
};

PyMODINIT_FUNC
PyInit__kpip_tls(void)
{
    return PyModuleDef_Init(&module);
}

#ifdef KPIP_TLS_BUILTIN
/* Registered before main, as _kpip_link_tree.c explains. */
static void
register_builtin(void)
{
    PyImport_AppendInittab("_kpip_tls", PyInit__kpip_tls);
}

#ifdef _MSC_VER
#ifdef _WIN64
#define KPIP_SYMBOL_PREFIX ""
#else
#define KPIP_SYMBOL_PREFIX "_"
#endif
#pragma section(".CRT$XCU", read)
__declspec(allocate(".CRT$XCU")) void(__cdecl *kpip_tls_register)(void) =
    register_builtin;
#pragma comment(linker, "/include:" KPIP_SYMBOL_PREFIX "kpip_tls_register")
#else
__attribute__((constructor)) static void
register_builtin_constructor(void)
{
    register_builtin();
}
#endif
#endif
