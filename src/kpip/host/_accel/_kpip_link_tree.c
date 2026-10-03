/* kpip.host.clone's directory and hard-link loops, in C, with the GIL
 * released for each whole loop so a large tree's slices link in parallel.
 *
 * kpip-compile compiles this into the binary as the built-in
 * _kpip_link_tree (KPIP_LINK_TREE_BUILTIN); elsewhere clone runs the same
 * loops in Python, from _kpip_link_tree.py beside this file. Each function returns (0, len(names)) or the errno and
 * index of the first failure, which clone handles and resumes after;
 * remove_tree returns an errno or 0.
 * Names are os.fsencode bytes: UTF-8 on Windows, widened for the W calls.
 */

#define PY_SSIZE_T_CLEAN
#include <Python.h>

#include <errno.h>
#include <string.h>

#ifdef _WIN32
#include <windows.h>
#define SEPARATOR '\\'
/* Bytes of UTF-8; widened, never more wide characters than bytes. */
#define PATH_CAPACITY 32768
#else
#include <dirent.h>
#include <fcntl.h>
#include <sys/stat.h>
#include <unistd.h>
#define SEPARATOR '/'
#define PATH_CAPACITY 4096
#endif

#ifdef _WIN32

static int
errno_of(DWORD error)
{
    switch (error) {
    case ERROR_ALREADY_EXISTS:
    case ERROR_FILE_EXISTS:
        return EEXIST;
    case ERROR_FILE_NOT_FOUND:
    case ERROR_PATH_NOT_FOUND:
        return ENOENT;
    case ERROR_ACCESS_DENIED:
    case ERROR_SHARING_VIOLATION:
        return EACCES;
    case ERROR_NOT_SAME_DEVICE:
        return EXDEV;
    case ERROR_TOO_MANY_LINKS:
        return EMLINK;
    case ERROR_FILENAME_EXCED_RANGE:
        return ENAMETOOLONG;
    case ERROR_INVALID_FUNCTION:
    case ERROR_NOT_SUPPORTED:
        return EOPNOTSUPP;
    case ERROR_INVALID_NAME:
    case ERROR_INVALID_PARAMETER:
        return EINVAL;
    default:
        return EIO;
    }
}

/* path, UTF-8, widened into wide; an errno, or 0. */
static int
widen(const char *path, wchar_t *wide)
{
    if (MultiByteToWideChar(CP_UTF8, MB_ERR_INVALID_CHARS, path, -1, wide, PATH_CAPACITY) == 0) {
        return GetLastError() == ERROR_INSUFFICIENT_BUFFER ? ENAMETOOLONG : EINVAL;
    }
    return 0;
}

static int
make_directory(const char *path, unsigned int mode, wchar_t *wide)
{
    int error = widen(path, wide);
    if (error) {
        return error;
    }
    return CreateDirectoryW(wide, NULL) ? 0 : errno_of(GetLastError());
}

/* As os.chmod does on Windows: the owner write bit is the read-only
 * attribute, and nothing else of the mode applies. */
static int
change_mode(const char *path, unsigned int mode, wchar_t *wide)
{
    int error = widen(path, wide);
    if (error) {
        return error;
    }
    DWORD attributes = GetFileAttributesW(wide);
    if (attributes == INVALID_FILE_ATTRIBUTES) {
        return errno_of(GetLastError());
    }
    DWORD wanted = (mode & 0200) ? (attributes & ~FILE_ATTRIBUTE_READONLY)
                                 : (attributes | FILE_ATTRIBUTE_READONLY);
    if (wanted == attributes) {
        return 0;
    }
    return SetFileAttributesW(wide, wanted) ? 0 : errno_of(GetLastError());
}

static int
link_file(const char *source, const char *destination, wchar_t *wide_source, wchar_t *wide_destination)
{
    int error = widen(source, wide_source);
    if (!error) {
        error = widen(destination, wide_destination);
    }
    if (error) {
        return error;
    }
    return CreateHardLinkW(wide_destination, wide_source, NULL) ? 0 : errno_of(GetLastError());
}

#else

static int
make_directory(const char *path, unsigned int mode, wchar_t *unused)
{
    return mkdir(path, (mode_t)mode) == 0 ? 0 : (errno ? errno : EIO);
}

static int
change_mode(const char *path, unsigned int mode, wchar_t *unused)
{
    return chmod(path, (mode_t)mode) == 0 ? 0 : (errno ? errno : EIO);
}

static int
link_file(const char *source, const char *destination, wchar_t *unused_source, wchar_t *unused_destination)
{
    return link(source, destination) == 0 ? 0 : (errno ? errno : EIO);
}

#endif

/* Write name after the root already in buffer; 0 if the path is too long. */
static int
join(char *buffer, Py_ssize_t root_length, const char *name, Py_ssize_t name_length)
{
    if (root_length + 1 + name_length >= PATH_CAPACITY) {
        return 0;
    }
    buffer[root_length] = SEPARATOR;
    memcpy(buffer + root_length + 1, name, name_length);
    buffer[root_length + 1 + name_length] = '\0';
    return 1;
}

/* Copy root into buffer; 0 if it leaves no room for a name. */
static int
start_path(char *buffer, PyObject *root, Py_ssize_t *length)
{
    *length = PyBytes_GET_SIZE(root);
    if (*length + 2 >= PATH_CAPACITY) {
        return 0;
    }
    memcpy(buffer, PyBytes_AS_STRING(root), *length);
    return 1;
}

/* A tuple of the names, holding them while the GIL is released, with
 * pointers to their bytes in pointers and lengths. */
static PyObject *
borrow_names(PyObject *names, const char ***pointers, Py_ssize_t **lengths)
{
    *pointers = NULL;
    *lengths = NULL;
    PyObject *held = PySequence_Tuple(names);
    if (held == NULL) {
        return NULL;
    }
    Py_ssize_t count = PyTuple_GET_SIZE(held);
    *pointers = PyMem_New(const char *, count ? count : 1);
    *lengths = PyMem_New(Py_ssize_t, count ? count : 1);
    if (*pointers == NULL || *lengths == NULL) {
        PyErr_NoMemory();
        goto error;
    }
    for (Py_ssize_t index = 0; index < count; index++) {
        PyObject *name = PyTuple_GET_ITEM(held, index);
        if (!PyBytes_Check(name)) {
            PyErr_SetString(PyExc_TypeError, "names must be bytes");
            goto error;
        }
        (*pointers)[index] = PyBytes_AS_STRING(name);
        (*lengths)[index] = PyBytes_GET_SIZE(name);
        if (memchr((*pointers)[index], '\0', (*lengths)[index]) != NULL) {
            PyErr_SetString(PyExc_ValueError, "embedded null byte");
            goto error;
        }
    }
    return held;

error:
    PyMem_Free(*pointers);
    PyMem_Free(*lengths);
    *pointers = NULL;
    *lengths = NULL;
    Py_DECREF(held);
    return NULL;
}

/* Path buffers too large for a thread's stack on Windows. */
typedef struct {
    char path[PATH_CAPACITY];
    char other[PATH_CAPACITY];
#ifdef _WIN32
    wchar_t wide[PATH_CAPACITY];
    wchar_t other_wide[PATH_CAPACITY];
#endif
} Buffers;

#ifdef _WIN32
#define WIDE(buffers) (buffers)->wide
#define OTHER_WIDE(buffers) (buffers)->other_wide
#else
#define WIDE(buffers) NULL
#define OTHER_WIDE(buffers) NULL
#endif

static PyObject *
each_directory(PyObject *args, int change)
{
    PyObject *root, *names, *modes;
    if (!PyArg_ParseTuple(args, "SOO", &root, &names, &modes)) {
        return NULL;
    }
    const char **pointers;
    Py_ssize_t *lengths;
    PyObject *held = borrow_names(names, &pointers, &lengths);
    if (held == NULL) {
        return NULL;
    }
    Py_ssize_t count = PyTuple_GET_SIZE(held);
    PyObject *result = NULL;
    Buffers *buffers = NULL;
    unsigned int *values = PyMem_New(unsigned int, count ? count : 1);
    if (values == NULL) {
        PyErr_NoMemory();
        goto done;
    }
    Py_ssize_t given = PySequence_Size(modes);
    if (given != count) {
        if (given >= 0) {
            PyErr_SetString(PyExc_ValueError, "names and modes differ in length");
        }
        goto done;
    }
    for (Py_ssize_t index = 0; index < count; index++) {
        PyObject *item = PySequence_GetItem(modes, index);
        if (item == NULL) {
            goto done;
        }
        long value = PyLong_AsLong(item);
        Py_DECREF(item);
        if (value == -1 && PyErr_Occurred()) {
            goto done;
        }
        values[index] = (unsigned int)value;
    }
    buffers = PyMem_RawMalloc(sizeof(Buffers));
    if (buffers == NULL) {
        PyErr_NoMemory();
        goto done;
    }

    Py_ssize_t root_length;
    if (!start_path(buffers->path, root, &root_length)) {
        result = Py_BuildValue("(in)", ENAMETOOLONG, (Py_ssize_t)0);
        goto done;
    }
    int error = 0;
    Py_ssize_t index;
    Py_BEGIN_ALLOW_THREADS
    for (index = 0; index < count; index++) {
        if (!join(buffers->path, root_length, pointers[index], lengths[index])) {
            error = ENAMETOOLONG;
            break;
        }
        error = change ? change_mode(buffers->path, values[index], WIDE(buffers))
                       : make_directory(buffers->path, values[index], WIDE(buffers));
        if (error) {
            break;
        }
    }
    Py_END_ALLOW_THREADS
    result = Py_BuildValue("(in)", error, index);

done:
    PyMem_RawFree(buffers);
    PyMem_Free(values);
    PyMem_Free(pointers);
    PyMem_Free(lengths);
    Py_DECREF(held);
    return result;
}

PyDoc_STRVAR(make_directories_doc,
"make_directories(root, names, modes) -> (errno, index)\n\n"
"mkdir each of names under root with its mode from modes, parents first.");

static PyObject *
make_directories(PyObject *module, PyObject *args)
{
    return each_directory(args, 0);
}

PyDoc_STRVAR(change_modes_doc,
"change_modes(root, names, modes) -> (errno, index)\n\n"
"chmod each of names under root to its mode from modes.");

static PyObject *
change_modes(PyObject *module, PyObject *args)
{
    return each_directory(args, 1);
}

PyDoc_STRVAR(link_files_doc,
"link_files(source_root, destination_root, names, start) -> (errno, index)\n\n"
"Hard link each of names from start on, from source_root to destination_root.");

static PyObject *
link_files(PyObject *module, PyObject *args)
{
    PyObject *source_root, *destination_root, *names;
    Py_ssize_t start;
    if (!PyArg_ParseTuple(args, "SSOn", &source_root, &destination_root, &names, &start)) {
        return NULL;
    }
    const char **pointers;
    Py_ssize_t *lengths;
    PyObject *held = borrow_names(names, &pointers, &lengths);
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
    PyObject *result = NULL;
    Buffers *buffers = PyMem_RawMalloc(sizeof(Buffers));
    if (buffers == NULL) {
        PyErr_NoMemory();
        goto done;
    }
    Py_ssize_t source_length, destination_length;
    if (!start_path(buffers->path, source_root, &source_length)
        || !start_path(buffers->other, destination_root, &destination_length)) {
        result = Py_BuildValue("(in)", ENAMETOOLONG, start);
        goto done;
    }
    int error = 0;
    Py_ssize_t index;
    Py_BEGIN_ALLOW_THREADS
    for (index = start; index < count; index++) {
        if (!join(buffers->path, source_length, pointers[index], lengths[index])
            || !join(buffers->other, destination_length, pointers[index], lengths[index])) {
            error = ENAMETOOLONG;
            break;
        }
        error = link_file(buffers->path, buffers->other, WIDE(buffers), OTHER_WIDE(buffers));
        if (error) {
            break;
        }
    }
    Py_END_ALLOW_THREADS
    result = Py_BuildValue("(in)", error, index);

done:
    PyMem_RawFree(buffers);
    PyMem_Free(pointers);
    PyMem_Free(lengths);
    Py_DECREF(held);
    return result;
}

#ifdef _WIN32

/* Remove everything in the directory whose wide path, length characters
 * long, is in path, which has room for PATH_CAPACITY: a directory's
 * contents, then the directory; a link or junction is removed, never
 * entered; a read-only file is made writable first, as Windows will not
 * delete one. found is scratch for the listing. An errno, or 0. */
static int
remove_entries(wchar_t *path, size_t length, WIN32_FIND_DATAW *found)
{
    if (length + 3 >= PATH_CAPACITY) {
        return ENAMETOOLONG;
    }
    path[length] = L'\\';
    path[length + 1] = L'*';
    path[length + 2] = L'\0';
    HANDLE listing = FindFirstFileExW(path, FindExInfoBasic, found, FindExSearchNameMatch,
                                      NULL, FIND_FIRST_EX_LARGE_FETCH);
    path[length] = L'\0';
    if (listing == INVALID_HANDLE_VALUE) {
        return errno_of(GetLastError());
    }
    int error = 0;
    do {
        const wchar_t *name = found->cFileName;
        if (name[0] == L'.' && (name[1] == L'\0' || (name[1] == L'.' && name[2] == L'\0'))) {
            continue;
        }
        size_t name_length = wcslen(name);
        if (length + 1 + name_length >= PATH_CAPACITY) {
            error = ENAMETOOLONG;
            break;
        }
        path[length] = L'\\';
        memcpy(path + length + 1, name, (name_length + 1) * sizeof(wchar_t));
        DWORD attributes = found->dwFileAttributes;
        if (attributes & FILE_ATTRIBUTE_READONLY) {
            SetFileAttributesW(path, attributes & ~FILE_ATTRIBUTE_READONLY);
        }
        if ((attributes & FILE_ATTRIBUTE_DIRECTORY)
            && !(attributes & FILE_ATTRIBUTE_REPARSE_POINT)) {
            error = remove_entries(path, length + 1 + name_length, found);
            if (!error && !RemoveDirectoryW(path)) {
                error = errno_of(GetLastError());
            }
        }
        else if (attributes & FILE_ATTRIBUTE_DIRECTORY) {
            if (!RemoveDirectoryW(path)) {
                error = errno_of(GetLastError());
            }
        }
        else if (!DeleteFileW(path)) {
            error = errno_of(GetLastError());
        }
        path[length] = L'\0';
    } while (!error && FindNextFileW(listing, found));
    if (!error && GetLastError() != ERROR_NO_MORE_FILES) {
        error = errno_of(GetLastError());
    }
    FindClose(listing);
    return error;
}

PyDoc_STRVAR(remove_tree_doc,
"remove_tree(path) -> errno\n\n"
"Remove the directory at path and everything in it, following no link.");

static PyObject *
remove_tree(PyObject *module, PyObject *args)
{
    PyObject *path;
    if (!PyArg_ParseTuple(args, "S", &path)) {
        return NULL;
    }
    const char *text = PyBytes_AS_STRING(path);
    if (memchr(text, '\0', PyBytes_GET_SIZE(path)) != NULL) {
        PyErr_SetString(PyExc_ValueError, "embedded null byte");
        return NULL;
    }
    Buffers *buffers = PyMem_RawMalloc(sizeof(Buffers));
    WIN32_FIND_DATAW *found = PyMem_RawMalloc(sizeof(WIN32_FIND_DATAW));
    if (buffers == NULL || found == NULL) {
        PyMem_RawFree(buffers);
        PyMem_RawFree(found);
        return PyErr_NoMemory();
    }
    int error = 0;
    Py_BEGIN_ALLOW_THREADS
    error = widen(text, buffers->wide);
    if (!error) {
        DWORD attributes = GetFileAttributesW(buffers->wide);
        if (attributes == INVALID_FILE_ATTRIBUTES) {
            error = errno_of(GetLastError());
        }
        else if (!(attributes & FILE_ATTRIBUTE_DIRECTORY)
                 || (attributes & FILE_ATTRIBUTE_REPARSE_POINT)) {
            /* Not a directory of its own: shutil.rmtree refuses it too. */
            error = ENOTDIR;
        }
        else {
            error = remove_entries(buffers->wide, wcslen(buffers->wide), found);
            if (!error) {
                if (attributes & FILE_ATTRIBUTE_READONLY) {
                    SetFileAttributesW(buffers->wide, attributes & ~FILE_ATTRIBUTE_READONLY);
                }
                if (!RemoveDirectoryW(buffers->wide)) {
                    error = errno_of(GetLastError());
                }
            }
        }
    }
    Py_END_ALLOW_THREADS
    PyMem_RawFree(found);
    PyMem_RawFree(buffers);
    return PyLong_FromLong(error);
}

#else

/* Remove everything in the directory open at descriptor, which it closes:
 * a directory's contents, then the directory; a link is removed, never
 * followed. Passes repeat until one finds nothing, as entries removed
 * while the directory is read may hide others from that read. An errno,
 * or 0. */
static int
remove_entries(int descriptor)
{
    DIR *directory = fdopendir(descriptor);
    if (directory == NULL) {
        int error = errno ? errno : EIO;
        close(descriptor);
        return error;
    }
    int at = dirfd(directory);
    int error = 0;
    int found = 1;
    while (found && !error) {
        found = 0;
        rewinddir(directory);
        struct dirent *entry;
        while (!error) {
            errno = 0;
            entry = readdir(directory);
            if (entry == NULL) {
                error = errno;
                break;
            }
            const char *name = entry->d_name;
            if (name[0] == '.' && (name[1] == '\0' || (name[1] == '.' && name[2] == '\0'))) {
                continue;
            }
            found = 1;
            int is_directory = 0;
#ifdef DT_DIR
            if (entry->d_type == DT_DIR) {
                is_directory = 1;
            }
            else if (entry->d_type == DT_UNKNOWN)
#endif
            {
                struct stat status;
                if (fstatat(at, name, &status, AT_SYMLINK_NOFOLLOW) == 0) {
                    is_directory = S_ISDIR(status.st_mode);
                }
            }
            if (is_directory) {
                int child = openat(at, name, O_RDONLY | O_DIRECTORY | O_NOFOLLOW | O_CLOEXEC, 0);
                if (child < 0) {
                    error = errno ? errno : EIO;
                    break;
                }
                error = remove_entries(child);
                if (!error && unlinkat(at, name, AT_REMOVEDIR) != 0) {
                    error = errno ? errno : EIO;
                }
            }
            else if (unlinkat(at, name, 0) != 0) {
                error = errno ? errno : EIO;
            }
        }
    }
    closedir(directory);
    return error;
}

PyDoc_STRVAR(remove_tree_doc,
"remove_tree(path) -> errno\n\n"
"Remove the directory at path and everything in it, following no link.");

static PyObject *
remove_tree(PyObject *module, PyObject *args)
{
    PyObject *path;
    if (!PyArg_ParseTuple(args, "S", &path)) {
        return NULL;
    }
    const char *text = PyBytes_AS_STRING(path);
    if (memchr(text, '\0', PyBytes_GET_SIZE(path)) != NULL) {
        PyErr_SetString(PyExc_ValueError, "embedded null byte");
        return NULL;
    }
    int error = 0;
    Py_BEGIN_ALLOW_THREADS
    int descriptor = open(text, O_RDONLY | O_DIRECTORY | O_NOFOLLOW | O_CLOEXEC, 0);
    if (descriptor < 0) {
        error = errno ? errno : EIO;
    }
    else {
        error = remove_entries(descriptor);
        if (!error && rmdir(text) != 0) {
            error = errno ? errno : EIO;
        }
    }
    Py_END_ALLOW_THREADS
    return PyLong_FromLong(error);
}

#endif

static PyMethodDef methods[] = {
    {"make_directories", make_directories, METH_VARARGS, make_directories_doc},
    {"change_modes", change_modes, METH_VARARGS, change_modes_doc},
    {"link_files", link_files, METH_VARARGS, link_files_doc},
    {"remove_tree", remove_tree, METH_VARARGS, remove_tree_doc},
    {NULL, NULL, 0, NULL},
};

static PyModuleDef_Slot slots[] = {
    {Py_mod_multiple_interpreters, Py_MOD_PER_INTERPRETER_GIL_SUPPORTED},
    {Py_mod_gil, Py_MOD_GIL_NOT_USED},
    {0, NULL},
};

static struct PyModuleDef module = {
    PyModuleDef_HEAD_INIT,
    .m_name = "_kpip_link_tree",
    .m_doc = "The directory and hard-link loops of kpip.host.clone, in C.",
    .m_size = 0,
    .m_methods = methods,
    .m_slots = slots,
};

PyMODINIT_FUNC
PyInit__kpip_link_tree(void)
{
    return PyModuleDef_Init(&module);
}

#ifdef KPIP_LINK_TREE_BUILTIN
/* Built into the binary: add the module to the interpreter's built-in table
 * before main starts it, which PyImport_AppendInittab must precede. Nuitka
 * has no step of its own for that, and an initializer runs before main on
 * every compiler: a constructor with GCC and clang, a CRT initializer with
 * MSVC. Never in an extension: past Py_Initialize, it is a fatal error. */
static void
register_builtin(void)
{
    PyImport_AppendInittab("_kpip_link_tree", PyInit__kpip_link_tree);
}

#ifdef _MSC_VER
/* External, and named to the linker, so neither it nor LTCG drops the
 * entry for being unreferenced. */
#ifdef _WIN64
#define KPIP_SYMBOL_PREFIX ""
#else
#define KPIP_SYMBOL_PREFIX "_"
#endif
#pragma section(".CRT$XCU", read)
__declspec(allocate(".CRT$XCU")) void(__cdecl *kpip_link_tree_register)(void) =
    register_builtin;
#pragma comment(linker, "/include:" KPIP_SYMBOL_PREFIX "kpip_link_tree_register")
#else
__attribute__((constructor)) static void
register_builtin_constructor(void)
{
    register_builtin();
}
#endif
#endif
