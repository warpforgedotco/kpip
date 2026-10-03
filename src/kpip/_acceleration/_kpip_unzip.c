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
#include <sys/stat.h>
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

/* --- unpack: a whole wheel, its directory read and its paths checked here --- */

#define CENTRAL_HEADER 46
#define END_RECORD 22
#define ZIP64_LOCATOR 20
#define ZIP64_END_RECORD 56
#define ENCRYPTED 0x1
#define UTF8_NAME 0x800

typedef struct {
    const unsigned char *name;
    size_t name_length;
    unsigned int flags;
    int method;
    unsigned long crc;
    long long compressed_size;
    long long size;
    long long offset;
    unsigned long external;
    char *relative;
    size_t relative_length;
} Entry;

static unsigned int
le16(const unsigned char *p)
{
    return (unsigned int)p[0] | ((unsigned int)p[1] << 8);
}

static unsigned long
le32(const unsigned char *p)
{
    return (unsigned long)p[0] | ((unsigned long)p[1] << 8) | ((unsigned long)p[2] << 16)
           | ((unsigned long)p[3] << 24);
}

static unsigned long long
le64(const unsigned char *p)
{
    return (unsigned long long)le32(p) | ((unsigned long long)le32(p + 4) << 32);
}

/* The central directory: its bytes, in *directory, and its entry count.
 * 0 on success; -1 for anything zipfile would read differently, which the
 * caller leaves to it. */
static int
read_directory(int descriptor, unsigned char **directory, size_t *directory_length,
               unsigned long long *count)
{
    struct stat info;
    if (fstat(descriptor, &info) != 0 || info.st_size < END_RECORD) {
        return -1;
    }
    long long size = (long long)info.st_size;
    size_t tail_length = (size_t)(size < 65535 + END_RECORD ? size : 65535 + END_RECORD);
    unsigned char *tail = malloc(tail_length);
    if (tail == NULL || read_at(descriptor, tail, tail_length, size - (long long)tail_length) != 0) {
        free(tail);
        return -1;
    }
    /* The end record, its comment running to the end of the file. */
    long long end = -1;
    for (long long at = (long long)tail_length - END_RECORD; at >= 0; at--) {
        const unsigned char *p = tail + at;
        if (memcmp(p, "PK\x05\x06", 4) == 0
            && (long long)le16(p + 20) == (long long)tail_length - at - END_RECORD) {
            end = at;
            break;
        }
    }
    if (end < 0) {
        free(tail);
        return -1;
    }
    const unsigned char *record = tail + end;
    long long record_offset = size - (long long)tail_length + end;
    unsigned long long entries = le16(record + 10);
    unsigned long long directory_size = le32(record + 12);
    unsigned long long directory_offset = le32(record + 16);
    long long directory_end = record_offset;
    if (le16(record + 4) != 0 || le16(record + 6) != 0) {
        free(tail);
        return -1;
    }
    if (entries == 0xffff || directory_size == 0xffffffffUL || directory_offset == 0xffffffffUL) {
        unsigned char locator[ZIP64_LOCATOR], record64[ZIP64_END_RECORD];
        if (record_offset < ZIP64_LOCATOR
            || read_at(descriptor, locator, ZIP64_LOCATOR, record_offset - ZIP64_LOCATOR) != 0
            || memcmp(locator, "PK\x06\x07", 4) != 0) {
            free(tail);
            return -1;
        }
        long long record64_offset = (long long)le64(locator + 8);
        if (record64_offset != record_offset - ZIP64_LOCATOR - ZIP64_END_RECORD
            || read_at(descriptor, record64, ZIP64_END_RECORD, record64_offset) != 0
            || memcmp(record64, "PK\x06\x06", 4) != 0) {
            free(tail);
            return -1;
        }
        entries = le64(record64 + 32);
        directory_size = le64(record64 + 40);
        directory_offset = le64(record64 + 48);
        directory_end = record64_offset;
    }
    free(tail);
    /* Data before the archive moves every offset; zipfile allows for it, and
     * a wheel never has any. */
    if ((long long)(directory_offset + directory_size) != directory_end
        || directory_size > (unsigned long long)size) {
        return -1;
    }
    unsigned char *bytes = malloc(directory_size ? (size_t)directory_size : 1);
    if (bytes == NULL
        || read_at(descriptor, bytes, (size_t)directory_size, (long long)directory_offset) != 0) {
        free(bytes);
        return -1;
    }
    *directory = bytes;
    *directory_length = (size_t)directory_size;
    *count = entries;
    return 0;
}

/* A file entry's path as validate_member_parts makes it, '/'-joined, into
 * entry->relative; -1 for a name it would refuse, or one zipfile reads
 * otherwise: Python then refuses it as it does. */
static int
relative_path(Entry *entry)
{
    const unsigned char *name = entry->name;
    size_t length = entry->name_length;
    if (length == 0 || name[0] == '/' || memchr(name, '\\', length) != NULL
        || memchr(name, '\0', length) != NULL) {
        return -1;
    }
    if (!(entry->flags & UTF8_NAME)) {
        for (size_t i = 0; i < length; i++) {
            if (name[i] >= 0x80) {
                return -1;
            }
        }
    }
    char *out = malloc(length + 1);
    if (out == NULL) {
        return -1;
    }
    size_t used = 0, start = 0;
    while (start <= length) {
        size_t stop = start;
        while (stop < length && name[stop] != '/') {
            stop++;
        }
        size_t part = stop - start;
        if (part == 2 && name[start] == '.' && name[start + 1] == '.') {
            free(out);
            return -1;
        }
        if (part > 0 && !(part == 1 && name[start] == '.')) {
            if (used > 0) {
                out[used++] = '/';
            }
            memcpy(out + used, name + start, part);
            used += part;
        }
        start = stop + 1;
    }
    if (used == 0) {
        free(out);
        return -1;
    }
    out[used] = '\0';
    entry->relative = out;
    entry->relative_length = used;
    return 0;
}

static int
by_relative(const void *left, const void *right)
{
    const Entry *a = *(const Entry *const *)left, *b = *(const Entry *const *)right;
    return strcmp(a->relative, b->relative);
}

/* The parent directories of every file, made under the tree once each:
 * entries are in archive order, which keeps a directory's files together,
 * so only what differs from the last entry's parent is made. */
static int
make_parents(int tree, Entry *entries, size_t count)
{
    const char *previous = "";
    size_t previous_length = 0;
    for (size_t index = 0; index < count; index++) {
        Entry *entry = &entries[index];
        const char *slash = strrchr(entry->relative, '/');
        if (slash == NULL) {
            continue;
        }
        size_t parent_length = (size_t)(slash - entry->relative);
        if (parent_length == previous_length
            && memcmp(entry->relative, previous, parent_length) == 0) {
            continue;
        }
        /* The components shared with the last parent exist already. */
        size_t shared = 0;
        size_t limit = parent_length < previous_length ? parent_length : previous_length;
        for (size_t i = 0; i <= limit; i++) {
            int left_end = i == parent_length || entry->relative[i] == '/';
            int right_end = i == previous_length || previous[i] == '/';
            if (left_end && right_end) {
                shared = i;
            }
            if (i == limit || entry->relative[i] != previous[i]) {
                break;
            }
        }
        char path[4096];
        if (parent_length >= sizeof path) {
            return -1;
        }
        memcpy(path, entry->relative, parent_length);
        path[parent_length] = '\0';
        for (size_t i = shared ? shared + 1 : 0; i <= parent_length; i++) {
            if (i == parent_length || path[i] == '/') {
                char kept = path[i];
                path[i] = '\0';
                if (mkdirat(tree, path, 0777) != 0 && errno != EEXIST) {
                    return -1;
                }
                path[i] = kept;
            }
        }
        previous = entry->relative;
        previous_length = parent_length;
    }
    return 0;
}

/* Extract one entry under the tree; 0 on success. */
static int
unpack_one(int descriptor, int tree, const Entry *entry, long long limit)
{
    if ((entry->method != STORED && entry->method != DEFLATED) || entry->flags & ENCRYPTED
        || entry->size > limit || entry->compressed_size > limit || entry->offset < 0) {
        return -1;
    }
    unsigned char header[LOCAL_HEADER];
    if (read_at(descriptor, header, LOCAL_HEADER, entry->offset) != 0
        || memcmp(header, "PK\x03\x04", 4) != 0) {
        return -1;
    }
    size_t name_length = le16(header + 26);
    size_t extra_length = le16(header + 28);
    if (name_length != entry->name_length) {
        return -1;
    }
    size_t compressed = (size_t)entry->compressed_size;
    size_t size = (size_t)entry->size;
    size_t span = name_length + extra_length + compressed;
    unsigned char *input = malloc(span ? span : 1);
    unsigned char *output = NULL;
    int result = -1;
    if (input == NULL) {
        return -1;
    }
    if (read_at(descriptor, input, span, entry->offset + LOCAL_HEADER) != 0
        || memcmp(input, entry->name, name_length) != 0) {
        goto done;
    }
    const unsigned char *data = input + name_length + extra_length;
    if (entry->method == DEFLATED) {
        output = malloc(size ? size : 1);
        if (output == NULL || inflate_exactly(data, compressed, output, size) != 0) {
            goto done;
        }
        data = output;
    }
    else if (compressed != size) {
        goto done;
    }
    if ((crc32(0L, data, (uInt)size) & 0xffffffffUL) != (entry->crc & 0xffffffffUL)) {
        goto done;
    }
    unsigned long mode = entry->external >> 16;
    int executable = mode != 0 && S_ISREG(mode) && (mode & 0111);
    int out = openat(tree, entry->relative, O_WRONLY | O_CREAT | O_TRUNC | O_CLOEXEC,
                     executable ? 0777 : 0666);
    if (out < 0) {
        goto done;
    }
    size_t written = 0;
    while (written < size) {
        ssize_t wrote = write(out, data + written, size - written);
        if (wrote < 0) {
            if (errno == EINTR) {
                continue;
            }
            close(out);
            goto done;
        }
        written += (size_t)wrote;
    }
    result = close(out) == 0 ? 0 : -1;

done:
    free(input);
    free(output);
    return result;
}

/* Read the directory, check every path, make the directories and extract
 * every file: 0 when all are written, -1 to leave the wheel to Python. */
static int
unpack_all(int descriptor, const char *tree_path, long long limit, Entry **entries_out,
           size_t *count_out, unsigned char **directory_out)
{
    unsigned char *directory = NULL;
    size_t directory_length = 0;
    unsigned long long declared = 0;
    if (read_directory(descriptor, &directory, &directory_length, &declared) != 0) {
        return -1;
    }
    if (declared > directory_length / CENTRAL_HEADER) {
        free(directory);
        return -1;
    }
    Entry *entries = calloc(declared ? (size_t)declared : 1, sizeof(Entry));
    if (entries == NULL) {
        free(directory);
        return -1;
    }
    size_t count = 0, at = 0;
    int failed = 0;
    for (unsigned long long index = 0; index < declared && !failed; index++) {
        if (at + CENTRAL_HEADER > directory_length
            || memcmp(directory + at, "PK\x01\x02", 4) != 0) {
            failed = 1;
            break;
        }
        const unsigned char *p = directory + at;
        size_t name_length = le16(p + 28), extra_length = le16(p + 30), comment_length = le16(p + 32);
        if (at + CENTRAL_HEADER + name_length + extra_length + comment_length > directory_length) {
            failed = 1;
            break;
        }
        Entry entry = {0};
        entry.flags = le16(p + 8);
        entry.method = (int)le16(p + 10);
        entry.crc = le32(p + 16);
        entry.compressed_size = (long long)le32(p + 20);
        entry.size = (long long)le32(p + 24);
        entry.external = le32(p + 38);
        entry.offset = (long long)le32(p + 42);
        entry.name = p + CENTRAL_HEADER;
        entry.name_length = name_length;
        /* zip64 sizes and offset, in that order, for the fields that need them */
        if (entry.size == 0xffffffffLL || entry.compressed_size == 0xffffffffLL
            || entry.offset == 0xffffffffLL) {
            const unsigned char *extra = p + CENTRAL_HEADER + name_length;
            const unsigned char *extra_end = extra + extra_length;
            int found = 0;
            while (extra + 4 <= extra_end) {
                unsigned int id = le16(extra), length = le16(extra + 2);
                if (extra + 4 + length > extra_end) {
                    break;
                }
                if (id == 0x0001) {
                    const unsigned char *field = extra + 4, *field_end = extra + 4 + length;
                    if (entry.size == 0xffffffffLL) {
                        if (field + 8 > field_end) break;
                        entry.size = (long long)le64(field);
                        field += 8;
                    }
                    if (entry.compressed_size == 0xffffffffLL) {
                        if (field + 8 > field_end) break;
                        entry.compressed_size = (long long)le64(field);
                        field += 8;
                    }
                    if (entry.offset == 0xffffffffLL) {
                        if (field + 8 > field_end) break;
                        entry.offset = (long long)le64(field);
                    }
                    found = 1;
                    break;
                }
                extra += 4 + length;
            }
            if (!found) {
                failed = 1;
                break;
            }
        }
        at += CENTRAL_HEADER + name_length + extra_length + comment_length;
        /* A directory entry makes nothing; its files make it. */
        if (name_length > 0 && entry.name[name_length - 1] == '/') {
            continue;
        }
        if (relative_path(&entry) != 0) {
            failed = 1;
            break;
        }
        entries[count++] = entry;
    }
    /* Every path is checked, and no two files share one, before any is
     * written. */
    if (!failed && count > 1) {
        Entry **sorted = malloc(count * sizeof(Entry *));
        if (sorted == NULL) {
            failed = 1;
        }
        else {
            for (size_t i = 0; i < count; i++) {
                sorted[i] = &entries[i];
            }
            qsort(sorted, count, sizeof(Entry *), by_relative);
            for (size_t i = 1; i < count && !failed; i++) {
                if (strcmp(sorted[i - 1]->relative, sorted[i]->relative) == 0) {
                    failed = 1;
                }
            }
            free(sorted);
        }
    }
    int tree = failed ? -1 : open(tree_path, O_RDONLY | O_DIRECTORY | O_CLOEXEC);
    if (tree < 0 || make_parents(tree, entries, count) != 0) {
        failed = 1;
    }
    for (size_t i = 0; i < count && !failed; i++) {
        if (unpack_one(descriptor, tree, &entries[i], limit) != 0) {
            failed = 1;
        }
    }
    if (tree >= 0) {
        close(tree);
    }
    *entries_out = entries;
    *count_out = count;
    *directory_out = directory;
    return failed ? -1 : 0;
}

static void
free_entries(Entry *entries, size_t count, unsigned char *directory)
{
    for (size_t i = 0; i < count; i++) {
        free(entries[i].relative);
    }
    free(entries);
    free(directory);
}

PyDoc_STRVAR(unpack_doc,
"unpack(fd, tree, limit) -> list | None\n\n"
"Extract every file of the wheel open at fd under the directory tree, whose\n"
"members are each at most limit bytes: its central directory read, every\n"
"path checked as validate_member_parts checks it and no two the same, the\n"
"directories made, and each member checked and written, with the\n"
"interpreter lock released. A list of (name, relative, size, mode) per\n"
"file, in the archive's order -- its name in the archive, its checked path,\n"
"and its mode in the archive, 0 for none -- or None for a wheel left to the\n"
"caller, which extracts it again and raises what that raises.");

static PyObject *
unpack(PyObject *module, PyObject *args)
{
    int descriptor;
    PyObject *tree_object;
    long long limit;
    if (!PyArg_ParseTuple(args, "iO&L", &descriptor, PyUnicode_FSConverter, &tree_object, &limit)) {
        return NULL;
    }
    Entry *entries = NULL;
    size_t count = 0;
    unsigned char *directory = NULL;
    int status;
    const char *tree_path = PyBytes_AS_STRING(tree_object);
    Py_BEGIN_ALLOW_THREADS
    status = unpack_all(descriptor, tree_path, limit, &entries, &count, &directory);
    Py_END_ALLOW_THREADS
    Py_DECREF(tree_object);
    if (status != 0) {
        if (entries != NULL) {
            free_entries(entries, count, directory);
        }
        Py_RETURN_NONE;
    }
    PyObject *result = PyList_New((Py_ssize_t)count);
    for (size_t i = 0; result != NULL && i < count; i++) {
        Entry *entry = &entries[i];
        PyObject *name = PyUnicode_DecodeUTF8((const char *)entry->name,
                                              (Py_ssize_t)entry->name_length, "strict");
        PyObject *relative = name == NULL ? NULL
            : PyUnicode_DecodeUTF8(entry->relative, (Py_ssize_t)entry->relative_length, "strict");
        unsigned long mode = entry->external >> 16;
        PyObject *row = relative == NULL ? NULL
            : Py_BuildValue("(OOLk)", name, relative, entry->size,
                            mode != 0 && S_ISREG(mode) ? mode : 0UL);
        Py_XDECREF(name);
        Py_XDECREF(relative);
        if (row == NULL) {
            Py_CLEAR(result);
            break;
        }
        PyList_SET_ITEM(result, (Py_ssize_t)i, row);
    }
    free_entries(entries, count, directory);
    if (result == NULL && PyErr_ExceptionMatches(PyExc_UnicodeDecodeError)) {
        PyErr_Clear();
        Py_RETURN_NONE;
    }
    return result;
}

#endif

static PyMethodDef methods[] = {
#ifdef KPIP_UNZIP_LOOP
    {"extract_members", extract_members, METH_VARARGS, extract_members_doc},
    {"unpack", unpack, METH_VARARGS, unpack_doc},
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
