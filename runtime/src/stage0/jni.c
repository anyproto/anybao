/* ADR-034 §0: JNI only on the calling Kotlin background thread. Rust execution
 * never calls Java. UTF-16 preserves arbitrary JSON without modified-UTF-8 loss. */
#include <jni.h>
#include <stddef.h>
#include <stdint.h>

typedef struct { uint16_t *ptr; size_t len; } BaoText;
extern BaoText bao_stage0_request(const uint16_t *input, size_t len);
extern void bao_stage0_free(BaoText text);

jstring bao_stage0_jni_request(JNIEnv *env, jstring input) {
    if (input == NULL) return NULL;
    jsize len = (*env)->GetStringLength(env, input);
    const jchar *chars = (*env)->GetStringChars(env, input, NULL);
    if (chars == NULL) return NULL;
    BaoText reply = bao_stage0_request(chars, (size_t)len);
    (*env)->ReleaseStringChars(env, input, chars);
    jstring result = (*env)->NewString(env, reply.ptr, (jsize)reply.len);
    bao_stage0_free(reply);
    return result;
}
