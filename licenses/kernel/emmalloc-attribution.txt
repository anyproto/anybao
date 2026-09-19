/*
 * Copyright 2018 The Emscripten Authors.  All rights reserved.
 * Emscripten is available under two separate licenses, the MIT license and the
 * University of Illinois/NCSA Open Source License.  Both these licenses can be
 * found in the LICENSE file.
 *
 * Simple minimalistic but efficient sbrk()-based malloc/free that works in
 * singlethreaded and multithreaded builds.
 *
 * Assumptions:
 *
 *  - sbrk() is used to claim new memory (sbrk handles geometric/linear
 *  - overallocation growth)
 *  - sbrk() can be used by other code outside emmalloc.
 *  - sbrk() is very fast in most cases (internal wasm call).
 *  - sbrk() returns pointers with an alignment of alignof(max_align_t)
 *
 * Invariants:
 *
 *  - Per-allocation header overhead is 8 bytes, smallest allocated payload
 *    amount is 8 bytes, and a multiple of 4 bytes.
 *  - Acquired memory blocks are subdivided into disjoint regions that lie
 *    next to each other.
 *  - A region is either in used or free.
 *    Used regions may be adjacent, and a used and unused region
 *    may be adjacent, but not two unused ones - they would be
 *    merged.
 *  - Memory allocation takes constant time, unless the alloc needs to sbrk()
 *    or memory is very close to being exhausted.
 *
 * Debugging:
 *
 *  - If not NDEBUG, runtime assert()s are in use.
 *  - If EMMALLOC_MEMVALIDATE is defined, a large amount of extra checks are done.
 *  - If EMMALLOC_VERBOSE is defined, a lot of operations are logged
 *    out, in addition to EMMALLOC_MEMVALIDATE.
 *  - Debugging and logging directly uses console.log via uses EM_ASM, not
 *    printf etc., to minimize any risk of debugging or logging depending on
 *    malloc.
 */
