// mp_serial_ext.c
#define PY_SSIZE_T_CLEAN
#include <Python.h>
#include <structmember.h>
#include <stdint.h>
#include <stdbool.h>
#include <stdio.h>
#include <string.h>
#include <stdlib.h>  // malloc/free

#ifdef _WIN32
  #define WIN32_LEAN_AND_MEAN
  #include <windows.h>
#else
  #include <unistd.h>
  #include <errno.h>
  #include <fcntl.h>
  #include <termios.h>
  #include <sys/select.h>
  #include <sys/time.h>
  #include <pthread.h>
  #include <sched.h>
  #include <sys/ioctl.h>
#endif

#include <inttypes.h>

static inline uint64_t now_ms(void) {
#ifdef _WIN32
    return (uint64_t)GetTickCount64();
#else
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return (uint64_t)ts.tv_sec * 1000u + (uint64_t)(ts.tv_nsec / 1000000u);
#endif
}

static inline void perf_log(const char* msg) {
#ifdef _WIN32
    OutputDebugStringA(msg);
    OutputDebugStringA("\r\n");
#endif
    fprintf(stderr, "%s\n", msg);
    fflush(stderr);
}

typedef struct {
    uint8_t* data;
    int      len;
} FrameItem;


// ----------------- SerialManager object -----------------
typedef struct {
    PyObject_HEAD
    // Config
    char* port;
    int baud;

    // Queues
    PyObject* q_in;
    PyObject* q_out;

    // Cached bound methods to reduce attribute lookups
    PyObject* q_out_put_nowait; // INCREF'ed bound method

    // State flags
    volatile int alive;       // worker loop control
    volatile int py_enabled;  // allow calling Python C-API from worker threads

#ifdef _WIN32
    // Threads and handle
    HANDLE h_read_thread;
    HANDLE h_write_thread;
    HANDLE h_port;
    HANDLE h_deliver_thread;
    // Ring buffer (shared by reader -> deliver thread)
    FrameItem* ring_buf;
    size_t ring_cap;
    size_t ring_head; // next write
    size_t ring_tail; // next read
    size_t ring_dropped;
    CRITICAL_SECTION ring_cs;
    HANDLE h_ring_event; // auto-reset event signaled when ring gains data
#else
    int fd; // serial fd
    pthread_t read_thread;
    pthread_t write_thread;
    pthread_t deliver_thread;

    // Ring buffer (shared by reader -> deliver thread)
    FrameItem* ring_buf;
    size_t ring_cap;
    size_t ring_head; // next write
    size_t ring_tail; // next read
    size_t ring_dropped;
    pthread_mutex_t ring_mx;
    pthread_cond_t ring_cond;
#endif
} SerialManagerObject;


// Elevate the current thread (reader thread) to highest priority on all platforms
static void set_current_thread_highest_priority(void) {
#ifdef _WIN32
    HANDLE h = GetCurrentThread();
    if (!SetThreadPriority(h, THREAD_PRIORITY_TIME_CRITICAL)) {
        SetThreadPriority(h, THREAD_PRIORITY_HIGHEST);
    }
    SetThreadPriorityBoost(h, FALSE);
#else
    struct sched_param sp;
    int maxp = sched_get_priority_max(SCHED_FIFO);
    if (maxp > 0) {
        sp.sched_priority = maxp;
        if (pthread_setschedparam(pthread_self(), SCHED_FIFO, &sp) != 0) {
            int maxp_rr = sched_get_priority_max(SCHED_RR);
            if (maxp_rr > 0) {
                sp.sched_priority = maxp_rr;
                if (pthread_setschedparam(pthread_self(), SCHED_RR, &sp) != 0) {
                    errno = 0;
                    nice(-20);
                }
            } else {
                errno = 0;
                nice(-20);
            }
        }
    } else {
        errno = 0;
        nice(-20);
    }
#endif
}

// ----------------- Utility: COBS decode -----------------
static int cobs_decode(const uint8_t* in, size_t in_len, uint8_t* out, size_t out_cap) {
    size_t in_idx = 0, out_idx = 0;
    while (in_idx < in_len) {
        uint8_t code = in[in_idx++];
        if (code == 0) return -1;
        size_t copy_len = (size_t)(code - 1);
        if (in_idx + copy_len > in_len) return -1;
        if (out_idx + copy_len + (code < 0xFF ? 1 : 0) > out_cap) return -1;

        memcpy(out + out_idx, in + in_idx, copy_len);
        out_idx += copy_len;
        in_idx += copy_len;

        if (code < 0xFF && in_idx < in_len) {
            out[out_idx++] = 0x00;
        }
    }
    return (int)out_idx;
}

// ----------------- Queue helpers -----------------

// Fast-path delivery: assumes GIL is already held and we have a bound method
static inline void deliver_frame_nogil(SerialManagerObject* self, const uint8_t* data, Py_ssize_t len) {
    PyObject* py_bytes = PyBytes_FromStringAndSize((const char*)data, len);
    if (py_bytes) {
        PyObject* r = PyObject_CallFunctionObjArgs(self->q_out_put_nowait, py_bytes, NULL);
        if (!r) PyErr_Clear();
        Py_XDECREF(r);
        Py_DECREF(py_bytes);
    } else {
        PyErr_Clear();
    }
}

// Ring buffer helpers (thread-safe via small mutex/critical section)
#ifdef _WIN32
static inline int ring_push(SerialManagerObject* self, const uint8_t* data, int len) {
    EnterCriticalSection(&self->ring_cs);
    size_t size = self->ring_head - self->ring_tail;
    if (size >= self->ring_cap) {
        self->ring_dropped++;
        LeaveCriticalSection(&self->ring_cs);
        return 0;
    }
    size_t idx = self->ring_head % self->ring_cap;
    uint8_t* p = (uint8_t*)malloc((size_t)len);
    if (!p) { self->ring_dropped++; LeaveCriticalSection(&self->ring_cs); return 0; }
    memcpy(p, data, (size_t)len);
    self->ring_buf[idx].data = p;
    self->ring_buf[idx].len  = len;
    self->ring_head++;
    LeaveCriticalSection(&self->ring_cs);

    // NEW: notify deliver thread that data is available
    if (self->h_ring_event) SetEvent(self->h_ring_event);
    return 1;
}


static inline int ring_pop(SerialManagerObject* self, FrameItem* out) {
    EnterCriticalSection(&self->ring_cs);
    if (self->ring_tail == self->ring_head) {
        LeaveCriticalSection(&self->ring_cs);
        return 0;
    }
    size_t idx = self->ring_tail % self->ring_cap;
    *out = self->ring_buf[idx];
    self->ring_tail++;
    LeaveCriticalSection(&self->ring_cs);
    return 1;
}
#else
static inline int ring_push(SerialManagerObject* self, const uint8_t* data, int len) {
    pthread_mutex_lock(&self->ring_mx);
    size_t size = self->ring_head - self->ring_tail;
    if (size >= self->ring_cap) {
        self->ring_dropped++;
        pthread_mutex_unlock(&self->ring_mx);
        return 0;
    }
    size_t idx = self->ring_head % self->ring_cap;
    uint8_t* p = (uint8_t*)malloc((size_t)len);
    if (!p) { self->ring_dropped++; pthread_mutex_unlock(&self->ring_mx); return 0; }
    memcpy(p, data, (size_t)len);
    self->ring_buf[idx].data = p;
    self->ring_buf[idx].len  = len;
    self->ring_head++;
    pthread_cond_signal(&self->ring_cond);
    pthread_mutex_unlock(&self->ring_mx);
    return 1;
}


static inline int ring_pop(SerialManagerObject* self, FrameItem* out) {
    pthread_mutex_lock(&self->ring_mx);
    if (self->ring_tail == self->ring_head) {
        pthread_mutex_unlock(&self->ring_mx);
        return 0;
    }
    size_t idx = self->ring_tail % self->ring_cap;
    *out = self->ring_buf[idx];
    self->ring_tail++;
    pthread_mutex_unlock(&self->ring_mx);
    return 1;
}
#endif

// Provide a flat-field ring_size helper matching SerialManagerObject members
static inline size_t ring_size(const SerialManagerObject* self) {
    size_t h = self->ring_head, t = self->ring_tail;
    return (h >= t) ? (h - t) : 0;
}


// Legacy single-frame helper (retained for any future use)
static void deliver_decoded_to_queue(PyObject* q_out, const uint8_t* data, int len) {
    PyGILState_STATE gstate = PyGILState_Ensure();
    PyObject* py_bytes = PyBytes_FromStringAndSize((const char*)data, len);
    if (py_bytes) {
        PyObject* r = PyObject_CallMethod(q_out, "put_nowait", "O", py_bytes);
        if (!r) PyErr_Clear();
        Py_XDECREF(r);
        Py_DECREF(py_bytes);
    } else {
        PyErr_Clear();
    }
    PyGILState_Release(gstate);
}

static int try_pop_write(PyObject* q_in, uint8_t* buf, size_t cap, double timeout_s, Py_ssize_t* out_len) {
    int got = 0;
    *out_len = 0;
    PyGILState_STATE gstate = PyGILState_Ensure();
    PyObject* item = NULL;

    if (timeout_s <= 0.0) {
        // Non-blocking path: q_in.get_nowait()
        item = PyObject_CallMethod(q_in, "get_nowait", NULL);
    } else {
        PyObject* py_timeout = PyFloat_FromDouble(timeout_s);
        if (!py_timeout) { PyErr_Clear(); PyGILState_Release(gstate); return 0; }
        // Correctly call get(block=True, timeout=timeout_s)
        item = PyObject_CallMethod(q_in, "get", "OO", Py_True, py_timeout);
        Py_DECREF(py_timeout);
    }

    if (item) {
        if (PyBytes_Check(item)) {
            char* p; Py_ssize_t n;
            if (PyBytes_AsStringAndSize(item, &p, &n) == 0 && (size_t)n <= cap) {
                memcpy(buf, p, (size_t)n);
                *out_len = n;
                got = 1;
            }
        }
        Py_DECREF(item);
    } else {
        PyErr_Clear();
    }
    PyGILState_Release(gstate);
    return got;
}

// ----------------- Platform-specific serial I/O -----------------
#ifdef _WIN32

static void cancel_all_io_win(HANDLE h) {
    if (!h || h == INVALID_HANDLE_VALUE) return;
    CancelIoEx(h, NULL);
    DWORD ce = 0; COMSTAT st = {0};
    ClearCommError(h, &ce, &st);
    PurgeComm(h, PURGE_RXABORT | PURGE_TXABORT | PURGE_RXCLEAR | PURGE_TXCLEAR);
}

static HANDLE open_serial_win(const char* port, int baud) {
    char name[256];
    if (strncmp(port, "\\\\.\\", 4) == 0) {
        strncpy(name, port, sizeof(name)-1);
        name[sizeof(name)-1] = 0;
    } else {
        snprintf(name, sizeof(name), "\\\\.\\%s", port);
    }

    HANDLE h = CreateFileA(
        name, GENERIC_READ | GENERIC_WRITE, 0, NULL,
        OPEN_EXISTING, FILE_FLAG_OVERLAPPED, NULL);
    if (h == INVALID_HANDLE_VALUE) {
        fprintf(stderr, "CreateFile failed for %s, err=%lu\n", name, GetLastError());
        return INVALID_HANDLE_VALUE;
    }

    SetupComm(h, 64 * 1024, 4 * 1024);
    PurgeComm(h, PURGE_RXCLEAR | PURGE_TXCLEAR | PURGE_RXABORT | PURGE_TXABORT);

    DCB dcb = (DCB){0};
    dcb.DCBlength = sizeof(DCB);
    if (!GetCommState(h, &dcb)) {
        fprintf(stderr, "GetCommState failed, err=%lu\n", GetLastError());
        CloseHandle(h);
        return INVALID_HANDLE_VALUE;
    }

    dcb.fBinary       = TRUE;
    dcb.fAbortOnError = FALSE;

    dcb.BaudRate = baud;
    dcb.ByteSize = 8;
    dcb.Parity   = NOPARITY;
    dcb.StopBits = ONESTOPBIT;

    dcb.fOutxCtsFlow = FALSE;
    dcb.fOutxDsrFlow = FALSE;
    dcb.fOutX = FALSE;
    dcb.fInX  = FALSE;

    dcb.fDtrControl = DTR_CONTROL_ENABLE;
    dcb.fRtsControl = RTS_CONTROL_ENABLE;

    if (!SetCommState(h, &dcb)) {
        fprintf(stderr, "SetCommState failed, err=%lu\n", GetLastError());
        CloseHandle(h);
        return INVALID_HANDLE_VALUE;
    }

    // Set no timeouts (non-blocking behavior controlled by our read logic)
    COMMTIMEOUTS to = (COMMTIMEOUTS){0};
    to.ReadIntervalTimeout = 0;
    to.ReadTotalTimeoutMultiplier = 0;
    to.ReadTotalTimeoutConstant = 0;
    to.WriteTotalTimeoutMultiplier = 0;
    to.WriteTotalTimeoutConstant = 0;
    if (!SetCommTimeouts(h, &to)) {
        fprintf(stderr, "SetCommTimeouts failed, err=%lu\n", GetLastError());
        CloseHandle(h);
        return INVALID_HANDLE_VALUE;
    }

    if (!SetCommMask(h, EV_RXCHAR | EV_ERR | EV_BREAK)) {
        fprintf(stderr, "SetCommMask failed, err=%lu\n", GetLastError());
        CloseHandle(h);
        return INVALID_HANDLE_VALUE;
    }

    DWORD ce = 0; COMSTAT st = {0};
    ClearCommError(h, &ce, &st);
    EscapeCommFunction(h, SETRTS);
    EscapeCommFunction(h, SETDTR);
    EscapeCommFunction(h, CLRDTR);
    Sleep(10);
    EscapeCommFunction(h, SETDTR);

    return h;
}

static int serial_write_win(HANDLE h, const uint8_t* data, size_t len) {
    OVERLAPPED ov = (OVERLAPPED){0};
    ov.hEvent = CreateEvent(NULL, TRUE, FALSE, NULL);
    if (!ov.hEvent) return -1;

    DWORD written = 0;
    BOOL ok = WriteFile(h, data, (DWORD)len, NULL, &ov);
    if (!ok) {
        DWORD err = GetLastError();
        if (err == ERROR_IO_PENDING) {
            DWORD wait_rc = WaitForSingleObject(ov.hEvent, 2000);
            if (wait_rc == WAIT_TIMEOUT) {
                if (!CancelIoEx(h, &ov)) CancelIo(h);
                DWORD ce = 0; COMSTAT st = {0};
                ClearCommError(h, &ce, &st);
                CloseHandle(ov.hEvent);
                return 0;
            }
            if (!GetOverlappedResult(h, &ov, &written, FALSE)) {
                CloseHandle(ov.hEvent);
                return -1;
            }
        } else {
            CloseHandle(ov.hEvent);
            return -1;
        }
    } else {
        if (!GetOverlappedResult(h, &ov, &written, TRUE)) {
            CloseHandle(ov.hEvent);
            return -1;
        }
    }

    CloseHandle(ov.hEvent);
    return (int)written;
}

// Windows non-blocking read: we poll cbInQue; if zero => return 0.
// If > 0, issue overlapped ReadFile and allow a minimal wait so completion
// can materialize; otherwise treat as no data.
static int serial_read_win(HANDLE h, uint8_t* buf, size_t cap) {
    DWORD errs = 0; COMSTAT st = {0};
    ClearCommError(h, &errs, &st);

    if (st.cbInQue == 0) {
        return 0;
    }

    DWORD to_read = st.cbInQue;
    if (to_read > cap) to_read = (DWORD)cap;

    OVERLAPPED ov = (OVERLAPPED){0};
    ov.hEvent = CreateEvent(NULL, TRUE, FALSE, NULL);
    if (!ov.hEvent) return -1;

    DWORD got = 0;
    BOOL ok = ReadFile(h, buf, to_read, NULL, &ov);
    if (!ok) {
        DWORD err = GetLastError();
        if (err == ERROR_IO_PENDING) {
            // Slightly longer wait to avoid busy loop while still low latency
            DWORD rc = WaitForSingleObject(ov.hEvent, 3);
            if (rc == WAIT_TIMEOUT) {
                CancelIoEx(h, &ov);
                DWORD ce2 = 0; COMSTAT st2 = {0};
                ClearCommError(h, &ce2, &st2);
                CloseHandle(ov.hEvent);
                return 0;
            }
            if (!GetOverlappedResult(h, &ov, &got, FALSE)) {
                CloseHandle(ov.hEvent);
                return -1;
            }
        } else if (err == ERROR_OPERATION_ABORTED) {
            CloseHandle(ov.hEvent);
            return 0;
        } else {
            CloseHandle(ov.hEvent);
            return -1;
        }
    } else {
        if (!GetOverlappedResult(h, &ov, &got, TRUE)) {
            CloseHandle(ov.hEvent);
            return -1;
        }
    }

    CloseHandle(ov.hEvent);
    return (int)got;
}

static void close_serial_win(HANDLE h) {
    if (h && h != INVALID_HANDLE_VALUE) {
        CloseHandle(h);
    }
}

#else // POSIX

static int set_interface_attribs(int fd, int speed) {
    struct termios tty;
    if (tcgetattr(fd, &tty) != 0) {
        perror("tcgetattr");
        return -1;
    }
    cfmakeraw(&tty);

    speed_t spd = B115200;
    switch (speed) {
        case 9600: spd = B9600; break;
        case 19200: spd = B19200; break;
        case 38400: spd = B38400; break;
        case 57600: spd = B57600; break;
        case 115200: spd = B115200; break;
        default: spd = B115200; break;
    }
    cfsetispeed(&tty, spd);
    cfsetospeed(&tty, spd);

    tty.c_cflag |= (CLOCAL | CREAD);
    tty.c_cflag &= ~CSIZE;
    tty.c_cflag |= CS8;
    tty.c_cflag &= ~(PARENB | CSTOPB | CRTSCTS);

    // Non-blocking read by default
    tty.c_cc[VTIME] = 0;
    tty.c_cc[VMIN]  = 0;

    if (tcsetattr(fd, TCSANOW, &tty) != 0) {
        perror("tcsetattr");
        return -1;
    }
    return 0;
}

static int open_serial_posix(const char* port, int baud) {
    int fd = open(port, O_RDWR | O_NOCTTY | O_NONBLOCK);
    if (fd < 0) {
        perror("open");
        return -1;
    }
    if (set_interface_attribs(fd, baud) != 0) {
        close(fd);
        return -1;
    }
    return fd;
}

static int serial_write_posix(int fd, const uint8_t* data, size_t len) {
    ssize_t w = write(fd, data, len);
    if (w < 0) return -1;
    return (int)w;
}

// POSIX non-blocking drain: read() until EAGAIN/EWOULDBLOCK or cap reached.
static int serial_read_posix(int fd, uint8_t* buf, size_t cap) {
    if (cap == 0) return 0;

    ssize_t total = 0;
    for (;;) {
        ssize_t r = read(fd, buf + total, cap - (size_t)total);
        if (r > 0) {
            total += r;
            if ((size_t)total >= cap) break;
            continue;
        }
        if (r == 0) {
            break;
        }
        if (r < 0) {
            if (errno == EINTR) continue;
            if (errno == EAGAIN || errno == EWOULDBLOCK) break;
            return -1;
        }
    }
    return (int)total;
}

static inline int bytes_available_posix(int fd) {
    int avail = 0;
    if (ioctl(fd, FIONREAD, &avail) < 0) {
        return 0;
    }
    return (avail > 0) ? avail : 0;
}

static void close_serial_posix(int fd) {
    if (fd >= 0) close(fd);
}


#endif

// ----------------- Threads -----------------
#ifdef _WIN32

static void wait_and_close_thread(HANDLE* phThread) {
    if (*phThread) {
        WaitForSingleObject(*phThread, INFINITE);
        CloseHandle(*phThread);
        *phThread = NULL;
    }
}

// Wait for EV_RXCHAR (or error/break) with a short timeout.
// Returns 1 if an event fired, 0 on timeout, -1 on error.
static int wait_for_rx_win(HANDLE h, DWORD timeout_ms) {
    DWORD mask = 0;
    OVERLAPPED ov = {0};
    ov.hEvent = CreateEvent(NULL, TRUE, FALSE, NULL);
    if (!ov.hEvent) return -1;

    // Ensure we’re listening for RX events
    if (!SetCommMask(h, EV_RXCHAR | EV_ERR | EV_BREAK)) {
        CloseHandle(ov.hEvent);
        return -1;
    }

    BOOL ok = WaitCommEvent(h, &mask, &ov);
    if (!ok) {
        DWORD err = GetLastError();
        if (err == ERROR_IO_PENDING) {
            DWORD rc = WaitForSingleObject(ov.hEvent, timeout_ms);
            if (rc == WAIT_TIMEOUT) {
                CancelIoEx(h, &ov);
                DWORD ce = 0; COMSTAT st = {0};
                ClearCommError(h, &ce, &st);
                CloseHandle(ov.hEvent);
                return 0; // no event in time
            }
            // completion occurred; fall-through to success
        } else if (err == ERROR_INVALID_PARAMETER) {
            // Some drivers don’t signal events reliably; treat as timeout
            CloseHandle(ov.hEvent);
            return 0;
        } else if (err == ERROR_OPERATION_ABORTED) {
            CloseHandle(ov.hEvent);
            return 0;
        } else {
            CloseHandle(ov.hEvent);
            return -1;
        }
    } else {
        // Synchronous success; mask already set
        (void)mask;
    }

    CloseHandle(ov.hEvent);
    return 1;
}


static DWORD WINAPI reader_thread_win(LPVOID param) {
    SerialManagerObject* self = (SerialManagerObject*)param;

    uint8_t inbuf[16384];
    uint8_t framebuf[65536];
    size_t  frame_len = 0;

    set_current_thread_highest_priority();

    int idle_backoff_ms = 0; // adaptive: 0, 1, 2, 3 (cap small to keep latency)

    while (self->alive) {
        if (!self->h_port || self->h_port == INVALID_HANDLE_VALUE) break;

        // Non-blocking drain
        int n = serial_read_win(self->h_port, inbuf, sizeof(inbuf));
        if (!self->alive) break;

        if (n < 0) {
            DWORD ce = 0; COMSTAT st = {0};
            ClearCommError(self->h_port, &ce, &st);
            Sleep(10);
            continue;
        }

        if (n > 0) {
            // got data -> reset backoff
            idle_backoff_ms = 0;

            const uint8_t* p   = inbuf;
            const uint8_t* end = inbuf + n;

            while (p < end) {
                const uint8_t* z = (const uint8_t*)memchr(p, 0x00, (size_t)(end - p));
                const uint8_t* q = z ? z : end;

                size_t chunk = (size_t)(q - p);
                if (chunk) {
                    if (frame_len + chunk <= sizeof(framebuf)) {
                        memcpy(framebuf + frame_len, p, chunk);
                        frame_len += chunk;
                    } else {
                        frame_len = 0; // overflow -> resync
                    }
                }

                if (z) {
                    if (frame_len > 0 && self->py_enabled) {
                        uint8_t tmp[65536];
                        int olen = cobs_decode(framebuf, frame_len, tmp, sizeof(tmp));
                        if (olen >= 0) {
                            (void)ring_push(self, tmp, olen);
                        }
                    }
                    frame_len = 0;
                    p = z + 1;
                } else {
                    break;
                }
            }
            continue;
        }

        // n == 0: nothing available right now.
        // Use event-driven wait to avoid spinning.
        int ev = wait_for_rx_win(self->h_port, 3);
        if (ev == 1) {
            // Event fired, loop to read
            continue;
        } else if (ev < 0) {
            // Treat as transient error; small sleep to avoid hot loop
            Sleep(2);
        } else {
            // Timeout: apply tiny adaptive backoff
            if (idle_backoff_ms < 3) idle_backoff_ms++;
            Sleep(idle_backoff_ms);
        }
    }
    return 0;
}


static DWORD WINAPI deliver_thread_win(LPVOID param) {
    SerialManagerObject* self = (SerialManagerObject*)param;

    while (self->alive) {
        if (self->ring_tail == self->ring_head) {
            // Wait until producer signals there is data; short timeout keeps responsiveness during shutdown
            if (self->h_ring_event) {
                (void)WaitForSingleObject(self->h_ring_event, 5);
            } else {
                Sleep(0); // fallback
            }
            continue;
        }

        if (!self->py_enabled) { Sleep(10); continue; }

        PyGILState_STATE g = PyGILState_Ensure();
        for (int i = 0; i < 2048; ++i) {
            FrameItem it;
            if (!ring_pop(self, &it)) break;
            if (it.data && it.len > 0) {
                deliver_frame_nogil(self, it.data, (Py_ssize_t)it.len);
                free(it.data);
            }
            if (!self->alive || !self->py_enabled) break;
        }
        PyGILState_Release(g);
        // loop back quickly to check for more items (batching already in place)
    }

    // Drain remaining frames
    FrameItem it;
    while (ring_pop(self, &it)) {
        if (it.data) free(it.data);
    }
    return 0;
}




static DWORD WINAPI writer_thread_win(LPVOID param) {
    SerialManagerObject* self = (SerialManagerObject*)param;
    uint8_t buf[65536];

    while (self->alive) {
        if (!self->py_enabled) break; // don't touch Python C-API after shutdown begins

        Py_ssize_t n = 0;
        // Strictly non-blocking to avoid Python C-API during shutdown races
        int got = try_pop_write(self->q_in, buf, sizeof(buf), 0.001, &n);
        if (!got) {
            // No data; yield
            // NOTE: !! changing this to 1ms causes firmware download to be SLOW !!
            Sleep(0);
            continue;
        }

        if (!self->alive || !self->py_enabled) break;
        if (!self->h_port || self->h_port == INVALID_HANDLE_VALUE) break;

        // Batch: drain more queued items without blocking to coalesce writes
        Py_ssize_t total = n;
        while (total < (Py_ssize_t)sizeof(buf)) {
            Py_ssize_t m = 0;
            if (!try_pop_write(self->q_in, buf + total, sizeof(buf) - (size_t)total, 0.0, &m))
                break; // queue empty
            total += m;
        }

        // Single OS write for the batch
        if (total > 0) {
            (void)serial_write_win(self->h_port, buf, (size_t)total);
        }
    }
    return 0;
}

#else
static void* reader_thread_posix(void* param) {
    SerialManagerObject* self = (SerialManagerObject*)param;

    // IO + framing
    uint8_t inbuf[16384];
    uint8_t framebuf[65536];
    size_t  frame_len = 0;

    set_current_thread_highest_priority();

    while (self->alive) {
        fd_set read_fds;
        FD_ZERO(&read_fds);
        if (self->fd < 0) break;
        FD_SET(self->fd, &read_fds);

        struct timeval timeout;
        timeout.tv_sec = 0;
        timeout.tv_usec = 100000; // 100ms timeout to remain responsive

        int rv = select(self->fd + 1, &read_fds, NULL, NULL, &timeout);
        if (!self->alive) break;

        if (rv < 0) {
            if (errno == EINTR) continue; // Interrupted by signal, just loop again
            break; // A real error occurred
        }
        if (rv == 0) {
            continue; // Timeout, no data. Loop to check self->alive.
        }

        // Data is available, so we read it.
        int n = serial_read_posix(self->fd, inbuf, sizeof(inbuf));
        if (!self->alive) break;

        if (n <= 0) {
            // Error or EOF, either way, we can't continue.
            break;
        }

        const uint8_t* p   = inbuf;
        const uint8_t* end = inbuf + n;

        while (p < end) {
            const uint8_t* z = (const uint8_t*)memchr(p, 0x00, (size_t)(end - p));
            const uint8_t* q = z ? z : end;

            size_t chunk = (size_t)(q - p);
            if (chunk) {
                if (frame_len + chunk <= sizeof(framebuf)) {
                    memcpy(framebuf + frame_len, p, chunk);
                    frame_len += chunk;
                } else {
                    // overflow -> drop partial to resync
                    frame_len = 0;
                }
            }

            if (z) {
                if (frame_len > 0 && self->py_enabled) {
                    uint8_t tmp[65536];
                    int olen = cobs_decode(framebuf, frame_len, tmp, sizeof(tmp));
                    if (olen >= 0) {
                        ring_push(self, tmp, olen);
                    }
                }
                frame_len = 0;
                p = z + 1;
            } else {
                break;
            }

            if (!self->alive) break;
        }
    }
    return NULL;
}

static void* deliver_thread_posix(void* param) {
    SerialManagerObject* self = (SerialManagerObject*)param;
    FrameItem item_batch[128];

    while (self->alive) {
        if (!self->py_enabled) { usleep(200); continue; }

        int batch_count = 0;

        pthread_mutex_lock(&self->ring_mx);
        while (self->alive && self->ring_head == self->ring_tail) {
            struct timespec ts;
            clock_gettime(CLOCK_REALTIME, &ts);
            ts.tv_nsec += 100 * 1000 * 1000; // 100ms timeout
            if (ts.tv_nsec >= 1000000000L) {
                ts.tv_sec++;
                ts.tv_nsec -= 1000000000L;
            }
            pthread_cond_timedwait(&self->ring_cond, &self->ring_mx, &ts);
        }

        if (!self->alive) {
            pthread_mutex_unlock(&self->ring_mx);
            break;
        }

        while (batch_count < 128 && self->ring_tail != self->ring_head) {
            size_t idx = self->ring_tail % self->ring_cap;
            item_batch[batch_count++] = self->ring_buf[idx];
            self->ring_tail++;
        }
        pthread_mutex_unlock(&self->ring_mx);

        if (batch_count > 0) {
            PyGILState_STATE g = PyGILState_Ensure();
            for (int i = 0; i < batch_count; ++i) {
                FrameItem* it = &item_batch[i];
                if (it->data && it->len > 0) {
                    deliver_frame_nogil(self, it->data, (Py_ssize_t)it->len);
                    free(it->data);
                }
                if (!self->alive || !self->py_enabled) break;
            }
            PyGILState_Release(g);
        }
    }

    // Drain remaining items (if any) to avoid leaks
    FrameItem it;
    while (ring_pop(self, &it)) {
        if (it.data) free(it.data);
    }
    return NULL;
}


static void* writer_thread_posix(void* param) {
    SerialManagerObject* self = (SerialManagerObject*)param;
    uint8_t buf[65536];

    while (self->alive) {
        if (!self->py_enabled) break;

        Py_ssize_t n = 0;
        int got = try_pop_write(self->q_in, buf, sizeof(buf), 0.001, &n); // non-blocking
        if (!got) {
            // No data; yield briefly
            usleep(500);
            continue;
        }

        if (!self->alive || !self->py_enabled) break;

        // Batch: drain more queued items to coalesce writes
        Py_ssize_t total = n;
        while (total < (Py_ssize_t)sizeof(buf)) {
            Py_ssize_t m = 0;
            if (!try_pop_write(self->q_in, buf + total, sizeof(buf) - (size_t)total, 0.0, &m))
                break;
            total += m;
        }

        if (self->fd >= 0 && total > 0) {
            (void)serial_write_posix(self->fd, buf, (size_t)total);
        }
    }
    return NULL;
}

#endif

// ----------------- Python type: SerialManager -----------------
static void SerialManager_dealloc(SerialManagerObject* self) {
    self->py_enabled = 0;
    self->alive = 0;
#ifdef _WIN32
    if (self->h_port && self->h_port != INVALID_HANDLE_VALUE) {
        SetCommMask(self->h_port, 0);
        cancel_all_io_win(self->h_port);
    }

    Py_BEGIN_ALLOW_THREADS
    wait_and_close_thread(&self->h_read_thread);
    wait_and_close_thread(&self->h_write_thread);
    wait_and_close_thread(&self->h_deliver_thread);
    Py_END_ALLOW_THREADS

    if (self->h_port && self->h_port != INVALID_HANDLE_VALUE) {
        close_serial_win(self->h_port);
        self->h_port = NULL;
    }
    if (self->ring_buf) {
        while (self->ring_tail != self->ring_head) {
            size_t idx = self->ring_tail % self->ring_cap;
            if (self->ring_buf[idx].data) free(self->ring_buf[idx].data);
            self->ring_tail++;
        }
        free(self->ring_buf);
        self->ring_buf = NULL;
    }
    if (self->h_ring_event) { CloseHandle(self->h_ring_event); self->h_ring_event = NULL; }
    DeleteCriticalSection(&self->ring_cs);
#else
    Py_BEGIN_ALLOW_THREADS
    if (self->read_thread)  { pthread_join(self->read_thread,  NULL); self->read_thread = (pthread_t)0; }
    if (self->write_thread) { pthread_join(self->write_thread, NULL); self->write_thread = (pthread_t)0; }
    if (self->deliver_thread) { pthread_join(self->deliver_thread, NULL); self->deliver_thread = (pthread_t)0; }
    Py_END_ALLOW_THREADS

    if (self->fd >= 0) { close_serial_posix(self->fd); self->fd = -1; }
    if (self->ring_buf) {
        while (self->ring_tail != self->ring_head) {
            size_t idx = self->ring_tail % self->ring_cap;
            if (self->ring_buf[idx].data) free(self->ring_buf[idx].data);
            self->ring_tail++;
        }
        free(self->ring_buf);
        self->ring_buf = NULL;
    }
    pthread_mutex_destroy(&self->ring_mx);
    pthread_cond_destroy(&self->ring_cond);
#endif
    Py_XDECREF(self->q_in);
    Py_XDECREF(self->q_out);
    Py_XDECREF(self->q_out_put_nowait);
    PyMem_Free(self->port);
    Py_TYPE(self)->tp_free((PyObject*)self);
}



static int SerialManager_init(SerialManagerObject* self, PyObject* args, PyObject* kwds) {
    // __init__(port, qin, qout, baud=115200)
    static char* kwlist[] = {"port", "qin", "qout", "baud", NULL};
    const char* port = NULL;
    int baud = 115200;
    PyObject* qin = NULL;
    PyObject* qout = NULL;

    self->q_in = NULL;
    self->q_out = NULL;
    self->q_out_put_nowait = NULL;

    if (!PyArg_ParseTupleAndKeywords(args, kwds, "sOO|i", kwlist,
                                     &port, &qin, &qout, &baud)) {
        return -1;
    }
    if (!PyObject_HasAttrString(qin, "get") || !PyObject_HasAttrString(qout, "put_nowait")) {
        PyErr_SetString(PyExc_ValueError, "qin/qout must be queue-like objects");
        return -1;
    }

    self->port = PyMem_Malloc(strlen(port) + 1);
    if (!self->port) return -1;
    strcpy(self->port, port);
    self->baud = baud;

    Py_INCREF(qin);  self->q_in  = qin;
    Py_INCREF(qout); self->q_out = qout;

    // Cache bound method q_out.put_nowait once
    self->q_out_put_nowait = PyObject_GetAttrString(qout, "put_nowait");
    if (!self->q_out_put_nowait) {
        PyErr_SetString(PyExc_ValueError, "qout must provide put_nowait()");
        return -1;
    }

    self->alive = 0;
    self->py_enabled = 0;
#ifdef _WIN32
    self->h_read_thread = NULL;
    self->h_write_thread = NULL;
    self->h_deliver_thread = NULL;
    self->h_port = NULL;

    // Init ring
    self->ring_cap = 4096; // frames
    self->ring_buf = (FrameItem*)calloc(self->ring_cap, sizeof(FrameItem));
    self->ring_head = 0;
    self->ring_tail = 0;
    self->ring_dropped = 0;
    if (!self->ring_buf) {
        PyErr_SetString(PyExc_MemoryError, "ring allocation failed");
        return -1;
    }
    InitializeCriticalSection(&self->ring_cs);
    self->h_ring_event = CreateEvent(NULL, /*manualReset*/FALSE, /*initialState*/FALSE, NULL);
    if (!self->h_ring_event) {
        PyErr_SetString(PyExc_RuntimeError, "CreateEvent failed for ring");
        return -1;
    }
#else
    self->fd = -1;
    self->read_thread = (pthread_t)0;
    self->write_thread = (pthread_t)0;
    self->deliver_thread = (pthread_t)0;

    // Init ring
    self->ring_cap = 4096; // frames
    self->ring_buf = (FrameItem*)calloc(self->ring_cap, sizeof(FrameItem));
    self->ring_head = 0;
    self->ring_tail = 0;
    self->ring_dropped = 0;
    if (!self->ring_buf) {
        PyErr_SetString(PyExc_MemoryError, "ring allocation failed");
        return -1;
    }
    pthread_mutex_init(&self->ring_mx, NULL);
    pthread_cond_init(&self->ring_cond, NULL);
#endif
    return 0;
}


static PyObject* SerialManager_start(SerialManagerObject* self, PyObject* Py_UNUSED(ignored)) {
#ifdef _WIN32
    if (self->h_port && self->h_port != INVALID_HANDLE_VALUE) Py_RETURN_NONE;
    HANDLE h = open_serial_win(self->port, self->baud);
    if (h == INVALID_HANDLE_VALUE) {
        return PyErr_Format(PyExc_OSError, "failed to open serial '%s'", self->port);
    }
    self->h_port = h;
#else
    if (self->fd >= 0) Py_RETURN_NONE;
    int fd = open_serial_posix(self->port, self->baud);
    if (fd < 0) {
        return PyErr_Format(PyExc_OSError, "failed to open serial '%s'", self->port);
    }
    self->fd = fd;
#endif

    self->py_enabled = 1; // allow worker threads to use Python C-API
    self->alive = 1;

#ifdef _WIN32
    self->h_read_thread  = CreateThread(NULL, 0, reader_thread_win, self, 0, NULL);
    self->h_write_thread = CreateThread(NULL, 0, writer_thread_win, self, 0, NULL);
    self->h_deliver_thread = CreateThread(NULL, 0, deliver_thread_win, self, 0, NULL);
#else
    pthread_create(&self->read_thread,  NULL, reader_thread_posix, self);
    pthread_create(&self->write_thread, NULL, writer_thread_posix, self);
    pthread_create(&self->deliver_thread, NULL, deliver_thread_posix, self);
#endif

    Py_RETURN_NONE;
}


static PyObject* SerialManager_is_running(SerialManagerObject* self, PyObject* Py_UNUSED(ignored)) {
#ifdef _WIN32
    if (self->alive && self->py_enabled && self->h_port && self->h_port != INVALID_HANDLE_VALUE) Py_RETURN_TRUE;
#else
    if (self->alive && self->py_enabled && self->fd >= 0) Py_RETURN_TRUE;
#endif
    Py_RETURN_FALSE;
}


static PyObject* SerialManager_shutdown(SerialManagerObject* self, PyObject* Py_UNUSED(ignored)) {
    self->py_enabled = 0;
    self->alive = 0;

#ifdef _WIN32
    if (self->h_port && self->h_port != INVALID_HANDLE_VALUE) {
        SetCommMask(self->h_port, 0);
        cancel_all_io_win(self->h_port);
        EscapeCommFunction(self->h_port, CLRDTR);
        EscapeCommFunction(self->h_port, CLRRTS);
    }

    Py_BEGIN_ALLOW_THREADS
    wait_and_close_thread(&self->h_read_thread);
    wait_and_close_thread(&self->h_write_thread);
    wait_and_close_thread(&self->h_deliver_thread);
    Py_END_ALLOW_THREADS

    if (self->h_port && self->h_port != INVALID_HANDLE_VALUE) {
        CloseHandle(self->h_port);
        self->h_port = NULL;
    }

    if (self->h_ring_event) { SetEvent(self->h_ring_event); } // wake waiter, if any

    if (self->ring_buf) {
        while (self->ring_tail != self->ring_head) {
            size_t idx = self->ring_tail % self->ring_cap;
            if (self->ring_buf[idx].data) free(self->ring_buf[idx].data);
            self->ring_tail++;
        }
        free(self->ring_buf);
        self->ring_buf = NULL;
    }
#else
    Py_BEGIN_ALLOW_THREADS
    if (self->read_thread)    { pthread_join(self->read_thread,    NULL); self->read_thread = (pthread_t)0; }
    if (self->write_thread)   { pthread_join(self->write_thread,   NULL); self->write_thread = (pthread_t)0; }
    if (self->deliver_thread) { pthread_join(self->deliver_thread, NULL); self->deliver_thread = (pthread_t)0; }
    Py_END_ALLOW_THREADS

    if (self->fd >= 0) {
        close_serial_posix(self->fd);
        self->fd = -1;
    }

    if (self->ring_buf) {
        while (self->ring_tail != self->ring_head) {
            size_t idx = self->ring_tail % self->ring_cap;
            if (self->ring_buf[idx].data) free(self->ring_buf[idx].data);
            self->ring_tail++;
        }
        free(self->ring_buf);
        self->ring_buf = NULL;
    }
#endif
    Py_RETURN_NONE;
}




// ----------------- Type and module boilerplate -----------------
static PyMethodDef SerialManager_methods[] = {
    {"start", (PyCFunction)SerialManager_start, METH_NOARGS, "Start I/O threads"},
    {"is_running", (PyCFunction)SerialManager_is_running, METH_NOARGS, "Return whether the I/O threads are running"},
    {"shutdown", (PyCFunction)SerialManager_shutdown, METH_NOARGS, "Stop threads and close the serial port"},
    {NULL, NULL, 0, NULL}
};

static PyMemberDef SerialManager_members[] = {
    {"baud",       T_INT, offsetof(SerialManagerObject, baud),       0, "baud rate"},
    {NULL}
};

static PyTypeObject SerialManagerType = {
    PyVarObject_HEAD_INIT(NULL, 0)
    .tp_name = "mp_serial_ext.SerialManager",
    .tp_basicsize = sizeof(SerialManagerObject),
    .tp_dealloc = (destructor)SerialManager_dealloc,
    .tp_flags = Py_TPFLAGS_DEFAULT,
    .tp_doc = "Serial manager (native, non-blocking)",
    .tp_methods = SerialManager_methods,
    .tp_members = SerialManager_members,
    .tp_init = (initproc)SerialManager_init,
    .tp_new = PyType_GenericNew,
};

static PyMethodDef module_methods[] = {
    {NULL, NULL, 0, NULL}
};

static struct PyModuleDef moduledef = {
    PyModuleDef_HEAD_INIT,
    "mp_serial_ext",
    "Serial extension",
    -1,
    module_methods,
    NULL, NULL, NULL, NULL
};

PyMODINIT_FUNC PyInit_mp_serial_ext(void) {
    PyObject* m;
    if (PyType_Ready(&SerialManagerType) < 0)
        return NULL;

    m = PyModule_Create(&moduledef);
    if (m == NULL)
        return NULL;

    Py_INCREF(&SerialManagerType);
    if (PyModule_AddObject(m, "SerialManager", (PyObject *)&SerialManagerType) < 0) {
        Py_DECREF(&SerialManagerType);
        Py_DECREF(m);
        return NULL;
    }

    return m;
}
