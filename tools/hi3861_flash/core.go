package main

import (
	"bytes"
	"crypto/sha256"
	"encoding/binary"
	"encoding/hex"
	"fmt"
	"io"
	"os"
	"syscall"
)

const candidateSHA = "951c3e569cd794821637263e805105c65b2e576cdd006f9028c4e3aea49679fb"
const originalSHA = "e01c4867892f611326c4935abdcfb1d9a6c19629f2953b0cd441fb36578596c2"
const appSHA = "b5ed2f4cfdbaa0f6fe3758d6f7110cb776aff4055e0acbea1c2ca5b34508b063"
const librarySHA = "9f9412314410c8d259f46b2f1b8bbb57f82f20e4182bdf403edd06544a9c95c4"

func hash(b []byte) string { s := sha256.Sum256(b); return hex.EncodeToString(s[:]) }
func checkFile(path, want string) error {
	f, e := openBlocking(path, syscall.O_RDONLY, 0)
	if e != nil {
		return e
	}
	defer f.Close()
	h := sha256.New()
	if _, e = io.CopyBuffer(h, f, make([]byte, 32768)); e != nil {
		return e
	}
	if hex.EncodeToString(h.Sum(nil)) != want {
		return fmt.Errorf("hash mismatch: %s", path)
	}
	return nil
}
func word(b []byte, off int) uint32   { return binary.LittleEndian.Uint32(b[off : off+4]) }
func put(b []byte, off int, v uint32) { binary.LittleEndian.PutUint32(b[off:off+4], v) }
func validateOTA(b []byte) error {
	if len(b) != 552608 || word(b, 0) != 0x3c78961e || word(b, 0x280) != 0x3c78961e {
		return fmt.Errorf("unexpected OTA size/magic")
	}
	if word(b, 8) != 0x280 || int(word(b, 12))+0x280 != len(b) || int(word(b, 0x34)) != len(b) {
		return fmt.Errorf("invalid OTA lengths")
	}
	if b[0x30] != 0xf0 || b[0x32] != 0x42 || word(b, 0x3c) != 0x3f0000 || !bytes.Equal(b[0x18:0x1c], []byte{2, 0, 139, 0}) {
		return fmt.Errorf("not the expected plaintext, same-version kernel OTA")
	}
	if b[0x28c] != 1 || b[0x2a8] != 1 || word(b, 0x2ac) != 0x3c0 || word(b, 0x2b0) != 551636 {
		return fmt.Errorf("unexpected compressed section")
	}
	if !bytes.Equal(b[0x3c0:0x3cd], []byte{0x6e, 0, 0, 1, 0, 0xb0, 0xe9, 0x0d, 0, 0, 0, 0, 0}) {
		return fmt.Errorf("unexpected LZMA properties/size")
	}
	for _, v := range []struct{ data, slot []byte }{{b[:0x180], b[0x180:0x280]}, {append(append([]byte{}, b[0x280:0x2c0]...), b[0x3c0:]...), b[0x2c0:0x3c0]}} {
		d := sha256.Sum256(v.data)
		if !bytes.Equal(d[:], v.slot[:32]) || !bytes.Equal(v.slot[32:], make([]byte, 224)) {
			return fmt.Errorf("wrapper SHA256 mismatch")
		}
	}
	if hash(b) != candidateSHA {
		return fmt.Errorf("candidate SHA256 mismatch")
	}
	return nil
}

// ARM worker: a fresh pthread receives context. No injected code runs the update
// on the temporarily borrowed application thread's stack. Context fields:
// target,arg0,arg1,status,result at offsets 0,4,8,12,16.
var worker = []uint32{
	0xe92d4010, // push {r4,lr}
	0xe1a04000, // mov r4,r0
	0xe3a01001, // mov r1,#1
	0xe584100c, // str r1,[r4,#12] (entered)
	0xe594c000, // ldr ip,[r4]
	0xe5940004, // ldr r0,[r4,#4]
	0xe5941008, // ldr r1,[r4,#8]
	0xe12fff3c, // blx ip
	0xe5840010, // str r0,[r4,#16]
	0xe3a01002, // mov r1,#2
	0xe584100c, // str r1,[r4,#12] (returned)
	0xe3a00000, // mov r0,#0
	0xe8bd8010, // pop {r4,pc}
}

func workerBytes() []byte {
	b := make([]byte, len(worker)*4)
	for i, v := range worker {
		put(b, i*4, v)
	}
	return b
}

// Use blocking descriptors explicitly: this camera's kernel has no epoll.
func openBlocking(path string, flags int, mode uint32) (*os.File, error) {
	fd, e := syscall.Open(path, flags, mode)
	if e != nil {
		return nil, e
	}
	return os.NewFile(uintptr(fd), path), nil
}
func readWhole(path string) ([]byte, error) {
	f, e := openBlocking(path, syscall.O_RDONLY, 0)
	if e != nil {
		return nil, e
	}
	defer f.Close()
	return io.ReadAll(f)
}
