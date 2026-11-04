/* First-run recovery of this user's own KakaoTalk numeric account ID.
 * Reads a SHA-512 account hash from stdin. No network, keys, messages, or files.
 * CommonCrypto is part of macOS libSystem. */
#include <CommonCrypto/CommonDigest.h>
#include <pthread.h>
#include <stdatomic.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

static unsigned char target[CC_SHA512_DIGEST_LENGTH];
static atomic_llong found = -1;
static atomic_ullong next_chunk = 0;
static atomic_int active = 0;
static uint64_t upper = 1000000000ULL;

static void *worker(void *unused) {
    (void)unused;
    unsigned char digest[CC_SHA512_DIGEST_LENGTH];
    char buffer[24];
    while (atomic_load(&found) < 0) {
        uint64_t start = atomic_fetch_add(&next_chunk, 100000);
        if (start >= upper) break;
        uint64_t end = start + 100000 < upper ? start + 100000 : upper;
        for (uint64_t n = start; n < end; n++) {
            if ((n & 4095) == 0 && atomic_load(&found) >= 0) break;
            uint64_t value = n;
            char *p = buffer + sizeof(buffer);
            do { *--p = '0' + value % 10; value /= 10; } while (value);
            CC_SHA512(p, (CC_LONG)(buffer + sizeof(buffer) - p), digest);
            if (memcmp(digest, target, sizeof(target)) == 0) {
                atomic_store(&found, (long long)n);
                break;
            }
        }
    }
    atomic_fetch_sub(&active, 1);
    return NULL;
}

int main(int argc, char **argv) {
    char hex[131];
    if (!fgets(hex, sizeof(hex), stdin) || strlen(hex) < 128) return 2;
    for (int i = 0; i < 64; i++) {
        unsigned int byte;
        if (sscanf(hex + i * 2, "%2x", &byte) != 1) return 2;
        target[i] = (unsigned char)byte;
    }
    int threads = argc > 1 ? atoi(argv[1]) : 4;
    if (threads < 1 || threads > 16) return 2;
    if (argc > 2) upper = strtoull(argv[2], NULL, 10);
    pthread_t ids[16];
    atomic_store(&active, threads);
    for (int i = 0; i < threads; i++) {
        if (pthread_create(&ids[i], NULL, worker, NULL) != 0) return 3;
    }
    unsigned int seconds = 0;
    while (atomic_load(&active) > 0) {
        sleep(1);
        if (++seconds % 10 == 0) {
            uint64_t count = atomic_load(&next_chunk);
            fprintf(stderr, "account recovery: %llu million candidates checked\n",
                    (unsigned long long)((count < upper ? count : upper) / 1000000));
        }
    }
    for (int i = 0; i < threads; i++) pthread_join(ids[i], NULL);
    long long answer = atomic_load(&found);
    if (answer < 0) return 1;
    printf("%lld\n", answer);
    return 0;
}
