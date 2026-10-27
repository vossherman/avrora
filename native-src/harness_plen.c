/* Experimental harness: NeoScrypt with a PARAMETERIZED password/salt length,
 * to test the hypothesis that the Windows ARM_Student build calls neoscrypt's
 * internal FastKDF with password_len = <arg3 r8 = 256> instead of the reference's
 * hardcoded 80. We include neoscrypt.c directly to reuse its static helpers.
 *
 * Usage: echo <inputhex up to 256B> | harness_plen <profile> <plen>
 *   prints 32-byte output hex on stdout.
 */
#include <stdio.h>
#include <stdint.h>
#include <string.h>
#include <stdlib.h>
#include "neoscrypt.c"

/* Verbatim copy of neoscrypt() from neoscrypt.c, with the three hardcoded 80s
 * (FastKDF password_len / salt_len) replaced by `plen`. kdf=0 path only. */
static void neoscrypt_plen(const uchar *password, uchar *output, uint profile, uint plen) {
    const size_t stack_align = 0x40;
    uint N = 128, r = 2, dblmix = 1, mixmode = 0x14;
    uint kdf, i, j;
    uint *X, *Y, *Z, *V;

    if(profile & 0x1) { N = 1024; r = 1; dblmix = 0; mixmode = 0x08; }
    if(profile >> 31) {
        N = (1 << (((profile >> 8) & 0x1F) + 1));
        r = (1 << ((profile >> 5) & 0x7));
    }

    uchar stack[(N + 3) * r * 2 * BLOCK_SIZE + stack_align];
    X = (uint *) (((size_t)stack & ~(stack_align - 1)) + stack_align);
    Z = &X[32 * r];
    Y = &X[64 * r];
    V = &X[96 * r];

    kdf = (profile >> 1) & 0xF;
    /* X = KDF(password, salt) ; password_len/salt_len parameterized */
    neoscrypt_fastkdf(password, plen, password, plen, 32, (uchar *) X, r * 2 * BLOCK_SIZE);

    if(dblmix) {
        neoscrypt_blkcpy(&Z[0], &X[0], r * 2 * BLOCK_SIZE);
        for(i = 0; i < N; i++) {
            neoscrypt_blkcpy(&V[i * (32 * r)], &Z[0], r * 2 * BLOCK_SIZE);
            neoscrypt_blkmix(&Z[0], &Y[0], r, (mixmode | 0x0100));
        }
        for(i = 0; i < N; i++) {
            j = (32 * r) * (Z[16 * (2 * r - 1)] & (N - 1));
            neoscrypt_blkxor(&Z[0], &V[j], r * 2 * BLOCK_SIZE);
            neoscrypt_blkmix(&Z[0], &Y[0], r, (mixmode | 0x0100));
        }
    }
    for(i = 0; i < N; i++) {
        neoscrypt_blkcpy(&V[i * (32 * r)], &X[0], r * 2 * BLOCK_SIZE);
        neoscrypt_blkmix(&X[0], &Y[0], r, mixmode);
    }
    for(i = 0; i < N; i++) {
        j = (32 * r) * (X[16 * (2 * r - 1)] & (N - 1));
        neoscrypt_blkxor(&X[0], &V[j], r * 2 * BLOCK_SIZE);
        neoscrypt_blkmix(&X[0], &Y[0], r, mixmode);
    }
    if(dblmix) neoscrypt_blkxor(&X[0], &Z[0], r * 2 * BLOCK_SIZE);

    /* output = KDF(password, X) ; password_len parameterized */
    neoscrypt_fastkdf(password, plen, (uchar *) X, r * 2 * BLOCK_SIZE, 32, output, 32);
    (void)kdf; (void)j;
}

static int hex2bin(const char *h, uchar *out, int maxn) {
    int n = 0;
    while(h[0] && h[1] && n < maxn) {
        if(h[0]=='\n'||h[0]=='\r'||h[0]==' ') { h++; continue; }
        unsigned v; sscanf(h, "%2x", &v); out[n++] = (uchar)v; h += 2;
    }
    return n;
}

int main(int argc, char **argv) {
    uint profile = (argc > 1) ? (uint)strtoul(argv[1], NULL, 0) : 0x80000720u;
    uint plen    = (argc > 2) ? (uint)strtoul(argv[2], NULL, 0) : 256u;
    char line[2048];
    if(!fgets(line, sizeof(line), stdin)) return 1;
    uchar in[256]; memset(in, 0, sizeof(in));
    hex2bin(line, in, 256);
    uchar out[32];
    neoscrypt_plen(in, out, profile, plen);
    for(int i = 0; i < 32; i++) printf("%02x", out[i]);
    printf("\n");
    /* reference comparison: the real neoscrypt() (hardcoded 80) */
    uchar refout[32];
    neoscrypt(in, refout, profile);
    fprintf(stderr, "ref_neoscrypt80=");
    for(int i = 0; i < 32; i++) fprintf(stderr, "%02x", refout[i]);
    fprintf(stderr, "\n");
    return 0;
}
