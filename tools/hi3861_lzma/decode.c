/*
 * Independent bounded-buffer LZMA decoder for CP4 Hi3861L OTA streams.
 *
 * This is a small file-to-file harness over the public-domain LZMA SDK 9.20
 * C decoder. It deliberately uses the older SDK and streams the input in
 * caller-selected chunks, providing a check that is independent of Python's
 * liblzma and of the SDK 19.00 encoder used to build the image.
 *
 * Usage: decode INPUT OUTPUT CHUNK
 *
 * INPUT is a .lzma-alone stream with a 13-byte header and a known output
 * size. CHUNK is 1..4096 input bytes per call. The program writes the
 * expanded file and prints a one-line JSON result to stdout.
 *
 * This program performs no device I/O.
 */

#include <stdio.h>
#include <stdlib.h>
#include <stdint.h>

#include "LzmaDec.h"

#define MAX_INPUT (16u * 1024u * 1024u)
#define MAX_OUTPUT (16u * 1024u * 1024u)
#define OUTPUT_CHUNK 4096u

static size_t in_use = 0;
static size_t in_peak = 0;

static void *allocate(void *opaque, size_t size)
{
    size_t *block;
    (void)opaque;
    block = (size_t *)malloc(size + sizeof(size_t));
    if (block == NULL) {
        return NULL;
    }
    *block = size;
    in_use += size;
    if (in_use > in_peak) {
        in_peak = in_use;
    }
    return block + 1;
}

static void release(void *opaque, void *address)
{
    size_t *block;
    (void)opaque;
    if (address != NULL) {
        block = (size_t *)address - 1;
        in_use -= *block;
        free(block);
    }
}

static Byte *read_file(const char *path, size_t *length)
{
    FILE *file = fopen(path, "rb");
    long size;
    Byte *buffer;
    size_t count;

    if (file == NULL) {
        fprintf(stderr, "decode: cannot open %s\n", path);
        return NULL;
    }
    if (fseek(file, 0, SEEK_END) != 0) {
        fclose(file);
        fprintf(stderr, "decode: cannot seek %s\n", path);
        return NULL;
    }
    size = ftell(file);
    if (size < 13 || (unsigned long)size > MAX_INPUT) {
        fclose(file);
        fprintf(stderr, "decode: %s is outside 13..%u bytes\n",
                path, MAX_INPUT);
        return NULL;
    }
    rewind(file);
    buffer = (Byte *)malloc((size_t)size);
    if (buffer == NULL) {
        fclose(file);
        fprintf(stderr, "decode: out of memory\n");
        return NULL;
    }
    count = fread(buffer, 1, (size_t)size, file);
    fclose(file);
    if (count != (size_t)size) {
        free(buffer);
        fprintf(stderr, "decode: short read from %s\n", path);
        return NULL;
    }
    *length = (size_t)size;
    return buffer;
}

int main(int argc, char **argv)
{
    Byte *stream = NULL;
    Byte *output = NULL;
    size_t stream_length = 0;
    size_t chunk;
    uint64_t expected = 0;
    size_t position;
    size_t total = 0;
    char *end = NULL;
    FILE *file;
    ISzAlloc allocator = { allocate, release };
    CLzmaDec decoder;
    SRes result;
    ELzmaStatus status = LZMA_STATUS_NOT_SPECIFIED;
    int index;

    if (argc != 4) {
        fprintf(stderr, "usage: %s INPUT OUTPUT CHUNK\n", argv[0]);
        return 2;
    }
    chunk = (size_t)strtoul(argv[3], &end, 0);
    if (end == argv[3] || *end != '\0' || chunk == 0 || chunk > OUTPUT_CHUNK) {
        fprintf(stderr, "decode: CHUNK must be 1..%u bytes\n", OUTPUT_CHUNK);
        return 2;
    }

    stream = read_file(argv[1], &stream_length);
    if (stream == NULL) {
        return 3;
    }
    for (index = 0; index < 8; index++) {
        expected |= (uint64_t)stream[5 + index] << (8 * index);
    }
    if (expected == 0 || expected > MAX_OUTPUT) {
        free(stream);
        fprintf(stderr, "decode: declared output size is invalid\n");
        return 4;
    }

    output = (Byte *)malloc((size_t)expected);
    if (output == NULL) {
        free(stream);
        fprintf(stderr, "decode: out of memory\n");
        return 5;
    }

    LzmaDec_Construct(&decoder);
    result = LzmaDec_Allocate(&decoder, stream, 5, &allocator);
    if (result != SZ_OK) {
        free(stream);
        free(output);
        fprintf(stderr, "decode: LzmaDec_Allocate failed with %d\n",
                (int)result);
        return 6;
    }
    LzmaDec_Init(&decoder);

    position = 13;
    while (total < expected) {
        SizeT consumed = stream_length - position;
        SizeT produced = OUTPUT_CHUNK;
        ELzmaFinishMode finish;

        if (consumed > chunk) {
            consumed = chunk;
        }
        if (produced > expected - total) {
            produced = (SizeT)(expected - total);
        }
        finish = produced == expected - total
                     ? LZMA_FINISH_END
                     : LZMA_FINISH_ANY;
        result = LzmaDec_DecodeToBuf(&decoder, output + total, &produced,
                                     stream + position, &consumed,
                                     finish, &status);
        if (result != SZ_OK || (produced == 0 && consumed == 0)) {
            LzmaDec_Free(&decoder, &allocator);
            free(stream);
            free(output);
            fprintf(stderr, "decode: failed with %d, status %d\n",
                    (int)result, (int)status);
            return 7;
        }
        position += consumed;
        total += produced;
    }
    LzmaDec_Free(&decoder, &allocator);

    file = fopen(argv[2], "wb");
    if (file == NULL) {
        free(stream);
        free(output);
        fprintf(stderr, "decode: cannot create %s\n", argv[2]);
        return 8;
    }
    if (fwrite(output, 1, total, file) != total) {
        fclose(file);
        free(stream);
        free(output);
        fprintf(stderr, "decode: short write to %s\n", argv[2]);
        return 9;
    }
    if (fclose(file) != 0) {
        free(stream);
        free(output);
        fprintf(stderr, "decode: cannot close %s\n", argv[2]);
        return 10;
    }

    printf("{\"result\":%d,\"status\":%d,\"consumed\":%zu,"
           "\"input_size\":%zu,\"output_size\":%zu,\"input_chunk\":%zu,"
           "\"decoder_peak_alloc\":%zu,\"unfreed\":%zu}\n",
           (int)result, (int)status, position, stream_length, total,
           chunk, in_peak, in_use);

    free(stream);
    free(output);
    return (result != SZ_OK || total != expected || position != stream_length
            || in_use != 0) ? 11 : 0;
}
