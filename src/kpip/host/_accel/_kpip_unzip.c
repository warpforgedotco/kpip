/* kpip.install.wheel_archive_cache's member extraction loop, in C, with the
 * GIL released for a wheel's members at once.
 *
 * kpip-compile compiles this into the binary as the built-in _kpip_unzip
 * (KPIP_UNZIP_BUILTIN). extract_members returns (0, len(members)), or
 * (1, index) for the first member it leaves to Python: one it does not
 * handle, one that fails a check, or one it cannot write. Python extracts
 * that member its own way, raising what zipfile raises, and resumes here
 * after it. It needs zlib linked in, which a build says by defining
 * KPIP_UNZIP_ZLIB; without it, or on Windows, which has no pread, the
 * module has no extract_members, and Python extracts every member.
 */

#define PY_SSIZE_T_CLEAN
#include <Python.h>

#if !defined(_WIN32) && defined(KPIP_UNZIP_ZLIB)
#define KPIP_UNZIP_LOOP 1
#endif

#ifdef KPIP_UNZIP_LOOP

#include <errno.h>
#include <fcntl.h>
#include <stdint.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include <zlib.h>

#ifndef O_CLOEXEC
#define O_CLOEXEC 0
#endif

#define LOCAL_HEADER 30
#define STORED 0
#define DEFLATED 8

typedef struct {
    long long offset;
    long long compressed_size;
    long long size;
    unsigned long crc;
    int method;
    unsigned int mode;
    const char *name;
    Py_ssize_t name_length;
    const char *destination;
} Member;

/* Read exactly length bytes at offset; 0 on success. */
static int
read_at(int descriptor, unsigned char *buffer, size_t length, long long offset)
{
    size_t done = 0;
    while (done < length) {
        ssize_t got = pread(descriptor, buffer + done, length - done, (off_t)(offset + done));
        if (got < 0) {
            if (errno == EINTR) {
                continue;
            }
            return -1;
        }
        if (got == 0) {
            return -1;
        }
        done += (size_t)got;
    }
    return 0;
}

/* Inflate a raw deflate stream into exactly size bytes; 0 on success. */
static int
inflate_exactly(const unsigned char *input, size_t input_length, unsigned char *output, size_t size)
{
    z_stream stream;
    memset(&stream, 0, sizeof stream);
    if (inflateInit2(&stream, -15) != Z_OK) {
        return -1;
    }
    stream.next_in = (unsigned char *)input;
    stream.avail_in = (uInt)input_length;
    stream.next_out = output;
    stream.avail_out = (uInt)size;
    int status = inflate(&stream, Z_FINISH);
    size_t produced = stream.total_out;
    inflateEnd(&stream);
    return (status == Z_STREAM_END && produced == size) ? 0 : -1;
}

/* Write all of data to a new file at path; 0 on success. */
static int
write_file(const char *path, const unsigned char *data, size_t length, unsigned int mode)
{
    int descriptor = open(path, O_WRONLY | O_CREAT | O_TRUNC | O_CLOEXEC, (mode_t)mode);
    if (descriptor < 0) {
        return -1;
    }
    size_t done = 0;
    while (done < length) {
        ssize_t wrote = write(descriptor, data + done, length - done);
        if (wrote < 0) {
            if (errno == EINTR) {
                continue;
            }
            close(descriptor);
            return -1;
        }
        done += (size_t)wrote;
    }
    return close(descriptor) == 0 ? 0 : -1;
}

/* Extract one member; 0 on success, -1 to leave it to Python. */
static int
extract_one(int descriptor, const Member *member)
{
    if (member->method != STORED && member->method != DEFLATED) {
        return -1;
    }
    unsigned char header[LOCAL_HEADER];
    if (read_at(descriptor, header, LOCAL_HEADER, member->offset) != 0
        || memcmp(header, "PK\x03\x04", 4) != 0) {
        return -1;
    }
    size_t name_length = header[26] | (header[27] << 8);
    size_t extra_length = header[28] | (header[29] << 8);
    if ((Py_ssize_t)name_length != member->name_length) {
        return -1;
    }
    size_t compressed = (size_t)member->compressed_size;
    size_t size = (size_t)member->size;
    size_t span = name_length + extra_length + compressed;
    unsigned char *input = malloc(span ? span : 1);
    unsigned char *output = NULL;
    int result = -1;
    if (input == NULL) {
        return -1;
    }
    if (read_at(descriptor, input, span, member->offset + LOCAL_HEADER) != 0
        || memcmp(input, member->name, name_length) != 0) {
        goto done;
    }
    const unsigned char *data = input + name_length + extra_length;
    if (member->method == DEFLATED) {
        output = malloc(size ? size : 1);
        if (output == NULL || inflate_exactly(data, compressed, output, size) != 0) {
            goto done;
        }
        data = output;
    }
    else if (compressed != size) {
        goto done;
    }
    if ((crc32(0L, data, (uInt)size) & 0xffffffffUL) != (member->crc & 0xffffffffUL)) {
        goto done;
    }
    result = write_file(member->destination, data, size, member->mode);

done:
    free(input);
    free(output);
    return result;
}

PyDoc_STRVAR(extract_members_doc,
"extract_members(fd, members, start) -> (status, index)\n\n"
"Extract each of members from start on, from the wheel open at fd. A member\n"
"is (offset, method, compressed_size, size, crc, name, destination, mode):\n"
"its local header's offset, the name its local header must hold, as bytes,\n"
"and the path and mode to create. (0, len(members)) when all are written,\n"
"else (1, index) of the first left to the caller.");

static PyObject *
extract_members(PyObject *module, PyObject *args)
{
    int descriptor;
    PyObject *members;
    Py_ssize_t start;
    if (!PyArg_ParseTuple(args, "iOn", &descriptor, &members, &start)) {
        return NULL;
    }
    PyObject *held = PySequence_Tuple(members);
    if (held == NULL) {
        return NULL;
    }
    Py_ssize_t count = PyTuple_GET_SIZE(held);
    if (start < 0) {
        start = 0;
    }
    if (start > count) {
        start = count;
    }
    Member *table = PyMem_New(Member, count ? count : 1);
    if (table == NULL) {
        Py_DECREF(held);
        return PyErr_NoMemory();
    }
    for (Py_ssize_t index = start; index < count; index++) {
        PyObject *item = PyTuple_GET_ITEM(held, index);
        PyObject *name, *destination;
        Member *member = &table[index];
        if (!PyArg_ParseTuple(item, "LiLLkSSI;member must be (offset, method, compressed_size, size, crc, name, destination, mode)",
                              &member->offset, &member->method, &member->compressed_size,
                              &member->size, &member->crc, &name, &destination, &member->mode)) {
            PyMem_Free(table);
            Py_DECREF(held);
            return NULL;
        }
        member->name = PyBytes_AS_STRING(name);
        member->name_length = PyBytes_GET_SIZE(name);
        member->destination = PyBytes_AS_STRING(destination);
        if (memchr(member->destination, '\0', PyBytes_GET_SIZE(destination)) != NULL
            || member->offset < 0 || member->compressed_size < 0 || member->size < 0) {
            member->method = -1;
        }
    }
    Py_ssize_t index;
    int status = 0;
    Py_BEGIN_ALLOW_THREADS
    for (index = start; index < count; index++) {
        if (extract_one(descriptor, &table[index]) != 0) {
            status = 1;
            break;
        }
    }
    Py_END_ALLOW_THREADS
    PyMem_Free(table);
    Py_DECREF(held);
    return Py_BuildValue("(in)", status, index);
}

#endif

static PyMethodDef methods[] = {
#ifdef KPIP_UNZIP_LOOP
    {"extract_members", extract_members, METH_VARARGS, extract_members_doc},
#endif
    {NULL, NULL, 0, NULL},
};

static PyModuleDef_Slot slots[] = {
    {Py_mod_multiple_interpreters, Py_MOD_PER_INTERPRETER_GIL_SUPPORTED},
    {Py_mod_gil, Py_MOD_GIL_NOT_USED},
    {0, NULL},
};

static struct PyModuleDef module = {
    PyModuleDef_HEAD_INIT,
    .m_name = "_kpip_unzip",
    .m_doc = "The wheel member extraction loop of kpip's archive cache, in C.",
    .m_size = 0,
    .m_methods = methods,
    .m_slots = slots,
};

PyMODINIT_FUNC
PyInit__kpip_unzip(void)
{
    return PyModuleDef_Init(&module);
}

#ifdef KPIP_UNZIP_BUILTIN
/* Built into the binary: registered before main, as _kpip_link_tree.c
 * explains. */
static void
register_builtin(void)
{
    PyImport_AppendInittab("_kpip_unzip", PyInit__kpip_unzip);
}

#ifdef _MSC_VER
#ifdef _WIN64
#define KPIP_SYMBOL_PREFIX ""
#else
#define KPIP_SYMBOL_PREFIX "_"
#endif
#pragma section(".CRT$XCU", read)
__declspec(allocate(".CRT$XCU")) void(__cdecl *kpip_unzip_register)(void) = register_builtin;
#pragma comment(linker, "/include:" KPIP_SYMBOL_PREFIX "kpip_unzip_register")
#else
__attribute__((constructor)) static void
register_builtin_constructor(void)
{
    register_builtin();
}
#endif
#endif
