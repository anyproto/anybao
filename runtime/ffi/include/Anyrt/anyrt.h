// anyrt's C embedding. Every call returns an owned UTF-8 JSON string;
// free it with anyrt_string_free. A failure is {"error": "..."}.
#ifndef ANYRT_H
#define ANYRT_H

#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

// `anyrt kernel check` on the embedded kernel: {"runs": [...]}, one
// report per run. Blocks for the load and the runs; call it off the
// main thread.
char *anyrt_kernel_check(uint32_t runs);

// Receives each runtime log line, from any thread.
typedef void (*anyrt_log_fn)(const char *line);

// Starts the agent against the any server at addr (http://127.0.0.1:PORT)
// with its files under data_dir (absolute): chat only, no control socket.
// Blocks until boot is done; call it off the main thread. log may be NULL.
// {"status": {...}} or {"error": "..."}.
char *anyrt_start(const char *addr, const char *data_dir, anyrt_log_fn log);

// The running agent's presence status: {"status": {...}}.
char *anyrt_status(void);

// Drains and stops the agent within deadline_ms:
// {"drained": n, "interrupted": n, "revoked": n}.
char *anyrt_stop_with_drain(uint32_t deadline_ms);

// Frees a string an anyrt_* call returned. NULL is a no-op.
void anyrt_string_free(char *s);

#ifdef __cplusplus
}
#endif

#endif
