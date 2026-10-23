/*
 * Encode a file with the CP4 OEM's LZMA settings.
 *
 * This is a small file-to-file harness over the public-domain LZMA SDK 19.00
 * C encoder. It writes a 13-byte .lzma-alone header containing the known
 * uncompressed size and no end marker, matching the framing in the CP4
 * Hi3861L OTA. The settings (dictionary 65536, lc=2, lp=2, pb=2, normal
 * algorithm, 128 fast bytes, one thread) reproduce the untouched OEM stream
 * byte-for-byte.
 *
 * Usage: encode INPUT OUTPUT
 *
 * This program performs no device I/O.
 */

#include <stdio.h>
#include <stdlib.h>

#include "LzmaEnc.h"

#define MAX_INPUT (16u * 1024u * 1024u)

static void *allocate(ISzAllocPtr p, size_t size)
{
    (void)p;
    return malloc(size);
}

static void release(ISzAllocPtr p, void *address)
{
    (void)p;
    free(address);
}

static int read_file(const char *path, Byte **data, size_t *length)
{
    FILE *file = fopen(path, "rb");
    long size;
    Byte *buffer;

    if (file == NULL) {
        fprintf(stderr, "encode: cannot open %s\n", path);
        return 0;
    }
    if (fseek(file, 0, SEEK_END) != 0) {
        fclose(file);
        fprintf(stderr, "encode: cannot seek %s\n", path);
        return 0;
    }
    size = ftell(file);
    if (size < 0 || (unsigned long)size > MAX_INPUT) {
        fclose(file);
        fprintf(stderr, "encode: %s is empty or larger than %u bytes\n",
                path, MAX_INPUT);
        return 0;
    }
    rewind(file);
    buffer = (Byte *)malloc((size_t)size);
    if (buffer == NULL) {
        fclose(file);
        fprintf(stderr, "encode: out of memory\n");
        return 0;
    }
    if (fread(buffer, 1, (size_t)size, file) != (size_t)size) {
        free(buffer);
        fclose(file);
        fprintf(stderr, "encode: short read from %s\n", path);
        return 0;
    }
    fclose(file);
    *data = buffer;
    *length = (size_t)size;
    return 1;
}

int main(int argc, char **argv)
{
    Byte *input = NULL;
    Byte *output = NULL;
    Byte header[13];
    size_t input_length = 0;
    size_t output_capacity;
    SizeT output_length;
    SizeT property_size = 5;
    CLzmaEncProps props;
    ISzAlloc allocator = { allocate, release };
    SRes result;
    FILE *file;

    if (argc != 3) {
        fprintf(stderr, "usage: %s INPUT OUTPUT\n", argv[0]);
        return 2;
    }
    if (!read_file(argv[1], &input, &input_length)) {
        return 3;
    }

    output_capacity = input_length * 2 + 4096;
    output = (Byte *)malloc(output_capacity);
    if (output == NULL) {
        free(input);
        fprintf(stderr, "encode: out of memory\n");
        return 4;
    }

    LzmaEncProps_Init(&props);
    props.dictSize = 65536;
    props.lc = 2;
    props.lp = 2;
    props.pb = 2;
    props.fb = 128;
    props.numThreads = 1;
    props.writeEndMark = 0;

    output_length = output_capacity;
    result = LzmaEncode(output, &output_length, input, input_length, &props,
                        header, &property_size, 0, NULL, &allocator,
                        &allocator);
    if (result != SZ_OK) {
        fprintf(stderr, "encode: LzmaEncode failed with %d\n", (int)result);
        free(input);
        free(output);
        return 5;
    }

    /* LzmaEncode writes five property bytes. .lzma-alone adds an eight-byte
       little-endian uncompressed size; the known size replaces the end
       marker used by streaming formats. */
    {
        UInt64 size = (UInt64)input_length;
        int i;
        for (i = 0; i < 8; i++) {
            header[5 + i] = (Byte)(size >> (8 * i));
        }
    }

    file = fopen(argv[2], "wb");
    if (file == NULL) {
        fprintf(stderr, "encode: cannot create %s\n", argv[2]);
        free(input);
        free(output);
        return 6;
    }
    if (fwrite(header, 1, sizeof(header), file) != sizeof(header)
        || fwrite(output, 1, output_length, file) != output_length) {
        fclose(file);
        free(input);
        free(output);
        fprintf(stderr, "encode: short write to %s\n", argv[2]);
        return 7;
    }
    if (fclose(file) != 0) {
        free(input);
        free(output);
        fprintf(stderr, "encode: cannot close %s\n", argv[2]);
        return 8;
    }

    free(input);
    free(output);
    return 0;
}
