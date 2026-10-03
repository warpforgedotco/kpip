/* _kpip_http: one HTTP/1.1 exchange on a connected socket, with the
 * interpreter lock released for all of it.
 *
 * urllib3 and http.client spend most of a small request under the lock:
 * building the request, reading the status line and every header a line at
 * a time, framing the body, each step Python bytecode between reads that
 * each give up and retake the lock. ``exchange`` writes a request Python
 * built, then reads the status line, the headers and the body -- by its
 * length, in chunks, or to the end of the stream -- without the lock, and
 * hands back what it read: Python only builds the response from it.
 *
 * The connection is urllib3's, connected, handshaken and verified there:
 * this reads and writes through its TLS object, the SSL pointer of _ssl's
 * PySSLSocket, as _kpip_tls does, or through the plain socket when there is
 * none. Anything this does not take on -- a folded header, a transfer
 * coding other than chunked, an interim response other than 1xx, a stream
 * that ends early -- is a negative result, and the caller makes the request
 * again through urllib3, which answers or raises as it always has.
 *
 * Built into kpip's binary (KPIP_HTTP_BUILTIN), where OpenSSL is linked
 * statically beside CPython.
 */

#define PY_SSIZE_T_CLEAN
#include <Python.h>

#include <errno.h>
#include <string.h>

#ifndef _WIN32
#include <poll.h>
#include <sys/socket.h>
#include <sys/types.h>
#else
#include <winsock2.h>
#endif

#include <limits.h>

typedef struct ssl_st SSL;

#ifndef KPIP_HTTP_PLAIN_ONLY
extern int SSL_read_ex(SSL *ssl, void *buffer, size_t count, size_t *read);
extern int SSL_write_ex(SSL *ssl, const void *buffer, size_t count, size_t *written);
extern int SSL_get_error(const SSL *ssl, int result);
extern void ERR_clear_error(void);
#else
/* Built for a test against an interpreter whose OpenSSL is its own: plain
 * sockets only, an SSL object refused before any of these could run. */
static int SSL_read_ex(SSL *ssl, void *buffer, size_t count, size_t *read) { return 0; }
static int SSL_write_ex(SSL *ssl, const void *buffer, size_t count, size_t *written) { return 0; }
static int SSL_get_error(const SSL *ssl, int result) { return 1; }
static void ERR_clear_error(void) {}
#endif

#define KPIP_SSL_ERROR_WANT_READ 2
#define KPIP_SSL_ERROR_WANT_WRITE 3
#define KPIP_SSL_ERROR_SYSCALL 5
#define KPIP_SSL_ERROR_ZERO_RETURN 6

typedef struct {
    PyObject_HEAD
    PyObject *socket;
    SSL *ssl;
} SSLSocketHead;

/* Results: the caller makes the request again through urllib3 on each. */
#define FAILED_IO (-1)
#define FAILED_TIMEOUT (-2)
#define FAILED_PROTOCOL (-3)
#define FAILED_UNSUPPORTED (-4)
#define FAILED_MEMORY (-5)

#define HEADER_LIMIT (256 * 1024)
#define READ_SIZE (64 * 1024)

typedef struct {
    SSL *ssl;
    int fd;
    int timeout_ms;
} Transport;

typedef struct {
    char *data;
    size_t length;
    size_t capacity;
} Buffer;

typedef struct {
    size_t name;
    size_t name_length;
    size_t value;
    size_t value_length;
} Field;

typedef struct {
    Transport *transport;
    Buffer input;
    size_t position; /* the first byte of input not yet parsed */
    int ended;       /* the stream has ended */
} Reader;

/* Wait for the socket, up to ``timeout_ms`` (-1: forever). 1 when ready, 0
 * on a timeout, -1 on an error. */
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

static int
would_block(void)
{
#ifndef _WIN32
    return errno == EAGAIN || errno == EWOULDBLOCK;
#else
    return WSAGetLastError() == WSAEWOULDBLOCK;
#endif
}

/* Read up to ``count`` bytes: the number read, 0 at the end of the stream,
 * or a negative FAILED_ result. */
static Py_ssize_t
transport_read(Transport *transport, char *out, size_t count)
{
    for (;;) {
        if (transport->ssl != NULL) {
            size_t got = 0;

            ERR_clear_error();
            errno = 0;

            int result = SSL_read_ex(transport->ssl, out, count, &got);

            if (result > 0) {
                return (Py_ssize_t)got;
            }

            int error = SSL_get_error(transport->ssl, result);

            if (error == KPIP_SSL_ERROR_ZERO_RETURN) {
                return 0;
            }

            if (error == KPIP_SSL_ERROR_WANT_READ || error == KPIP_SSL_ERROR_WANT_WRITE) {
                int ready = wait_ready(
                    transport->fd, error == KPIP_SSL_ERROR_WANT_WRITE, transport->timeout_ms
                );

                if (ready > 0) {
                    continue;
                }

                return ready == 0 ? FAILED_TIMEOUT : FAILED_IO;
            }

            /* The peer closed without a close_notify: the end of the
             * stream, as ssl's suppress_ragged_eofs reads it. */
            if (error == KPIP_SSL_ERROR_SYSCALL && errno == 0) {
                return 0;
            }

            return FAILED_IO;
        }

#ifndef _WIN32
        ssize_t got = recv(transport->fd, out, count, 0);
#else
        int got = recv((SOCKET)transport->fd, out, (int)(count > INT_MAX ? INT_MAX : count), 0);
#endif

        if (got >= 0) {
            return (Py_ssize_t)got;
        }

#ifndef _WIN32
        if (errno == EINTR) {
            continue;
        }
#endif

        if (!would_block()) {
            return FAILED_IO;
        }

        int ready = wait_ready(transport->fd, 0, transport->timeout_ms);

        if (ready <= 0) {
            return ready == 0 ? FAILED_TIMEOUT : FAILED_IO;
        }
    }
}

static int
transport_write(Transport *transport, const char *data, size_t count)
{
    size_t sent = 0;

    while (sent < count) {
        if (transport->ssl != NULL) {
            size_t wrote = 0;

            ERR_clear_error();

            int result = SSL_write_ex(transport->ssl, data + sent, count - sent, &wrote);

            if (result > 0) {
                sent += wrote;
                continue;
            }

            int error = SSL_get_error(transport->ssl, result);

            if (error != KPIP_SSL_ERROR_WANT_READ && error != KPIP_SSL_ERROR_WANT_WRITE) {
                return FAILED_IO;
            }

            int ready = wait_ready(
                transport->fd, error == KPIP_SSL_ERROR_WANT_WRITE, transport->timeout_ms
            );

            if (ready <= 0) {
                return ready == 0 ? FAILED_TIMEOUT : FAILED_IO;
            }

            continue;
        }

#ifndef _WIN32
        ssize_t wrote = send(transport->fd, data + sent, count - sent, 0);
#else
        size_t left = count - sent;
        int wrote = send((SOCKET)transport->fd, data + sent, (int)(left > INT_MAX ? INT_MAX : left), 0);
#endif

        if (wrote >= 0) {
            sent += (size_t)wrote;
            continue;
        }

#ifndef _WIN32
        if (errno == EINTR) {
            continue;
        }
#endif

        if (!would_block()) {
            return FAILED_IO;
        }

        int ready = wait_ready(transport->fd, 1, transport->timeout_ms);

        if (ready <= 0) {
            return ready == 0 ? FAILED_TIMEOUT : FAILED_IO;
        }
    }

    return 0;
}

static int
reserve(Buffer *buffer, size_t more)
{
    if (buffer->length + more <= buffer->capacity) {
        return 0;
    }

    size_t capacity = buffer->capacity ? buffer->capacity : READ_SIZE;

    while (capacity < buffer->length + more) {
        if (capacity > ((size_t)PY_SSIZE_T_MAX) / 2) {
            return FAILED_MEMORY;
        }

        capacity *= 2;
    }

    char *data = PyMem_RawRealloc(buffer->data, capacity);

    if (data == NULL) {
        return FAILED_MEMORY;
    }

    buffer->data = data;
    buffer->capacity = capacity;

    return 0;
}

static int
append(Buffer *buffer, const char *data, size_t count)
{
    int failed = reserve(buffer, count);

    if (failed) {
        return failed;
    }

    memcpy(buffer->data + buffer->length, data, count);
    buffer->length += count;

    return 0;
}

/* Read more of the stream into the reader's input: 1 when some came, 0 at
 * the end, or a negative FAILED_ result. */
static int
fill(Reader *reader)
{
    if (reader->ended) {
        return 0;
    }

    int failed = reserve(&reader->input, READ_SIZE);

    if (failed) {
        return failed;
    }

    Py_ssize_t got = transport_read(
        reader->transport,
        reader->input.data + reader->input.length,
        reader->input.capacity - reader->input.length
    );

    if (got < 0) {
        return (int)got;
    }

    if (got == 0) {
        reader->ended = 1;
        return 0;
    }

    reader->input.length += (size_t)got;

    return 1;
}

/* Ensure ``count`` unparsed bytes: 0, or a negative FAILED_ result, an
 * early end of the stream being a protocol failure. */
static int
need(Reader *reader, size_t count)
{
    while (reader->input.length - reader->position < count) {
        int got = fill(reader);

        if (got < 0) {
            return got;
        }

        if (got == 0) {
            return FAILED_PROTOCOL;
        }
    }

    return 0;
}

/* The offset past the next CRLF from ``reader->position``, reading more as
 * needed but no further than ``limit`` bytes: 0 and a negative *failed when
 * there is none. */
static size_t
line_end(Reader *reader, size_t limit, int *failed)
{
    size_t scanned = reader->position;

    for (;;) {
        const char *start = reader->input.data + scanned;
        size_t available = reader->input.length - scanned;
        const char *found = available ? memchr(start, '\n', available) : NULL;

        if (found != NULL) {
            size_t end = (size_t)(found - reader->input.data);

            if (end == reader->position || reader->input.data[end - 1] != '\r') {
                *failed = FAILED_PROTOCOL;
                return 0;
            }

            return end + 1;
        }

        scanned = reader->input.length;

        if (scanned - reader->position > limit) {
            *failed = FAILED_PROTOCOL;
            return 0;
        }

        int got = fill(reader);

        if (got <= 0) {
            *failed = got < 0 ? got : FAILED_PROTOCOL;
            return 0;
        }
    }
}

static int
lower_equals(const char *text, size_t length, const char *expected)
{
    size_t expected_length = strlen(expected);

    if (length != expected_length) {
        return 0;
    }

    for (size_t i = 0; i < length; i++) {
        char c = text[i];

        if (c >= 'A' && c <= 'Z') {
            c = (char)(c + 32);
        }

        if (c != expected[i]) {
            return 0;
        }
    }

    return 1;
}

static int
is_token_char(char c)
{
    if ((c >= 'a' && c <= 'z') || (c >= 'A' && c <= 'Z') || (c >= '0' && c <= '9')) {
        return 1;
    }

    return strchr("!#$%&'*+-.^_`|~", c) != NULL && c != '\0';
}

/* Each comma-separated token of a header value: whether ``token`` is among
 * them, and whether it is the last. */
static void
find_token(const char *value, size_t length, const char *token, int *any, int *last)
{
    size_t start = 0;

    *any = 0;
    *last = 0;

    while (start <= length) {
        size_t end = start;

        while (end < length && value[end] != ',') {
            end++;
        }

        size_t a = start, b = end;

        while (a < b && (value[a] == ' ' || value[a] == '\t')) {
            a++;
        }

        while (b > a && (value[b - 1] == ' ' || value[b - 1] == '\t')) {
            b--;
        }

        int matches = lower_equals(value + a, b - a, token);

        if (matches) {
            *any = 1;
        }

        if (b > a) {
            *last = matches;
        }

        start = end + 1;
    }
}

typedef struct {
    int status;
    int minor;
    size_t reason;
    size_t reason_length;
    Field *fields;
    size_t field_count;
    size_t field_capacity;
    Buffer body;
    int keep_alive;
} Response;

/* Read one response head into ``response``, its offsets into the reader's
 * input. 0, or a negative FAILED_ result. */
static int
read_head(Reader *reader, Response *response)
{
    int failed = 0;
    size_t end = line_end(reader, HEADER_LIMIT, &failed);

    if (end == 0) {
        return failed;
    }

    const char *line = reader->input.data + reader->position;
    size_t length = end - 2 - reader->position;

    /* HTTP/1.x SP 3DIGIT [SP reason] */
    if (length < 12 || memcmp(line, "HTTP/1.", 7) != 0 || (line[7] != '0' && line[7] != '1')
        || line[8] != ' ') {
        return FAILED_PROTOCOL;
    }

    for (int i = 9; i < 12; i++) {
        if (line[i] < '0' || line[i] > '9') {
            return FAILED_PROTOCOL;
        }
    }

    if (length > 12 && line[12] != ' ') {
        return FAILED_PROTOCOL;
    }

    response->minor = line[7] - '0';
    response->status = (line[9] - '0') * 100 + (line[10] - '0') * 10 + (line[11] - '0');
    response->reason = reader->position + (length > 12 ? 13 : 12);
    response->reason_length = length > 12 ? length - 13 : 0;
    response->field_count = 0;
    reader->position = end;

    size_t head_start = end;

    for (;;) {
        end = line_end(reader, HEADER_LIMIT - (reader->position - head_start), &failed);

        if (end == 0) {
            return failed;
        }

        line = reader->input.data + reader->position;
        length = end - 2 - reader->position;

        if (length == 0) {
            reader->position = end;
            return 0;
        }

        /* A folded line continues the one before: left to urllib3. */
        if (line[0] == ' ' || line[0] == '\t') {
            return FAILED_UNSUPPORTED;
        }

        size_t colon = 0;

        while (colon < length && line[colon] != ':') {
            if (!is_token_char(line[colon])) {
                return FAILED_PROTOCOL;
            }

            colon++;
        }

        if (colon == 0 || colon == length) {
            return FAILED_PROTOCOL;
        }

        size_t value = colon + 1, value_end = length;

        while (value < value_end && (line[value] == ' ' || line[value] == '\t')) {
            value++;
        }

        while (value_end > value && (line[value_end - 1] == ' ' || line[value_end - 1] == '\t')) {
            value_end--;
        }

        if (response->field_count == response->field_capacity) {
            size_t capacity = response->field_capacity ? response->field_capacity * 2 : 32;
            Field *fields = PyMem_RawRealloc(response->fields, capacity * sizeof(Field));

            if (fields == NULL) {
                return FAILED_MEMORY;
            }

            response->fields = fields;
            response->field_capacity = capacity;
        }

        Field *field = &response->fields[response->field_count++];

        field->name = reader->position;
        field->name_length = colon;
        field->value = reader->position + value;
        field->value_length = value_end - value;
        reader->position = end;
    }
}

static int
read_chunked(Reader *reader, Buffer *body)
{
    for (;;) {
        int failed = 0;
        size_t end = line_end(reader, 4096, &failed);

        if (end == 0) {
            return failed;
        }

        const char *line = reader->input.data + reader->position;
        size_t length = end - 2 - reader->position;
        size_t size = 0, digits = 0;

        while (digits < length) {
            char c = line[digits];
            int value;

            if (c >= '0' && c <= '9') {
                value = c - '0';
            }
            else if (c >= 'a' && c <= 'f') {
                value = c - 'a' + 10;
            }
            else if (c >= 'A' && c <= 'F') {
                value = c - 'A' + 10;
            }
            else {
                break;
            }

            if (size > (((size_t)PY_SSIZE_T_MAX) >> 4)) {
                return FAILED_PROTOCOL;
            }

            size = size * 16 + (size_t)value;
            digits++;
        }

        /* Whitespace, then extensions after ';', are ignored. */
        size_t rest = digits;

        while (rest < length && (line[rest] == ' ' || line[rest] == '\t')) {
            rest++;
        }

        if (digits == 0 || (rest < length && line[rest] != ';')) {
            return FAILED_PROTOCOL;
        }

        reader->position = end;

        if (size == 0) {
            /* Trailers, up to the empty line, are read and dropped. */
            for (;;) {
                end = line_end(reader, HEADER_LIMIT, &failed);

                if (end == 0) {
                    return failed;
                }

                int empty = end - reader->position == 2;

                reader->position = end;

                if (empty) {
                    return 0;
                }
            }
        }

        failed = need(reader, size + 2);

        if (failed) {
            return failed;
        }

        const char *data = reader->input.data + reader->position;

        if (data[size] != '\r' || data[size + 1] != '\n') {
            return FAILED_PROTOCOL;
        }

        failed = append(body, data, size);

        if (failed) {
            return failed;
        }

        reader->position += size + 2;
    }
}

/* Read the body the head frames into ``response->body``. */
static int
read_body(Reader *reader, Response *response, int head_only)
{
    const char *input = reader->input.data;
    int chunked = 0, has_length = 0, close = 0, keep_alive = 0;
    size_t content_length = 0;

    for (size_t i = 0; i < response->field_count; i++) {
        Field *field = &response->fields[i];
        const char *name = input + field->name;
        const char *value = input + field->value;
        size_t value_length = field->value_length;

        if (lower_equals(name, field->name_length, "transfer-encoding")) {
            int any, last;

            find_token(value, value_length, "chunked", &any, &last);

            /* Only "chunked" alone: any other coding is urllib3's. */
            if (!(any && last) || !lower_equals(value, value_length, "chunked")) {
                return FAILED_UNSUPPORTED;
            }

            chunked = 1;
        }
        else if (lower_equals(name, field->name_length, "content-length")) {
            size_t parsed = 0;

            if (value_length == 0) {
                return FAILED_PROTOCOL;
            }

            for (size_t j = 0; j < value_length; j++) {
                if (value[j] < '0' || value[j] > '9' || parsed > (((size_t)PY_SSIZE_T_MAX) - 9) / 10) {
                    return FAILED_PROTOCOL;
                }

                parsed = parsed * 10 + (size_t)(value[j] - '0');
            }

            if (has_length && parsed != content_length) {
                return FAILED_PROTOCOL;
            }

            has_length = 1;
            content_length = parsed;
        }
        else if (lower_equals(name, field->name_length, "connection")) {
            int any, last;

            find_token(value, value_length, "close", &any, &last);
            close |= any;
            find_token(value, value_length, "keep-alive", &any, &last);
            keep_alive |= any;
        }
    }

    response->keep_alive = !close && (response->minor == 1 || keep_alive);

    int status = response->status;

    if (head_only || status == 204 || status == 304 || (status >= 100 && status < 200)) {
        return 0;
    }

    if (chunked) {
        return read_chunked(reader, &response->body);
    }

    if (has_length) {
        int failed = need(reader, content_length);

        if (failed) {
            return failed;
        }

        failed = append(&response->body, reader->input.data + reader->position, content_length);
        reader->position += content_length;

        return failed;
    }

    /* Neither: the body is the rest of the stream. */
    response->keep_alive = 0;

    for (;;) {
        size_t available = reader->input.length - reader->position;
        int failed = append(&response->body, reader->input.data + reader->position, available);

        if (failed) {
            return failed;
        }

        reader->position += available;

        int got = fill(reader);

        if (got < 0) {
            return got;
        }

        if (got == 0) {
            return 0;
        }
    }
}

static int
exchange_unlocked(Transport *transport, const char *request, size_t request_length,
                  int head_only, Reader *reader, Response *response)
{
    int failed = transport_write(transport, request, request_length);

    if (failed) {
        return failed;
    }

    reader->transport = transport;

    for (;;) {
        failed = read_head(reader, response);

        if (failed) {
            return failed;
        }

        /* 100 Continue, 103 Early Hints: the real response follows. */
        if (response->status >= 100 && response->status < 200) {
            if (response->status == 101) {
                return FAILED_UNSUPPORTED;
            }

            continue;
        }

        break;
    }

    failed = read_body(reader, response, head_only);

    if (failed) {
        return failed;
    }

    /* Bytes past the response: nothing else may share the connection. */
    if (reader->position != reader->input.length) {
        response->keep_alive = 0;
    }

    return 0;
}

static PyObject *
build_result(Reader *reader, Response *response)
{
    PyObject *reason = NULL, *fields = NULL, *body = NULL, *result = NULL;
    const char *input = reader->input.data;

    reason = PyUnicode_DecodeLatin1(input + response->reason, (Py_ssize_t)response->reason_length, NULL);
    fields = PyList_New((Py_ssize_t)response->field_count);

    if (reason == NULL || fields == NULL) {
        goto done;
    }

    for (size_t i = 0; i < response->field_count; i++) {
        Field *field = &response->fields[i];
        PyObject *name = PyUnicode_DecodeLatin1(input + field->name, (Py_ssize_t)field->name_length, NULL);
        PyObject *value = name == NULL
            ? NULL
            : PyUnicode_DecodeLatin1(input + field->value, (Py_ssize_t)field->value_length, NULL);
        PyObject *pair = value == NULL ? NULL : PyTuple_Pack(2, name, value);

        Py_XDECREF(name);
        Py_XDECREF(value);

        if (pair == NULL) {
            goto done;
        }

        PyList_SET_ITEM(fields, (Py_ssize_t)i, pair);
    }

    body = PyBytes_FromStringAndSize(response->body.data, (Py_ssize_t)response->body.length);

    if (body == NULL) {
        goto done;
    }

    result = Py_BuildValue(
        "(iOOOO)", response->status, reason, fields, body, response->keep_alive ? Py_True : Py_False
    );

done:
    Py_XDECREF(reason);
    Py_XDECREF(fields);
    Py_XDECREF(body);

    return result;
}

PyDoc_STRVAR(exchange_doc,
"exchange(sslobj, fd, request, timeout_ms, head_only) -> tuple | int\n\n"
"Write ``request`` to the connection and read one response to it: through\n"
"``sslobj``, an _ssl._SSLSocket, or the socket ``fd`` itself when it is\n"
"None. ``(status, reason, [(name, value), ...], body, keep_alive)``, the\n"
"body as sent, any content coding still on it; or a negative int when the\n"
"exchange failed or took a turn this leaves to urllib3. ``timeout_ms`` bounds\n"
"each wait for the socket, -1 for none. The connection is unusable after a\n"
"failure, and after a response without ``keep_alive``.");

static PyObject *
exchange(PyObject *self, PyObject *args)
{
    PyObject *sslobj;
    int fd;
    Py_buffer request;
    int timeout_ms;
    int head_only;

    if (!PyArg_ParseTuple(args, "Oiy*ip", &sslobj, &fd, &request, &timeout_ms, &head_only)) {
        return NULL;
    }

    SSL *ssl = NULL;

    if (sslobj != Py_None) {
#ifdef KPIP_HTTP_PLAIN_ONLY
        PyBuffer_Release(&request);
        PyErr_SetString(PyExc_TypeError, "built for plain sockets only");
        return NULL;
#endif
        if (strcmp(Py_TYPE(sslobj)->tp_name, "_ssl._SSLSocket") != 0) {
            PyBuffer_Release(&request);
            PyErr_SetString(PyExc_TypeError, "exchange needs an _ssl._SSLSocket or None");
            return NULL;
        }

        ssl = ((SSLSocketHead *)sslobj)->ssl;
    }

    Transport transport = {ssl, fd, timeout_ms};
    Reader reader = {0};
    Response response = {0};
    int failed;

    Py_BEGIN_ALLOW_THREADS
    failed = exchange_unlocked(
        &transport, request.buf, (size_t)request.len, head_only, &reader, &response
    );
    if (ssl != NULL) {
        ERR_clear_error();
    }
    Py_END_ALLOW_THREADS

    PyBuffer_Release(&request);

    PyObject *result = failed ? PyLong_FromLong(failed) : build_result(&reader, &response);

    PyMem_RawFree(reader.input.data);
    PyMem_RawFree(response.fields);
    PyMem_RawFree(response.body.data);

    return result;
}

static PyMethodDef methods[] = {
    {"exchange", exchange, METH_VARARGS, exchange_doc},
    {NULL, NULL, 0, NULL},
};

static int
add_constants(PyObject *module)
{
    if (PyModule_AddIntConstant(module, "FAILED_IO", FAILED_IO) < 0
        || PyModule_AddIntConstant(module, "FAILED_TIMEOUT", FAILED_TIMEOUT) < 0
        || PyModule_AddIntConstant(module, "FAILED_PROTOCOL", FAILED_PROTOCOL) < 0
        || PyModule_AddIntConstant(module, "FAILED_UNSUPPORTED", FAILED_UNSUPPORTED) < 0
        || PyModule_AddIntConstant(module, "FAILED_MEMORY", FAILED_MEMORY) < 0) {
        return -1;
    }

    return 0;
}

static PyModuleDef_Slot slots[] = {
    {Py_mod_exec, add_constants},
    {Py_mod_multiple_interpreters, Py_MOD_PER_INTERPRETER_GIL_SUPPORTED},
    {Py_mod_gil, Py_MOD_GIL_NOT_USED},
    {0, NULL},
};

static struct PyModuleDef module = {
    PyModuleDef_HEAD_INIT,
    .m_name = "_kpip_http",
    .m_doc = "One HTTP/1.1 exchange on a connected socket, without the lock.",
    .m_size = 0,
    .m_methods = methods,
    .m_slots = slots,
};

PyMODINIT_FUNC
PyInit__kpip_http(void)
{
    return PyModuleDef_Init(&module);
}

#ifdef KPIP_HTTP_BUILTIN
/* Registered before main, as _kpip_link_tree.c explains. */
static void
register_builtin(void)
{
    PyImport_AppendInittab("_kpip_http", PyInit__kpip_http);
}

#ifdef _MSC_VER
#ifdef _WIN64
#define KPIP_SYMBOL_PREFIX ""
#else
#define KPIP_SYMBOL_PREFIX "_"
#endif
#pragma section(".CRT$XCU", read)
__declspec(allocate(".CRT$XCU")) void(__cdecl *kpip_http_register)(void) =
    register_builtin;
#pragma comment(linker, "/include:" KPIP_SYMBOL_PREFIX "kpip_http_register")
#else
__attribute__((constructor)) static void
register_builtin_constructor(void)
{
    register_builtin();
}
#endif
#endif
