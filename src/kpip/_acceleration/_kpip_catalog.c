/* kpip.index.page_parsing's catalog compilation for a JSON index page, in C.
 *
 * kpip-compile compiles this into the binary as the built-in _kpip_catalog
 * (KPIP_CATALOG_BUILTIN). compile_page takes a page's decoded file entries
 * and returns (groups, unparsed) as IndexPageParser.catalog_from_json does:
 * for a file at a plain absolute URL it builds the record and finds the
 * release here, and every other file goes to the caller's fallback, which
 * compiles it as Python does. earliest_upload_text answers what
 * catalog_cache.earliest_upload's fast path asks.
 */

#define PY_SSIZE_T_CLEAN
#include <Python.h>
#include <string.h>

static PyObject *s_url, *s_filename, *s_yanked, *s_hashes, *s_requires_python,
    *s_upload_time, *s_size, *s_core_metadata, *s_dist_info_metadata, *s_sha256, *s_public, *zero;

static int is_ascii_alnum(char c) { return (c >= '0' && c <= '9') || (c >= 'a' && c <= 'z') || (c >= 'A' && c <= 'Z'); }

/* join_index_url's fast path: an absolute http(s) URL it returns unchanged. */
static int
joins_to_itself(const char *u, Py_ssize_t n)
{
    Py_ssize_t start;
    if (n >= 8 && memcmp(u, "https://", 8) == 0) start = 8;
    else if (n >= 7 && memcmp(u, "http://", 7) == 0) start = 7;
    else return 0;
    char last = u[n - 1];
    if (last == ';' || last == '?' || last == '#') return 0;
    for (Py_ssize_t i = 0; i < n; i++) {
        unsigned char c = (unsigned char)u[i];
        if (c >= 0x7f || c < 0x20) return 0; /* ascii and printable */
        if (c == '[' || c == ']') return 0;
        if (i + 1 < n) {
            char d = u[i + 1];
            if ((c == '?' && d == '#') || (c == ';' && (d == '?' || d == '#'))) return 0;
        }
    }
    if (start < n && (u[start] == '/' || u[start] == '?' || u[start] == '#')) return 0;
    return 1;
}

/* _PLAIN_URL: (https?)://([A-Za-z0-9.\-_:]+)(/[^\s#?\\\[\]]*)?\Z, no '&';
 * the path's start in *path, or -1 for none. */
static int
is_plain(const char *u, Py_ssize_t n, Py_ssize_t *path)
{
    Py_ssize_t p = (u[4] == 's') ? 8 : 7;
    Py_ssize_t host = p;
    while (p < n && (is_ascii_alnum(u[p]) || u[p] == '.' || u[p] == '-' || u[p] == '_' || u[p] == ':')) p++;
    if (p == host) return 0;
    *path = -1;
    if (p < n) {
        if (u[p] != '/') return 0;
        *path = p;
        for (Py_ssize_t i = p; i < n; i++) {
            char c = u[i];
            if (c == ' ' || c == '#' || c == '?' || c == '\\' || c == '[' || c == ']' || c == '&') return 0;
        }
    }
    for (Py_ssize_t i = 0; i < n; i++) {
        if (u[i] == '&') return 0;
    }
    return 1;
}

/* _is_escaped_name for an ASCII field. */
static int
escaped_name(const char *f, Py_ssize_t n)
{
    if (n == 0) return 0;
    int all_alnum = 1, any_kept = 0;
    for (Py_ssize_t i = 0; i < n; i++) {
        if (i + 1 < n && f[i] == '_' && f[i + 1] == '_') return 0;
        if (!is_ascii_alnum(f[i])) all_alnum = 0;
    }
    if (all_alnum) return 1;
    for (Py_ssize_t i = 0; i < n; i++) {
        if (f[i] == '.' || f[i] == '_') continue;
        if (!is_ascii_alnum(f[i])) return 0;
        any_kept = 1;
    }
    return any_kept;
}

/* canonicalize_name: lower-cased, each run of -_. one '-'. */
static PyObject *
canonical(const char *f, Py_ssize_t n)
{
    char buffer[512];
    if (n >= (Py_ssize_t)sizeof buffer) return NULL;
    Py_ssize_t m = 0;
    for (Py_ssize_t i = 0; i < n; i++) {
        char c = f[i];
        if (c == '-' || c == '_' || c == '.') {
            /* a run collapses to one '-' */
            if (m == 0 || buffer[m - 1] != '-') buffer[m++] = '-';
        }
        else {
            buffer[m++] = (c >= 'A' && c <= 'Z') ? (char)(c + 32) : c;
        }
    }
    return PyUnicode_FromStringAndSize(buffer, m);
}

/* What json_record stores of a hashes or metadata mapping: its lone sha256
 * as the digest; NULL with no error to leave the entry to Python. */
static PyObject *
lone_sha256(PyObject *mapping)
{
    if (PyDict_GET_SIZE(mapping) != 1) return NULL;
    PyObject *digest = PyDict_GetItemWithError(mapping, s_sha256);
    if (digest == NULL || !PyUnicode_CheckExact(digest)) return NULL;
    return Py_NewRef(digest);
}

#define WHEEL 1
#define SDIST 2
#define OTHER 0

static const char *SDIST_SUFFIXES[] = {".tar.gz", ".tar.bz2", ".tar.xz", ".tar.lzma", ".tgz", ".zip"};

static int
ends_with(const char *s, Py_ssize_t n, const char *suffix)
{
    Py_ssize_t m = (Py_ssize_t)strlen(suffix);
    return n >= m && memcmp(s + n - m, suffix, m) == 0;
}

/* Link.artifact_kind_from_filename, as its records need it. */
static int
artifact_kind(const char *tail, Py_ssize_t n)
{
    if (ends_with(tail, n, ".whl")) return WHEEL;
    if (ends_with(tail, n, ".metadata") || ends_with(tail, n, ".attestation")) return OTHER;
    for (int i = 0; i < 6; i++) if (ends_with(tail, n, SDIST_SUFFIXES[i])) return SDIST;
    return OTHER;
}

/* identity_for an sdist: the version starts after the last hyphen that
 * leaves one, as project_version_from_filename finds it. */
static PyObject *
sdist_identity(const char *tail, Py_ssize_t n, PyObject *version_type, PyObject *record)
{
    Py_ssize_t stem = n;
    for (int i = 0; i < 6; i++) {
        if (ends_with(tail, n, SDIST_SUFFIXES[i])) { stem = n - (Py_ssize_t)strlen(SDIST_SUFFIXES[i]); break; }
    }
    Py_ssize_t end = stem;
    for (;;) {
        Py_ssize_t sep = -1;
        for (Py_ssize_t i = end - 1; i > 0; i--) if (tail[i] == '-') { sep = i; break; }
        if (sep <= 0) break;
        PyObject *text = PyUnicode_FromStringAndSize(tail + sep + 1, stem - sep - 1);
        if (text == NULL) return NULL;
        PyObject *version = PyObject_CallOneArg(version_type, text);
        Py_DECREF(text);
        if (version == NULL) {
            if (!PyErr_ExceptionMatches(PyExc_ValueError)) return NULL;
            PyErr_Clear();
            end = sep;
            continue;
        }
        PyObject *version_text = PyObject_Str(version);
        Py_DECREF(version);
        if (version_text == NULL) return NULL;
        PyObject *name = canonical(tail, sep);
        if (name == NULL) { Py_DECREF(version_text); if (PyErr_Occurred()) return NULL; return Py_NewRef(Py_None); }
        PyObject *kind = PyLong_FromLong(SDIST);
        PyObject *result = kind ? PyTuple_Pack(4, kind, name, version_text, record) : NULL;
        Py_XDECREF(kind);
        Py_DECREF(name);
        Py_DECREF(version_text);
        return result;
    }
    return PyTuple_Pack(4, zero, Py_None, Py_None, record);
}

static PyObject *
public_version(PyObject *versions, PyObject *version_type, const char *text, Py_ssize_t length)
{
    /* The public form of a wheel's version, made once per text on a page;
     * NULL with ValueError for one that does not parse. */
    PyObject *key = PyUnicode_FromStringAndSize(text, length);
    if (key == NULL) return NULL;
    PyObject *known = versions == NULL ? NULL : PyDict_GetItemWithError(versions, key);
    if (known != NULL) { Py_DECREF(key); return Py_NewRef(known); }
    if (PyErr_Occurred()) { Py_DECREF(key); return NULL; }
    PyObject *version = PyObject_CallOneArg(version_type, key);
    if (version == NULL) { Py_DECREF(key); return NULL; }
    PyObject *public = PyObject_GetAttr(version, s_public);
    Py_DECREF(version);
    if (public != NULL && versions != NULL && PyDict_SetItem(versions, key, public) < 0) Py_CLEAR(public);
    Py_DECREF(key);
    return public;
}

/* A pinned release's filter: keep_url(url) for a file whose name this does
 * not read a version from, and keep_version(text) for a wheel's version
 * field, asked once per text and remembered in kept. */
typedef struct {
    PyObject *keep_url;
    PyObject *keep_version;
    PyObject *kept;
} Filter;

/* Whether the filter keeps the file at url, whose last segment is tail: 1,
 * 0, or -1 with an exception. A wheel's version is its name's second field
 * when it has five or six, as release_filter reads it. */
static int
filter_keeps(const Filter *filter, PyObject *url, const char *tail, Py_ssize_t n, int kind)
{
    if (filter == NULL || filter->keep_version == NULL) {
        return 1;
    }
    if (kind == WHEEL) {
        Py_ssize_t stem = n - 4, fields = 1, start = -1, end = -1;
        for (Py_ssize_t i = 0; i < stem; i++) {
            if (tail[i] == '-') {
                fields++;
                if (fields == 2) start = i + 1;
                else if (fields == 3) end = i;
            }
        }
        if (fields != 5 && fields != 6) {
            return 1;
        }
        PyObject *text = PyUnicode_FromStringAndSize(tail + start, end - start);
        if (text == NULL) return -1;
        PyObject *known = PyDict_GetItemWithError(filter->kept, text);
        if (known == NULL) {
            if (PyErr_Occurred()) { Py_DECREF(text); return -1; }
            known = PyObject_CallOneArg(filter->keep_version, text);
            if (known == NULL || PyDict_SetItem(filter->kept, text, known) < 0) {
                Py_XDECREF(known);
                Py_DECREF(text);
                return -1;
            }
            Py_DECREF(known);
        }
        Py_DECREF(text);
        return PyObject_IsTrue(known);
    }
    PyObject *kept = PyObject_CallOneArg(filter->keep_url, url);
    if (kept == NULL) return -1;
    int truth = PyObject_IsTrue(kept);
    Py_DECREF(kept);
    return truth;
}

/* One entry: (kind, name, version, record), (0, None, None, record) for a
 * file whose release does not parse, Py_False for one the filter drops, or
 * None to leave it to the fallback. */
static PyObject *
compile_entry(PyObject *entry, PyObject *unset, PyObject *version_type, PyObject *wheel_kind,
              PyObject *versions, const Filter *filter)
{
    PyObject *url = NULL, *filename = NULL, *yanked = NULL, *hashes = NULL, *rp = NULL,
             *upload = NULL, *size = NULL, *metadata = NULL, *text = NULL, *stored_hashes = NULL,
             *yanked_reason = NULL, *stored_metadata = NULL, *record = NULL, *result = NULL;

    url = PyObject_GetAttr(entry, s_url);
    if (url == NULL) goto error;
    if (!PyUnicode_CheckExact(url) || !PyUnicode_IS_ASCII(url)) goto fallback;
    Py_ssize_t n = PyUnicode_GET_LENGTH(url);
    const char *u = (const char *)PyUnicode_1BYTE_DATA(url);
    Py_ssize_t path;
    if (n == 0 || !joins_to_itself(u, n) || !is_plain(u, n, &path) || path < 0) goto fallback;
    for (Py_ssize_t i = path; i < n; i++) if (u[i] == '%') goto fallback;
    Py_ssize_t end = n;
    while (end > path && u[end - 1] == '/') end--;
    Py_ssize_t start = end;
    while (start > path && u[start - 1] != '/') start--;
    const char *tail = u + start;
    Py_ssize_t tail_length = end - start;
    if (tail_length == 0 || (tail_length == 1 && tail[0] == '.') || (tail_length == 2 && tail[0] == '.' && tail[1] == '.')) goto fallback;
    int kind = artifact_kind(tail, tail_length);
    int keeps = filter_keeps(filter, url, tail, tail_length, kind);
    if (keeps < 0) goto error;
    if (!keeps) {
        result = Py_NewRef(Py_False);
        goto done;
    }

    /* json_record's fields */
    filename = PyObject_GetAttr(entry, s_filename);
    if (filename == NULL) goto error;
    int truthy = PyObject_IsTrue(filename);
    if (truthy < 0) goto error;
    if (!truthy) text = PyUnicode_FromStringAndSize("", 0);
    else if (PyUnicode_CheckExact(filename)) text = Py_NewRef(filename);
    else goto fallback;
    if (text == NULL) goto error;

    yanked = PyObject_GetAttr(entry, s_yanked);
    if (yanked == NULL) goto error;
    if (yanked == Py_False || yanked == Py_None) yanked_reason = Py_NewRef(Py_None);
    else if (yanked == Py_True) yanked_reason = PyUnicode_FromStringAndSize("", 0);
    else if (PyUnicode_CheckExact(yanked)) yanked_reason = Py_NewRef(yanked);
    else goto fallback;

    hashes = PyObject_GetAttr(entry, s_hashes);
    if (hashes == NULL) goto error;
    if (PyDict_CheckExact(hashes)) {
        stored_hashes = lone_sha256(hashes);
        if (stored_hashes == NULL) { if (PyErr_Occurred()) goto error; goto fallback; }
    }
    else if (PyDict_Check(hashes)) goto fallback;
    else stored_hashes = PyDict_New();

    rp = PyObject_GetAttr(entry, s_requires_python);
    if (rp == NULL) goto error;
    upload = PyObject_GetAttr(entry, s_upload_time);
    if (upload == NULL) goto error;
    size = PyObject_GetAttr(entry, s_size);
    if (size == NULL) goto error;

    metadata = PyObject_GetAttr(entry, s_core_metadata);
    if (metadata == NULL) goto error;
    if (metadata == unset) {
        Py_SETREF(metadata, PyObject_GetAttr(entry, s_dist_info_metadata));
        if (metadata == NULL) goto error;
        if (metadata == unset) Py_SETREF(metadata, Py_NewRef(Py_None));
    }
    if (PyDict_CheckExact(metadata)) {
        if (PyDict_GET_SIZE(metadata) == 0) stored_metadata = Py_NewRef(Py_True);
        else {
            stored_metadata = lone_sha256(metadata);
            if (stored_metadata == NULL) { if (PyErr_Occurred()) goto error; goto fallback; }
        }
    }
    else if (PyDict_Check(metadata)) goto fallback;
    else stored_metadata = Py_NewRef(metadata == Py_True ? Py_True : Py_None);

    int size_ok = PyLong_CheckExact(size) && PyObject_RichCompareBool(size, zero, Py_GE) == 1;
    record = PyTuple_Pack(9, url, text, stored_hashes,
                          PyUnicode_Check(rp) ? rp : Py_None,
                          yanked_reason, stored_metadata,
                          PyUnicode_Check(upload) ? upload : Py_None,
                          Py_None,
                          size_ok ? size : Py_None);
    if (record == NULL) goto error;

    if (kind == SDIST) {
        result = sdist_identity(tail, tail_length, version_type, record);
        if (result == NULL) goto error;
        goto done;
    }
    if (kind != WHEEL) {
        result = PyTuple_Pack(4, zero, Py_None, Py_None, record);
        goto done;
    }

    /* wheel_release_from_filename */
    const char *fields[6];
    Py_ssize_t lengths[6];
    int count = 0;
    Py_ssize_t stem = tail_length - 4, field_start = 0;
    int too_many = 0;
    for (Py_ssize_t i = 0; i <= stem; i++) {
        if (i == stem || tail[i] == '-') {
            if (count == 6) { too_many = 1; break; }
            fields[count] = tail + field_start;
            lengths[count] = i - field_start;
            count++;
            field_start = i + 1;
        }
    }
    int parses = !too_many && (count == 5 || count == 6) && escaped_name(fields[0], lengths[0]);
    if (parses && count == 6 && !(lengths[2] > 0 && fields[2][0] >= '0' && fields[2][0] <= '9')) parses = 0;
    if (parses) {
        PyObject *public = public_version(versions, version_type, fields[1], lengths[1]);
        if (public == NULL) {
            if (!PyErr_ExceptionMatches(PyExc_ValueError)) goto error;
            PyErr_Clear();
            parses = 0;
        }
        else {
            PyObject *name = canonical(fields[0], lengths[0]);
            if (name == NULL) { Py_DECREF(public); if (PyErr_Occurred()) goto error; goto fallback; }
            result = PyTuple_Pack(4, wheel_kind, name, public, record);
            Py_DECREF(name);
            Py_DECREF(public);
        }
    }
    if (!parses) {
        result = PyTuple_Pack(4, zero, Py_None, Py_None, record);
    }
    goto done;

fallback:
    result = Py_NewRef(Py_None);
    goto done;
error:
    result = NULL;
done:
    Py_XDECREF(url); Py_XDECREF(filename); Py_XDECREF(yanked); Py_XDECREF(hashes);
    Py_XDECREF(rp); Py_XDECREF(upload); Py_XDECREF(size); Py_XDECREF(metadata);
    Py_XDECREF(text); Py_XDECREF(stored_hashes); Py_XDECREF(yanked_reason);
    Py_XDECREF(stored_metadata); Py_XDECREF(record);
    return result;
}

/* release_facts: (kind_mask, requires_python, yanked) per distinct pair, in
 * the order the pairs first appear. */
static PyObject *
release_facts(PyObject *artifacts)
{
    PyObject *masks = PyDict_New();
    if (masks == NULL) return NULL;
    Py_ssize_t count = PyList_GET_SIZE(artifacts);
    for (Py_ssize_t i = 0; i < count; i++) {
        PyObject *pair = PyList_GET_ITEM(artifacts, i);
        PyObject *kind = PyTuple_GET_ITEM(pair, 0);
        PyObject *record = PyTuple_GET_ITEM(pair, 1);
        PyObject *rp = PyTuple_GET_ITEM(record, 3), *yanked = PyTuple_GET_ITEM(record, 4);
        PyObject *key = PyTuple_Pack(2, PyUnicode_Check(rp) ? rp : Py_None, PyUnicode_Check(yanked) ? yanked : Py_None);
        if (key == NULL) { Py_DECREF(masks); return NULL; }
        PyObject *previous = PyDict_GetItemWithError(masks, key);
        if (previous == NULL && PyErr_Occurred()) { Py_DECREF(key); Py_DECREF(masks); return NULL; }
        PyObject *mask = previous == NULL ? Py_NewRef(kind) : PyNumber_Or(previous, kind);
        if (mask == NULL || PyDict_SetItem(masks, key, mask) < 0) { Py_XDECREF(mask); Py_DECREF(key); Py_DECREF(masks); return NULL; }
        Py_DECREF(mask);
        Py_DECREF(key);
    }
    PyObject *facts = PyList_New(0);
    PyObject *key, *mask;
    Py_ssize_t position = 0;
    while (facts != NULL && PyDict_Next(masks, &position, &key, &mask)) {
        PyObject *fact = PyTuple_Pack(3, mask, PyTuple_GET_ITEM(key, 0), PyTuple_GET_ITEM(key, 1));
        if (fact == NULL || PyList_Append(facts, fact) < 0) { Py_XDECREF(fact); Py_CLEAR(facts); break; }
        Py_DECREF(fact);
    }
    Py_DECREF(masks);
    return facts;
}

PyDoc_STRVAR(compile_page_doc,
"compile_page(entries, unset, version_type, wheel_kind, fallback, keep_url=None,\n"
"             keep_version=None) -> (groups, unparsed)\n\n"
"A page's file entries compiled as catalog_from_json compiles them.\n"
"fallback(entry) compiles an entry this leaves, filtering it first: None to\n"
"skip it, else (record, identity). With keep_url and keep_version, only the\n"
"files of one release: keep_version(text) for a wheel's version field,\n"
"keep_url(url) for any other file.");

static PyObject *
compile_page(PyObject *module, PyObject *args)
{
    PyObject *entries, *unset, *version_type, *wheel_kind, *fallback;
    PyObject *keep_url = Py_None, *keep_version = Py_None;
    if (!PyArg_ParseTuple(args, "OOOOO|OO", &entries, &unset, &version_type, &wheel_kind,
                          &fallback, &keep_url, &keep_version)) return NULL;
    Filter filter = {NULL, NULL, NULL};
    if (keep_version != Py_None) {
        filter.keep_url = keep_url;
        filter.keep_version = keep_version;
        filter.kept = PyDict_New();
        if (filter.kept == NULL) return NULL;
    }
    PyObject *sequence = PySequence_Fast(entries, "entries must be a sequence");
    if (sequence == NULL) return NULL;
    PyObject *grouped = PyDict_New(), *unparsed = PyList_New(0), *groups = NULL, *result = NULL;
    PyObject *versions = PyDict_New();
    if (grouped == NULL || unparsed == NULL || versions == NULL) goto done;
    Py_ssize_t count = PySequence_Fast_GET_SIZE(sequence);
    for (Py_ssize_t i = 0; i < count; i++) {
        PyObject *entry = PySequence_Fast_GET_ITEM(sequence, i);
        PyObject *item = compile_entry(entry, unset, version_type, wheel_kind, versions, &filter);
        if (item == NULL) goto done;
        if (item == Py_False) { Py_DECREF(item); continue; }
        PyObject *kind, *name, *version, *record;
        if (item == Py_None) {
            Py_DECREF(item);
            item = PyObject_CallOneArg(fallback, entry);
            if (item == NULL) goto done;
            if (item == Py_None) { Py_DECREF(item); continue; }
            PyObject *identity = PyTuple_GET_ITEM(item, 1);
            record = PyTuple_GET_ITEM(item, 0);
            if (identity == Py_None) {
                int failed = PyList_Append(unparsed, record) < 0;
                Py_DECREF(item);
                if (failed) goto done;
                continue;
            }
            kind = PyTuple_GET_ITEM(identity, 0);
            name = PyTuple_GET_ITEM(identity, 1);
            version = PyTuple_GET_ITEM(identity, 2);
        }
        else {
            kind = PyTuple_GET_ITEM(item, 0);
            name = PyTuple_GET_ITEM(item, 1);
            version = PyTuple_GET_ITEM(item, 2);
            record = PyTuple_GET_ITEM(item, 3);
            if (name == Py_None) {
                int failed = PyList_Append(unparsed, record) < 0;
                Py_DECREF(item);
                if (failed) goto done;
                continue;
            }
        }
        PyObject *key = PyTuple_Pack(2, name, version);
        PyObject *pair = PyTuple_Pack(2, kind, record);
        Py_DECREF(item);
        if (key == NULL || pair == NULL) { Py_XDECREF(key); Py_XDECREF(pair); goto done; }
        PyObject *artifacts = PyDict_GetItemWithError(grouped, key);
        if (artifacts == NULL) {
            if (PyErr_Occurred()) { Py_DECREF(key); Py_DECREF(pair); goto done; }
            artifacts = PyList_New(0);
            if (artifacts == NULL || PyDict_SetItem(grouped, key, artifacts) < 0) { Py_XDECREF(artifacts); Py_DECREF(key); Py_DECREF(pair); goto done; }
            Py_DECREF(artifacts);
        }
        int failed = PyList_Append(artifacts, pair) < 0;
        Py_DECREF(key);
        Py_DECREF(pair);
        if (failed) goto done;
    }
    groups = PyList_New(0);
    if (groups == NULL) goto done;
    PyObject *key, *artifacts;
    Py_ssize_t position = 0;
    while (PyDict_Next(grouped, &position, &key, &artifacts)) {
        PyObject *facts = release_facts(artifacts);
        if (facts == NULL) goto done;
        PyObject *group = PyTuple_Pack(4, PyTuple_GET_ITEM(key, 0), PyTuple_GET_ITEM(key, 1), artifacts, facts);
        Py_DECREF(facts);
        if (group == NULL || PyList_Append(groups, group) < 0) { Py_XDECREF(group); goto done; }
        Py_DECREF(group);
    }
    result = PyTuple_Pack(2, groups, unparsed);
done:
    Py_XDECREF(filter.kept);
    Py_XDECREF(versions);
    Py_XDECREF(groups);
    Py_XDECREF(grouped);
    Py_XDECREF(unparsed);
    Py_DECREF(sequence);
    return result;
}

PyDoc_STRVAR(earliest_upload_text_doc,
"earliest_upload_text(artifacts) -> (status, text)\n\n"
"(0, None) when no artifact gives an upload time, (1, least) when every one\n"
"is fixed-width UTC ISO text, whose least is the earliest, else (2, None)\n"
"for the caller's own loop.");

static PyObject *
earliest_upload_text(PyObject *module, PyObject *artifacts)
{
    if (!PyList_CheckExact(artifacts)) return Py_BuildValue("(iO)", 2, Py_None);
    PyObject *least = NULL;
    Py_ssize_t width = -1;
    Py_ssize_t count = PyList_GET_SIZE(artifacts);
    for (Py_ssize_t i = 0; i < count; i++) {
        PyObject *record = PyTuple_GET_ITEM(PyList_GET_ITEM(artifacts, i), 1);
        PyObject *text = PyTuple_GET_ITEM(record, 6);
        if (!PyUnicode_CheckExact(text)) continue;
        Py_ssize_t length = PyUnicode_GET_LENGTH(text);
        if (width < 0) width = length;
        if (length != width || length < 11 || PyUnicode_READ_CHAR(text, length - 1) != 'Z'
            || PyUnicode_READ_CHAR(text, 10) != 'T') {
            return Py_BuildValue("(iO)", 2, Py_None);
        }
        if (least == NULL) { least = text; continue; }
        int less = PyUnicode_Compare(text, least);
        if (less == -1 && PyErr_Occurred()) return NULL;
        if (less < 0) least = text;
    }
    return least == NULL ? Py_BuildValue("(iO)", 0, Py_None) : Py_BuildValue("(iO)", 1, least);
}

static PyMethodDef methods[] = {
    {"earliest_upload_text", earliest_upload_text, METH_O, earliest_upload_text_doc},
    {"compile_page", compile_page, METH_VARARGS, compile_page_doc},
    {NULL, NULL, 0, NULL},
};

/* Its names and zero are made once, for the main interpreter, the only one
 * that imports it. */
static struct PyModuleDef module = {
    PyModuleDef_HEAD_INIT,
    .m_name = "_kpip_catalog",
    .m_doc = "Index page catalog compilation of kpip.index.page_parsing, in C.",
    .m_size = -1,
    .m_methods = methods,
};

PyMODINIT_FUNC
PyInit__kpip_catalog(void)
{
    if (zero != NULL) return PyModule_Create(&module);
#define INTERN(var, text) if ((var = PyUnicode_InternFromString(text)) == NULL) return NULL
    INTERN(s_url, "url"); INTERN(s_filename, "filename"); INTERN(s_yanked, "yanked");
    INTERN(s_hashes, "hashes"); INTERN(s_requires_python, "requires_python");
    INTERN(s_upload_time, "upload_time"); INTERN(s_size, "size");
    INTERN(s_core_metadata, "core_metadata"); INTERN(s_dist_info_metadata, "dist_info_metadata");
    INTERN(s_sha256, "sha256"); INTERN(s_public, "public");
    if ((zero = PyLong_FromLong(0)) == NULL) return NULL;
    return PyModule_Create(&module);
}

#ifdef KPIP_CATALOG_BUILTIN
/* Registered before main, as _kpip_link_tree.c explains. */
static void
register_builtin(void)
{
    PyImport_AppendInittab("_kpip_catalog", PyInit__kpip_catalog);
}

#ifdef _MSC_VER
#ifdef _WIN64
#define KPIP_SYMBOL_PREFIX ""
#else
#define KPIP_SYMBOL_PREFIX "_"
#endif
#pragma section(".CRT$XCU", read)
__declspec(allocate(".CRT$XCU")) void(__cdecl *kpip_catalog_register)(void) = register_builtin;
#pragma comment(linker, "/include:" KPIP_SYMBOL_PREFIX "kpip_catalog_register")
#else
__attribute__((constructor)) static void
register_builtin_constructor(void)
{
    register_builtin();
}
#endif
#endif
