#include <stdbool.h>
#include <string.h>
#include <stdint.h>

size_t cobs_enc_size(size_t n) {
  return (n + 253)/254 + n;
}

size_t cobs_enc(uint8_t* out, uint8_t* in, size_t n) {
  size_t nout = 0;
  bool last_max = false;
  out[0] = 1;
  while (n-- > 0) {
    last_max = false;
    uint8_t v = *in++;
    if (v == 0) {
      nout += out[0];
      out += out[0];
      out[0] = 1;
    }
    else {
      out[out[0]++] = v;
      if (out[0] == 255) {
        nout += out[0];
        out += out[0];
        out[0] = 1;
        last_max = true;
      }
    }
  }
  if (!last_max) {
    // Implicit 0x00 terminator - so output last segment
    nout += out[0];
  }
  return nout;
}


long int cobs_dec(uint8_t* out, uint8_t* in, size_t n) {
  bool out0 = false;
  uint8_t code = 0;
  size_t nout = 0;
  if (memchr(in, 0x00, n) != NULL) return -1; // Input must not contain 0x00
  while (n > 0) {
    if (code == 0) {
      if (out0) { *out++ = 0x00; nout++; }
      code = *in++;  n--; // Code can't be 0x00 - memchr test above
      out0 = code != 255;
      code--;
    }
    else {
      *out++ = *in++;
      nout++; n--;
      code--;
    }
  }
  if (code >  0) return -2;  // Insufficient input to decode last segment
  return nout;
}





#define PY_SSIZE_T_CLEAN
#include <Python.h>

static PyObject *CobsError;

static PyObject *
cobs_enc_fn(PyObject *self, PyObject *args) {
    const char *in;
    char* out;
    Py_ssize_t n;

    if (!PyArg_ParseTuple(args, "y#", &in, &n)) {
        return NULL;
    }
    out = PyMem_Malloc(cobs_enc_size(n));
    if (out == NULL) {
      return NULL;
    }
    n = cobs_enc((uint8_t*) out, (uint8_t*) in, n);

    PyObject *r = PyBytes_FromStringAndSize(out, n);
    PyMem_Free(out);
    return r;
}

static PyObject *
cobs_dec_fn(PyObject *self, PyObject *args) {
    const char *in;
    char* out;
    Py_ssize_t n;
    PyObject *r;

    if (!PyArg_ParseTuple(args, "y#", &in, &n)) {
        return NULL;
    }
    out = PyMem_Malloc(n);
    if (out == NULL) {
      return NULL;
    }
    n = cobs_dec((uint8_t*) out, (uint8_t*) in, n);
    if (n >= 0) {
      r = PyBytes_FromStringAndSize(out, n);
    }
    else {
      r = CobsError;
    }
    PyMem_Free(out);
    return r;
}

static PyMethodDef CobsMethods[] = {
    {"enc",  cobs_enc_fn, METH_VARARGS, "CBOS encode some bytes."},
    {"dec",  cobs_dec_fn, METH_VARARGS, "CBOS decode some bytes."},
    {NULL, NULL, 0, NULL}
};

static struct PyModuleDef cobs_module = {
    PyModuleDef_HEAD_INIT,
    "cobs",   /* name of module */
    NULL, /* module documentation, may be NULL */
    -1,       /* size of per-interpreter state of the module,
                 or -1 if the module keeps state in global variables. */
    CobsMethods
};

PyMODINIT_FUNC
PyInit_cobs(void)
{
     PyObject *m;

    m = PyModule_Create(&cobs_module);
    if (m == NULL)
        return NULL;

    CobsError = PyErr_NewException("cobs.error", NULL, NULL);
    Py_XINCREF(CobsError);
    if (PyModule_AddObject(m, "error", CobsError) < 0) {
        Py_XDECREF(CobsError);
        Py_CLEAR(CobsError);
        Py_DECREF(m);
        return NULL;
    }

    return m;
}