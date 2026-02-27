#include <Python.h>
#include "structmember.h"

#ifdef _WIN32
#include <windows.h>
#else
#include <time.h>
#endif
#include <string.h>

#include "cbor.h"
#include "arrays.h"
#include "strings.h"
#include "bytestrings.h"
#include "floats_ctrls.h"
#include "ints.h"
#include "maps.h"

#define TARGET_DIGIT_SHIFT 20

#if defined(_MSC_VER)
#endif

#ifdef _WIN32
// Global variable to store the performance counter frequency.
// Initialized once in the first LogData object creation.
static LARGE_INTEGER performance_frequency;
static BOOL perf_freq_initialized = FALSE;
#endif

static const char *level_map[] = {"INFO", "TRACE ", "WARN ", "ERROR", "FATAL", "PANIC"};

typedef struct {
    PyObject_HEAD
    PyObject *enums;
    PyObject *tdenums;
    PyObject *variables;
    PyObject *functions;
    PyObject *saddr;
    PyObject *fmts;
    PyObject *filename;
    long count;
    double start_time;
} LogDataObject;


// C-level representation of parser types
typedef enum {
    PARSER_UNKNOWN,
    PARSER_INT32,
    PARSER_UINT32,
    PARSER_INT64,
    PARSER_UINT64,
    PARSER_DOUBLE,
    PARSER_POINTER,
    PARSER_BYTES,
    PARSER_STRING,
    PARSER_SYM,
    PARSER_ENUM
} ParserType;


// Forward declarations
static PyObject* cbor_item_to_pyobject(cbor_item_t * item);
static PyObject* process_fmts(cbor_item_t* fmts_item);
static PyObject* LogData_decode(LogDataObject *self, PyObject *args);
static PyObject* extract_vals_from_frame(LogDataObject *logdata, PyObject* frame, PyObject* parser_list);
static PyObject* LogData_target(LogDataObject *self, PyObject *Py_UNUSED(ignored));


// +++++ START C PARSER IMPLEMENTATION +++++

// Little-endian unpack helpers
static int32_t unpack_le_int32(const unsigned char* b) {
    return (int32_t)((uint32_t)b[0] | ((uint32_t)b[1] << 8) | ((uint32_t)b[2] << 16) | ((uint32_t)b[3] << 24));
}
static uint32_t unpack_le_uint32(const unsigned char* b) {
    return (uint32_t)b[0] | ((uint32_t)b[1] << 8) | ((uint32_t)b[2] << 16) | ((uint32_t)b[3] << 24);
}
static int64_t unpack_le_int64(const unsigned char* b) {
    return (int64_t)((uint64_t)b[0] | ((uint64_t)b[1] << 8) | ((uint64_t)b[2] << 16) | ((uint64_t)b[3] << 24) |
                     ((uint64_t)b[4] << 32) | ((uint64_t)b[5] << 40) | ((uint64_t)b[6] << 48) | ((uint64_t)b[7] << 56));
}
static uint64_t unpack_le_uint64(const unsigned char* b) {
    return (uint64_t)b[0] | ((uint64_t)b[1] << 8) | ((uint64_t)b[2] << 16) | ((uint64_t)b[3] << 24) |
           ((uint64_t)b[4] << 32) | ((uint64_t)b[5] << 40) | ((uint64_t)b[6] << 48) | ((uint64_t)b[7] << 56);
}
static double unpack_le_double(const unsigned char* b) {
    double d;
    memcpy(&d, b, sizeof(double));
    return d;
}

// C Parser functions
static PyObject* parse_c_int32(const unsigned char **b, Py_ssize_t *len) {
    if (*len < 4) { PyErr_SetString(PyExc_ValueError, "<missing int32>"); return NULL; }
    int32_t val = unpack_le_int32(*b);
    *b += 4; *len -= 4;
    return PyLong_FromLong(val);
}
static PyObject* parse_c_uint32(const unsigned char **b, Py_ssize_t *len) {
    if (*len < 4) { PyErr_SetString(PyExc_ValueError, "<missing uint32>"); return NULL; }
    uint32_t val = unpack_le_uint32(*b);
    *b += 4; *len -= 4;
    return PyLong_FromUnsignedLong(val);
}
static PyObject* parse_c_int64(const unsigned char **b, Py_ssize_t *len) {
    if (*len < 8) { PyErr_SetString(PyExc_ValueError, "<missing int64>"); return NULL; }
    int64_t val = unpack_le_int64(*b);
    *b += 8; *len -= 8;
    return PyLong_FromLongLong(val);
}
static PyObject* parse_c_uint64(const unsigned char **b, Py_ssize_t *len) {
    if (*len < 8) { PyErr_SetString(PyExc_ValueError, "<missing uint64>"); return NULL; }
    uint64_t val = unpack_le_uint64(*b);
    *b += 8; *len -= 8;
    return PyLong_FromUnsignedLongLong(val);
}
static PyObject* parse_c_double(const unsigned char **b, Py_ssize_t *len) {
    if (*len < 8) { PyErr_SetString(PyExc_ValueError, "<missing double>"); return NULL; }
    double val = unpack_le_double(*b);
    *b += 8; *len -= 8;
    return PyFloat_FromDouble(val);
}
static PyObject* parse_c_pointer(const unsigned char **b, Py_ssize_t *len) {
    if (*len < 4) { PyErr_SetString(PyExc_ValueError, "<missing pointer>"); return NULL; }
    uint32_t val = unpack_le_uint32(*b);
    *b += 4; *len -= 4;
    return PyLong_FromUnsignedLong(val);
}
static PyObject* parse_c_bytes(const unsigned char **b, Py_ssize_t *len) {
    PyObject *val = PyBytes_FromStringAndSize((const char*)*b, *len);
    *b += *len; *len = 0;
    return val;
}
static PyObject* parse_c_string(const unsigned char **b, Py_ssize_t *len) {
    size_t str_len = strnlen((const char*)*b, *len);
    if (str_len >= (size_t)*len) { // No null terminator found
        PyErr_SetString(PyExc_ValueError, "<missing string>");
        return NULL;
    }
    PyObject* val = PyUnicode_FromStringAndSize((const char*)*b, str_len);
    *b += str_len + 1;
    *len -= str_len + 1;
    return val;
}
static PyObject* lookup_c_func(PyObject *functions_dict, uint32_t a) {
    a = a & ~1;
    PyObject *key, *value;
    Py_ssize_t pos = 0;
    while(PyDict_Next(functions_dict, &pos, &key, &value)) {
        if (!PyTuple_Check(key) || PyTuple_Size(key) != 2) continue;
        long low = PyLong_AsLong(PyTuple_GET_ITEM(key, 0));
        long hi = PyLong_AsLong(PyTuple_GET_ITEM(key, 1));
        if (PyErr_Occurred()) { return NULL; }

        if (a >= low && a < hi) {
            return PyUnicode_FromFormat("%S+0x%x", value, (unsigned int)(a - low));
        }
    }
    return NULL; // Not found, no error set
}

static PyObject* lookup_c_var(PyObject *variables_dict, uint32_t a) {
    PyObject *key, *value;
    Py_ssize_t pos = 0;

    PyObject *min_var_name = NULL;
    long min_offset = -1;

    while(PyDict_Next(variables_dict, &pos, &key, &value)) {
        long addr = PyLong_AsLong(key);
        if (PyErr_Occurred()) { return NULL; }

        long offset = a - addr;
        if (a >= addr && offset < 0x3000) {
            if (min_offset == -1 || offset < min_offset) {
                min_offset = offset;
                min_var_name = value;
            }
        }
    }

    if (min_var_name) {
        return PyUnicode_FromFormat("%S+0x%x", min_var_name, (unsigned int)min_offset);
    }
    return NULL; // Not found, no error set
}
static PyObject* parse_c_sym(const unsigned char **b, Py_ssize_t *len, LogDataObject *s) {
    if (*len < 4) { PyErr_SetString(PyExc_ValueError, "<missing uint32>"); return NULL; }
    uint32_t r = unpack_le_uint32(*b);
    *b += 4; *len -= 4;

    PyObject* f_str = lookup_c_func(s->functions, r);
    if (f_str != NULL || PyErr_Occurred()) return f_str;

    PyObject* v_str = lookup_c_var(s->variables, r);
    if (v_str != NULL || PyErr_Occurred()) return v_str;

    return PyUnicode_FromFormat("0x%08x", r);
}
static PyObject* parse_c_enum(const unsigned char **b, Py_ssize_t *len, LogDataObject *s, PyObject *enum_t_obj) {
    if (*len < 4) { PyErr_SetString(PyExc_ValueError, "<missing int32>"); return NULL; }
    int32_t r = unpack_le_int32(*b);
    *b += 4; *len -= 4;

    const char* enum_t = PyUnicode_AsUTF8(enum_t_obj);

    PyObject *enum_dict = PyDict_GetItem(s->enums, enum_t_obj); // borrowed
    if (enum_dict == NULL) {
        enum_dict = PyDict_GetItem(s->tdenums, enum_t_obj); // borrowed
    }

    if (enum_dict && PyDict_Check(enum_dict)) {
        PyObject *r_obj = PyLong_FromLong(r);
        PyObject *val = PyDict_GetItem(enum_dict, r_obj); // borrowed
        Py_DECREF(r_obj);
        if (val) {
            Py_INCREF(val);
            return val;
        } else {
            return PyUnicode_FromFormat("<%s:%d>", enum_t, r);
        }
    }
    return PyUnicode_FromFormat("<!%s:%d>", enum_t, r);
}
static PyObject* c_fndecode(PyObject* parser_spec) {
    if (PyUnicode_Check(parser_spec)) {
        const char* p_str = PyUnicode_AsUTF8(parser_spec);
        if (strcmp(p_str, "int32") == 0) return PyLong_FromLong(PARSER_INT32);
        if (strcmp(p_str, "uint32") == 0) return PyLong_FromLong(PARSER_UINT32);
        if (strcmp(p_str, "int64") == 0) return PyLong_FromLong(PARSER_INT64);
        if (strcmp(p_str, "uint64") == 0) return PyLong_FromLong(PARSER_UINT64);
        if (strcmp(p_str, "double") == 0) return PyLong_FromLong(PARSER_DOUBLE);
        if (strcmp(p_str, "pointer") == 0) return PyLong_FromLong(PARSER_POINTER);
        if (strcmp(p_str, "bytes") == 0) return PyLong_FromLong(PARSER_BYTES);
        if (strcmp(p_str, "string") == 0) return PyLong_FromLong(PARSER_STRING);
        if (strcmp(p_str, "sym") == 0) return PyLong_FromLong(PARSER_SYM);
    } else if (PyList_Check(parser_spec) && PyList_Size(parser_spec) == 2) {
        PyObject* p0 = PyList_GetItem(parser_spec, 0); // borrowed
        PyObject* p1 = PyList_GetItem(parser_spec, 1); // borrowed
        if (PyUnicode_Check(p0) && strcmp(PyUnicode_AsUTF8(p0), "enum") == 0 && PyUnicode_Check(p1)) {
            PyObject* type_obj = PyLong_FromLong(PARSER_ENUM);
            PyObject* tuple = PyTuple_Pack(2, type_obj, p1);
            Py_DECREF(type_obj);
            return tuple;
        }
    }
    PyErr_SetString(PyExc_ValueError, "unknown parser spec");
    return NULL;
}
// +++++ END C PARSER IMPLEMENTATION +++++


// Converts a libcbor item to an equivalent Python object.
static PyObject* cbor_item_to_pyobject(cbor_item_t * item) {
    if (item == NULL) { Py_RETURN_NONE; }

    switch (cbor_typeof(item)) {
        case CBOR_TYPE_UINT:
            return PyLong_FromUnsignedLongLong(cbor_get_int(item));
        case CBOR_TYPE_NEGINT:
            return PyLong_FromLongLong(-1LL - cbor_get_int(item));
        case CBOR_TYPE_BYTESTRING:
            return PyBytes_FromStringAndSize((const char *)cbor_bytestring_handle(item), cbor_bytestring_length(item));
        case CBOR_TYPE_STRING:
            return PyUnicode_FromStringAndSize((const char *)cbor_string_handle(item), cbor_string_length(item));
        case CBOR_TYPE_ARRAY: {
            size_t len = cbor_array_size(item);
            PyObject * list = PyList_New(len);
            if (!list) return NULL;
            for (size_t i = 0; i < len; i++) {
                PyObject * val = cbor_item_to_pyobject(cbor_array_get(item, i));
                if (!val) {
                    Py_DECREF(list);
                    return NULL;
                }
                PyList_SET_ITEM(list, i, val); // Steals reference to val
            }
            return list;
        }
        case CBOR_TYPE_MAP: {
            size_t len = cbor_map_size(item);
            PyObject * dict = PyDict_New();
            if (!dict) return NULL;

            struct cbor_pair * pairs = cbor_map_handle(item);
            for (size_t i = 0; i < len; i++) {
                PyObject * key = cbor_item_to_pyobject(pairs[i].key);
                if (!key) {
                    Py_DECREF(dict);
                    return NULL;
                }

                // If the key is a list, it must be converted to a tuple to be hashable.
                if (PyList_Check(key)) {
                    PyObject *tuple_key = PyList_AsTuple(key);
                    Py_DECREF(key); // We don't need the list version anymore.
                    if (!tuple_key) {
                        Py_DECREF(dict);
                        return NULL; // Error converting list to tuple
                    }
                    key = tuple_key; // Use the tuple as the key from now on.
                }

                PyObject * val = cbor_item_to_pyobject(pairs[i].value);
                if (!val) {
                    Py_DECREF(key);
                    Py_DECREF(dict);
                    return NULL;
                }

                if (PyDict_SetItem(dict, key, val) < 0) {
                    // This will trigger if the key is still not hashable.
                    Py_DECREF(key);
                    Py_DECREF(val);
                    Py_DECREF(dict);
                    return NULL;
                }
                Py_DECREF(key);
                Py_DECREF(val);
            }
            return dict;
        }
        case CBOR_TYPE_FLOAT_CTRL:
            if (cbor_is_bool(item)) {
                if (cbor_get_bool(item)) { Py_RETURN_TRUE; } else { Py_RETURN_FALSE; }
            }
            if (cbor_is_null(item)) { Py_RETURN_NONE; }
            // Assuming it's a float if not bool or null
            return PyFloat_FromDouble(cbor_float_get_float(item));
        default:
            PyErr_SetString(PyExc_TypeError, "Unsupported CBOR type");
            return NULL;
    }
}


// Special handling for the 'fmts' dictionary using pure C parsers
static PyObject* process_fmts(cbor_item_t* fmts_cbor_item) {
    PyObject* fmts_dict = PyDict_New();
    if (!fmts_dict) return NULL;

    size_t map_size = cbor_map_size(fmts_cbor_item);
    struct cbor_pair* pairs = cbor_map_handle(fmts_cbor_item);

    for (size_t i = 0; i < map_size; i++) {
        PyObject* key = cbor_item_to_pyobject(pairs[i].key);
        if (!key) {
            Py_DECREF(fmts_dict);
            return NULL;
        }
        cbor_item_t* val_item = pairs[i].value;

        if (!cbor_isa_array(val_item)) {
            Py_DECREF(key);
            continue;
        }

        size_t array_size = cbor_array_size(val_item);
        if (array_size == 3) {
            PyObject* val_tuple = cbor_item_to_pyobject(val_item);
            if (!val_tuple) {
                Py_DECREF(key);
                Py_DECREF(fmts_dict);
                return NULL;
            }
            PyDict_SetItem(fmts_dict, key, val_tuple);
            Py_DECREF(val_tuple);
        } else if (array_size == 5) {
            PyObject* level = cbor_item_to_pyobject(cbor_array_get(val_item, 0));
            PyObject* fname = cbor_item_to_pyobject(cbor_array_get(val_item, 1));
            PyObject* line = cbor_item_to_pyobject(cbor_array_get(val_item, 2));
            PyObject* clean_fmt = cbor_item_to_pyobject(cbor_array_get(val_item, 3));
            cbor_item_t* parser_str_array = cbor_array_get(val_item, 4);

            if (!level || !fname || !line || !clean_fmt) {
                Py_XDECREF(level); Py_XDECREF(fname); Py_XDECREF(line); Py_XDECREF(clean_fmt);
                Py_DECREF(key); Py_DECREF(fmts_dict);
                return NULL;
            }

            size_t parser_count = cbor_array_size(parser_str_array);
            PyObject* parser_id_list = PyList_New(parser_count);
            if (!parser_id_list) {
                Py_DECREF(level); Py_DECREF(fname); Py_DECREF(line); Py_DECREF(clean_fmt);
                Py_DECREF(key); Py_DECREF(fmts_dict);
                return NULL;
            }

            for (size_t j = 0; j < parser_count; j++) {
                PyObject* parser_spec = cbor_item_to_pyobject(cbor_array_get(parser_str_array, j));
                if (!parser_spec) {
                    Py_DECREF(parser_id_list); Py_DECREF(level); Py_DECREF(fname); Py_DECREF(line);
                    Py_DECREF(clean_fmt); Py_DECREF(key); Py_DECREF(fmts_dict);
                    return NULL;
                }
                PyObject* parser_id = c_fndecode(parser_spec);
                Py_DECREF(parser_spec);
                if (!parser_id) { // Error from c_fndecode
                    Py_DECREF(parser_id_list); Py_DECREF(level); Py_DECREF(fname); Py_DECREF(line);
                    Py_DECREF(clean_fmt); Py_DECREF(key); Py_DECREF(fmts_dict);
                    return NULL;
                }
                PyList_SET_ITEM(parser_id_list, j, parser_id); // Steals ref
            }

            PyObject* final_tuple = PyTuple_Pack(5, level, fname, line, clean_fmt, parser_id_list);
            Py_DECREF(level); Py_DECREF(fname); Py_DECREF(line); Py_DECREF(clean_fmt); Py_DECREF(parser_id_list);

            if (!final_tuple) {
                Py_DECREF(key); Py_DECREF(fmts_dict);
                return NULL;
            }

            PyDict_SetItem(fmts_dict, key, final_tuple);
            Py_DECREF(final_tuple);
        }
        Py_DECREF(key);
    }
    return fmts_dict;
}

// __new__ method
static PyObject* LogData_new(PyTypeObject *type, PyObject *args, PyObject *kwds) {
    LogDataObject *self = (LogDataObject *) type->tp_alloc(type, 0);
    if (self != NULL) {
        self->enums = NULL;
        self->tdenums = NULL;
        self->variables = NULL;
        self->functions = NULL;
        self->saddr = NULL;
        self->fmts = NULL;
        self->filename = NULL;
        self->count = 0;
        self->start_time = 0.0;
    }
    return (PyObject *) self;
}

// __init__ method using libcbor
static int LogData_init(LogDataObject *self, PyObject *args, PyObject *kwds) {
    const char *filename_str;
    static char *kwlist[] = {"filename", NULL};
    if (!PyArg_ParseTupleAndKeywords(args, kwds, "s", kwlist, &filename_str)) {
        return -1;
    }

    self->filename = PyUnicode_FromString(filename_str);
    if (self->filename == NULL) return -1;

    FILE *file = fopen(filename_str, "rb");
    if (!file) {
        PyErr_SetFromErrnoWithFilename(PyExc_IOError, filename_str);
        return -1;
    }
    fseek(file, 0, SEEK_END);
    long length = ftell(file);
    fseek(file, 0, SEEK_SET);

    unsigned char *buffer = PyMem_Malloc(length);
    if (!buffer) {
        fclose(file);
        PyErr_NoMemory();
        return -1;
    }
    fread(buffer, 1, length, file);
    fclose(file);

    // Use libcbor to load the data
    struct cbor_load_result result;
    cbor_item_t *root_item = cbor_load(buffer, length, &result);
    PyMem_Free(buffer);

    if (result.error.code != CBOR_ERR_NONE) {
        PyErr_SetString(PyExc_ValueError, "CBOR decoding failed");
        cbor_decref(&root_item);
        return -1;
    }

    // Traverse the CBOR map
    size_t map_size = cbor_map_size(root_item);
    struct cbor_pair *pairs = cbor_map_handle(root_item);

    for (size_t i = 0; i < map_size; i++) {
        cbor_item_t *key_item = pairs[i].key;
        if (!cbor_isa_string(key_item)) continue;

        size_t key_len = cbor_string_length(key_item);
        const unsigned char* key_str = cbor_string_handle(key_item);
        cbor_item_t *value_item = pairs[i].value;

        PyObject* py_val = NULL;
        if (strncmp((const char*)key_str, "fmts", key_len) == 0) {
            py_val = process_fmts(value_item); // No longer pass fndecode
            if (py_val == NULL) {
                cbor_decref(&root_item);
                return -1;
            }
            self->fmts = py_val;
        } else {
            py_val = cbor_item_to_pyobject(value_item);
            if (py_val == NULL) {
                cbor_decref(&root_item);
                return -1;
            }
            if (strncmp((const char*)key_str, "enums", key_len) == 0) self->enums = py_val;
            else if (strncmp((const char*)key_str, "tdenums", key_len) == 0) self->tdenums = py_val;
            else if (strncmp((const char*)key_str, "vars", key_len) == 0) self->variables = py_val;
            else if (strncmp((const char*)key_str, "fns", key_len) == 0) self->functions = py_val;
            else if (strncmp((const char*)key_str, "saddr", key_len) == 0) self->saddr = py_val;
            else Py_DECREF(py_val); // Unused value
        }
    }

    cbor_decref(&root_item);

#ifdef _WIN32
    if (!perf_freq_initialized) {
        if (QueryPerformanceFrequency(&performance_frequency)) {
            perf_freq_initialized = TRUE;
        } else {
            PyErr_SetString(PyExc_SystemError, "Failed to query performance frequency.");
            return -1;
        }
    }
    LARGE_INTEGER start_counter;
    if (QueryPerformanceCounter(&start_counter)) {
        self->start_time = (double)start_counter.QuadPart;
    } else {
        PyErr_SetString(PyExc_SystemError, "Failed to query performance counter.");
        return -1;
    }
#else
    struct timespec start_spec;
    if (clock_gettime(CLOCK_MONOTONIC, &start_spec) == 0) {
        self->start_time = (double)start_spec.tv_sec + (double)start_spec.tv_nsec / 1e9;
    } else {
        PyErr_SetString(PyExc_SystemError, "Failed to get monotonic clock time.");
        return -1;
    }
#endif
    self->count = 0;
    return 0;
}


static void
LogData_dealloc(LogDataObject *self) {
    Py_XDECREF(self->enums);
    Py_XDECREF(self->tdenums);
    Py_XDECREF(self->variables);
    Py_XDECREF(self->functions);
    Py_XDECREF(self->saddr);
    Py_XDECREF(self->fmts);
    Py_XDECREF(self->filename);
    Py_TYPE(self)->tp_free((PyObject *) self);
}

static PyObject *
LogData_target(LogDataObject *self, PyObject *Py_UNUSED(ignored)) {
    if (self->saddr == NULL) {
        PyErr_SetString(PyExc_AttributeError, "saddr not initialized");
        return NULL;
    }
    long saddr_val = PyLong_AsLong(self->saddr);
    if (saddr_val == -1 && PyErr_Occurred()) {
        return NULL;
    }
    long target_val = (saddr_val >> TARGET_DIGIT_SHIFT) & 0xf;
    return PyLong_FromLong(target_val);
}

static PyObject *
LogData_decode(LogDataObject *self, PyObject *args) {
    PyObject *item_tuple = NULL;
    long target, addr;
    PyObject *frame;

    if (!PyArg_ParseTuple(args, "O!", &PyTuple_Type, &item_tuple) ||
        !PyArg_ParseTuple(item_tuple, "llO", &target, &addr, &frame)) {
        return NULL;
    }

    long kind = addr & 3;
    long clean_addr = addr & ~3;

    double ts;
#ifdef _WIN32
    LARGE_INTEGER current_counter;
    if (QueryPerformanceCounter(&current_counter)) {
        double elapsed_ticks = (double)current_counter.QuadPart - self->start_time;
        ts = elapsed_ticks / (double)performance_frequency.QuadPart;
    } else {
        PyErr_SetString(PyExc_SystemError, "Failed to query performance counter.");
        return NULL;
    }
#else
    struct timespec current_spec;
    if (clock_gettime(CLOCK_MONOTONIC, &current_spec) == 0) {
        double current_s = (double)current_spec.tv_sec + (double)current_spec.tv_nsec / 1e9;
        ts = current_s - self->start_time;
    } else {
        PyErr_SetString(PyExc_SystemError, "Failed to get monotonic clock time.");
        return NULL;
    }
#endif

    self->count++;

    PyObject *addr_key = PyLong_FromLong(clean_addr);
    PyObject *fmt_tuple = PyDict_GetItem(self->fmts, addr_key); // Borrowed
    Py_DECREF(addr_key);

    if (fmt_tuple == NULL || !PyTuple_Check(fmt_tuple) || PyTuple_Size(fmt_tuple) < 5 || PyTuple_GET_ITEM(fmt_tuple, 0) == Py_None) {
        PyObject *hex_frame = PyObject_CallMethod(frame, "hex", NULL);
        PyObject *text = PyUnicode_FromFormat("UNDECODED: TGT=%ld ADDR=0x%lX FRAME=%S", target, addr, hex_frame);
        Py_DECREF(hex_frame);
        PyObject* result = Py_BuildValue("(ldssiO)", self->count, ts, "RAW", "?", 0, text);
        Py_DECREF(text);
        return result;
    }

    PyObject *level_obj = PyTuple_GET_ITEM(fmt_tuple, 0);
    PyObject *fname = PyTuple_GET_ITEM(fmt_tuple, 1);
    PyObject *line_obj = PyTuple_GET_ITEM(fmt_tuple, 2);
    PyObject *clean_fmt = PyTuple_GET_ITEM(fmt_tuple, 3);
    PyObject *parser_list = PyTuple_GET_ITEM(fmt_tuple, 4);

    PyObject *vals_and_error = extract_vals_from_frame(self, frame, parser_list);
    if (vals_and_error == NULL) {
        return NULL;
    }

    PyObject *vals = PyTuple_GET_ITEM(vals_and_error, 0);
    PyObject *error = PyTuple_GET_ITEM(vals_and_error, 1);
    PyObject *result_tuple;

    if (vals != Py_None) {
        PyObject* text = PyUnicode_Format(clean_fmt, vals);

        if (text == NULL) { // If formatting failed, create a debug message
            PyErr_Clear();
            PyObject* repr_vals = PyObject_Repr(vals);
            text = PyUnicode_FromFormat("%S (FORMATTING FAILED) %S", clean_fmt, repr_vals);
            Py_DECREF(repr_vals);
        }
        PyObject* py_level = PyNumber_Long(level_obj);
        if (!py_level) { Py_DECREF(text); Py_DECREF(vals_and_error); return NULL; }
        long level_val = PyLong_AsLong(py_level);
        Py_DECREF(py_level);

        PyObject* py_line = PyNumber_Long(line_obj);
        if (!py_line) { Py_DECREF(text); Py_DECREF(vals_and_error); return NULL; }
        long line_val = PyLong_AsLong(py_line);
        Py_DECREF(py_line);

        const char *level_str = (level_val >= 0 && level_val < 6) ? level_map[level_val] : "<bad level>";
        result_tuple = Py_BuildValue("(ldOOlO)", self->count, ts, PyUnicode_FromString(level_str), fname, line_val, text);
        Py_DECREF(text);
    } else {
        PyObject *hex_frame = PyObject_CallMethod(frame, "hex", NULL);
        PyObject *text = PyUnicode_FromFormat("%U [%S - %U]", clean_fmt, hex_frame, error);
        Py_DECREF(hex_frame);

        PyObject* py_level = PyNumber_Long(level_obj);
        if (!py_level) { Py_DECREF(text); Py_DECREF(vals_and_error); return NULL; }
        long level_val = PyLong_AsLong(py_level);
        Py_DECREF(py_level);

        PyObject* py_line = PyNumber_Long(line_obj);
        if (!py_line) { Py_DECREF(text); Py_DECREF(vals_and_error); return NULL; }
        long line_val = PyLong_AsLong(py_line);
        Py_DECREF(py_line);

        const char *level_str = (level_val >= 0 && level_val < 6) ? level_map[level_val] : "<bad level>";
        result_tuple = Py_BuildValue("(ldOOlO)", self->count, ts, PyUnicode_FromString(level_str), fname, line_val, text);
        Py_DECREF(text);
    }

    Py_DECREF(vals_and_error);
    return result_tuple;
}

static PyObject *
extract_vals_from_frame(LogDataObject *logdata, PyObject* frame, PyObject* parser_list) {
    if (!PyBytes_Check(frame)) {
        PyErr_SetString(PyExc_TypeError, "frame must be a bytes object");
        return NULL;
    }
    if (!PyList_Check(parser_list)) {
        PyErr_SetString(PyExc_TypeError, "parser_list must be a list");
        return NULL;
    }
    Py_ssize_t num_parsers = PyList_Size(parser_list);
    PyObject *vals_list = PyList_New(num_parsers);
    if(!vals_list) return NULL;

    const unsigned char *current_frame_data = (const unsigned char*) PyBytes_AsString(frame);
    Py_ssize_t current_frame_len = PyBytes_Size(frame);

    for (Py_ssize_t i = 0; i < num_parsers; i++) {
        PyObject *parser_id_obj = PyList_GET_ITEM(parser_list, i); // Borrowed
        PyObject *val = NULL;
        ParserType p_type;
        PyObject* p_extra = NULL; // for enum name

        if (PyLong_Check(parser_id_obj)) {
            p_type = (ParserType)PyLong_AsLong(parser_id_obj);
        } else if (PyTuple_Check(parser_id_obj) && PyTuple_Size(parser_id_obj) == 2) {
            p_type = (ParserType)PyLong_AsLong(PyTuple_GET_ITEM(parser_id_obj, 0));
            p_extra = PyTuple_GET_ITEM(parser_id_obj, 1);
        } else {
             PyErr_SetString(PyExc_TypeError, "Invalid parser identifier in list");
             Py_DECREF(vals_list);
             return NULL;
        }

        switch(p_type) {
            case PARSER_INT32:   val = parse_c_int32(&current_frame_data, &current_frame_len);   break;
            case PARSER_UINT32:  val = parse_c_uint32(&current_frame_data, &current_frame_len);  break;
            case PARSER_INT64:   val = parse_c_int64(&current_frame_data, &current_frame_len);   break;
            case PARSER_UINT64:  val = parse_c_uint64(&current_frame_data, &current_frame_len);  break;
            case PARSER_DOUBLE:  val = parse_c_double(&current_frame_data, &current_frame_len);  break;
            case PARSER_POINTER: val = parse_c_pointer(&current_frame_data, &current_frame_len); break;
            case PARSER_BYTES:   val = parse_c_bytes(&current_frame_data, &current_frame_len);   break;
            case PARSER_STRING:  val = parse_c_string(&current_frame_data, &current_frame_len);  break;
            case PARSER_SYM:     val = parse_c_sym(&current_frame_data, &current_frame_len, logdata); break;
            case PARSER_ENUM:    val = parse_c_enum(&current_frame_data, &current_frame_len, logdata, p_extra); break;
            default:
                PyErr_SetString(PyExc_ValueError, "Unknown parser type");
                val = NULL;
                break;
        }

        if (val == NULL) {
            Py_DECREF(vals_list);
            PyObject* error_str = PyUnicode_FromFormat("Failed to parse field %zd", i);
            return Py_BuildValue("ON", Py_None, error_str);
        }
        PyList_SET_ITEM(vals_list, i, val); // Steals ref
    }

    PyObject *vals_tuple = PyList_AsTuple(vals_list);
    Py_DECREF(vals_list);

    if (current_frame_len > 0) {
        PyObject* remaining_bytes = PyBytes_FromStringAndSize((const char*)current_frame_data, current_frame_len);
        PyObject* hex_bytes = PyObject_CallMethod(remaining_bytes, "hex", NULL);
        Py_DECREF(remaining_bytes);
        PyObject *error_str = PyUnicode_FromFormat("Extra data in frame: %S", hex_bytes);
        Py_DECREF(hex_bytes);
        Py_DECREF(vals_tuple);
        return Py_BuildValue("ON", Py_None, error_str);
    }

    return Py_BuildValue("ON", vals_tuple, Py_None);
}

static PyMemberDef LogData_members[] = {
    {"enums", T_OBJECT_EX, offsetof(LogDataObject, enums), 0, "enums table"},
    {"tdenums", T_OBJECT_EX, offsetof(LogDataObject, tdenums), 0, "tdenums table"},
    {"variables", T_OBJECT_EX, offsetof(LogDataObject, variables), 0, "variables table"},
    {"functions", T_OBJECT_EX, offsetof(LogDataObject, functions), 0, "functions table"},
    {NULL}
};

static PyMethodDef LogData_methods[] = {
    {"decode", (PyCFunction)LogData_decode, METH_VARARGS, "Decodes a log item."},
    {"target", (PyCFunction)LogData_target, METH_NOARGS, "Returns the target ID."},
    {NULL}
};

static PyTypeObject LogDataType = {
    PyVarObject_HEAD_INIT(NULL, 0)
    .tp_name = "logdata_ext.LogData",
    .tp_doc = "LogData objects",
    .tp_basicsize = sizeof(LogDataObject),
    .tp_itemsize = 0,
    .tp_flags = Py_TPFLAGS_DEFAULT | Py_TPFLAGS_BASETYPE,
    .tp_new = LogData_new,
    .tp_init = (initproc) LogData_init,
    .tp_dealloc = (destructor) LogData_dealloc,
    .tp_members = LogData_members,
    .tp_methods = LogData_methods,
};

static PyModuleDef logdatamodule = {
    PyModuleDef_HEAD_INIT,
    "logdata_ext",
    "Module that provides a C implementation of the LogData class.",
    -1,
    NULL
};

PyMODINIT_FUNC
PyInit_logdata_ext(void) {
    PyObject *m;
    if (PyType_Ready(&LogDataType) < 0)
        return NULL;

    m = PyModule_Create(&logdatamodule);
    if (m == NULL)
        return NULL;

    Py_INCREF(&LogDataType);
    if (PyModule_AddObject(m, "LogData", (PyObject *) &LogDataType) < 0) {
        Py_DECREF(&LogDataType);
        Py_DECREF(m);
        return NULL;
    }
    return m;
}
