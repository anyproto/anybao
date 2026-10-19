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

// Frees a string an anyrt_* call returned. NULL is a no-op.
void anyrt_string_free(char *s);

#ifdef __cplusplus
}
#endif

#endif
