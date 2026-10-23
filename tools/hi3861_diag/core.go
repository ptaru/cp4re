package main

// Pure helpers and data shared by the Linux/ARM readback binary and host tests.
// The Linux-only ptrace/worker code is in main_linux_arm.go.

import (
	"crypto/sha256"
	"encoding/binary"
	"encoding/hex"
	"errors"
	"fmt"
	"strings"
)

const (
	sleepWord     = 0x670bd4
	mapSize       = 0x10000
	invalidReturn = 0xdeadbeec
	createPLT     = 0x170c4
	mmapPLT       = 0x16ef0
	joinPLT       = 0x17b20
	unmapPLT      = 0x17454
	pidPLT        = 0x16e78
	versionPLT    = 0x17100

	appSHA     = "b5ed2f4cfdbaa0f6fe3758d6f7110cb776aff4055e0acbea1c2ca5b34508b063"
	librarySHA = "9f9412314410c8d259f46b2f1b8bbb57f82f20e4182bdf403edd06544a9c95c4"

	libWakeupReason     = 0x9fc4
	libWifiWakeupInfo   = 0xc578
	libFwLog            = 0xbc50
	libSetLogcfg        = 0xc338
	libAwakePacket      = 0xb794
	wordWakeupReason    = 0xe92d4010 // push {r4, lr}
	wordWifiWakeupInfo  = 0xe3500000 // cmp r0, #0
	wordFwLog           = 0xeaffe43c // b <PLT ezwifi_lpw_get_fw_log_hisi3861>
	wordSetLogcfg       = 0xe52de004 // str lr, [sp, #-4]!
	wordAwakePacket     = 0xe301281f // movw r2, #0x181f
	infoSize            = 0xd0
	fwLogSize           = 0x1000
	scratchReasonOffset = 0x2020
	scratchInfoOffset   = 0x2100
	scratchAwakeOffset  = 0x3000
	awakeSize           = 0x1820
	awakeBufferSize     = 0x4000
)

type mapping struct {
	lo, hi, offset uint32
	perms          string
	path           string
}

type mappings []mapping

func parseMapsText(text string) (mappings, error) {
	var out mappings
	for _, line := range strings.Split(text, "\n") {
		fields := strings.Fields(line)
		if len(fields) < 5 {
			continue
		}
		var lo, hi, off uint32
		if _, e := fmt.Sscanf(fields[0], "%x-%x", &lo, &hi); e != nil {
			continue
		}
		fmt.Sscanf(fields[2], "%x", &off)
		path := ""
		if len(fields) >= 6 {
			path = strings.Join(fields[5:], " ")
		}
		out = append(out, mapping{lo: lo, hi: hi, offset: off, perms: fields[1], path: path})
	}
	return out, nil
}

func (ms mappings) at(addr, n uint32) (*mapping, bool) {
	for i := range ms {
		if ms[i].lo <= addr && addr+n <= ms[i].hi {
			return &ms[i], true
		}
	}
	return nil, false
}
func (ms mappings) libraryBase() (uint32, error) {
	for _, m := range ms {
		if strings.HasSuffix(m.path, "/liblpwwifi.so") && m.offset == 0 {
			if m.lo == 0 || m.lo >= 0xfffff000 {
				return 0, fmt.Errorf("implausible library base %#x", m.lo)
			}
			return m.lo, nil
		}
	}
	return 0, errors.New("liblpwwifi.so base mapping not found")
}
func (ms mappings) requireExec(addr, n uint32, label string) error {
	m, ok := ms.at(addr, n)
	if !ok {
		return fmt.Errorf("%s address %#x is not mapped", label, addr)
	}
	if !strings.Contains(m.perms, "x") {
		return fmt.Errorf("%s address %#x is not executable (%s)", label, addr, m.perms)
	}
	return nil
}
func (ms mappings) requireReadable(addr, n uint32, label string) error {
	m, ok := ms.at(addr, n)
	if !ok {
		return fmt.Errorf("%s address %#x is not mapped", label, addr)
	}
	if !strings.Contains(m.perms, "r") {
		return fmt.Errorf("%s address %#x is not readable (%s)", label, addr, m.perms)
	}
	return nil
}

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

func word(b []byte, off int) uint32   { return binary.LittleEndian.Uint32(b[off : off+4]) }
func put(b []byte, off int, v uint32) { binary.LittleEndian.PutUint32(b[off:off+4], v) }
func hash(b []byte) string            { s := sha256.Sum256(b); return hex.EncodeToString(s[:]) }

func printableStrings(data []byte) string {
	var out strings.Builder
	var run []byte
	flush := func() {
		if len(run) >= 4 {
			out.Write(run)
			out.WriteByte('\n')
		}
		run = nil
	}
	for _, c := range data {
		if c == '\n' || c == '\r' {
			flush()
			continue
		}
		if c == '\t' || (c >= 32 && c < 127) {
			run = append(run, c)
		} else {
			flush()
		}
	}
	flush()
	return out.String()
}
func hexDump(data []byte) string {
	var b strings.Builder
	for i := 0; i < len(data); i += 16 {
		end := i + 16
		if end > len(data) {
			end = len(data)
		}
		fmt.Fprintf(&b, "%08x  ", i)
		for j := i; j < end; j++ {
			fmt.Fprintf(&b, "%02x ", data[j])
		}
		for j := end; j < i+16; j++ {
			b.WriteString("   ")
		}
		b.WriteString(" |")
		for j := i; j < end; j++ {
			c := data[j]
			if c >= 32 && c < 127 {
				b.WriteByte(c)
			} else {
				b.WriteByte('.')
			}
		}
		b.WriteString("|\n")
	}
	return b.String()
}
func workerRetained(e error) bool {
	return e != nil && strings.Contains(e.Error(), "worker retained")
}

type readResult struct {
	Name         string `json:"name"`
	AddressHex   string `json:"address_hex"`
	Return       uint32 `json:"return"`
	ReturnHex    string `json:"return_hex"`
	ReturnSigned int32  `json:"return_signed"`
	OK           bool   `json:"ok"`
	File         string `json:"file,omitempty"`
	Bytes        int    `json:"bytes,omitempty"`
	SHA256       string `json:"sha256,omitempty"`
	Value        uint32 `json:"value,omitempty"`
	ValueHex     string `json:"value_hex,omitempty"`
	PointerHex   string `json:"pointer_hex,omitempty"`
	Error        string `json:"error,omitempty"`
}
type logcfgResult struct {
	Requested  bool   `json:"requested"`
	OnReturn   uint32 `json:"on_return"`
	OnHex      string `json:"on_return_hex"`
	OffReturn  uint32 `json:"off_return"`
	OffHex     string `json:"off_return_hex"`
	SkippedOff string `json:"skipped_off,omitempty"`
}
type manifest struct {
	LinuxReplyPatch string           `json:"linux_reply_patch,omitempty"`
	Status          map[string]int64 `json:"status,omitempty"`
	AdapterProbe    string           `json:"adapter_probe,omitempty"`
	Version         int              `json:"version"`
	Tool            string           `json:"tool"`
	Mode            string           `json:"mode"`
	Label           string           `json:"label"`
	OutDir          string           `json:"out_dir"`
	LogPath         string           `json:"log_path"`
	EzappPID        int              `json:"ezapp_pid"`
	AppSHA256       string           `json:"app_sha256"`
	LibrarySHA256   string           `json:"library_sha256"`
	LibraryBaseHex  string           `json:"library_base_hex"`
	MCUVersion      string           `json:"mcu_version"`
	HoldMode        string           `json:"hold_mode"`
	RequestedFields []string         `json:"requested_fields"`
	WakeupReason    readResult       `json:"wakeup_reason"`
	WakeupInfo      readResult       `json:"wakeup_info"`
	FwLog           readResult       `json:"fw_log"`
	AwakePacket     readResult       `json:"awake_packet"`
	Logcfg          *logcfgResult    `json:"logcfg,omitempty"`
	Result          string           `json:"result"`
	Errors          []string         `json:"errors"`
}
type stdoutResult struct {
	StatusB64       string   `json:"status_b64,omitempty"`
	Manifest        manifest `json:"manifest"`
	WakeupReasonHex string   `json:"wakeup_reason_hex,omitempty"`
	WakeupInfoB64   string   `json:"wakeup_info_b64,omitempty"`
	FwLogB64        string   `json:"fw_log_b64,omitempty"`
	AwakePacketB64  string   `json:"awake_packet_b64,omitempty"`
}

func statusAdapter(base, command, size uint32) []byte {
	words := []uint32{0xe92d4010, 0xe24dd008, 0xe1a01000, 0xe3a00000 | command,
		0xe3a02000, 0xe3a03000 | size, 0xe3a04001, 0xe58d4000, 0xe59fc008,
		0xe12fff3c, 0xe28dd008, 0xe8bd8010, base + 0xd6d8}
	b := make([]byte, len(words)*4)
	for i, w := range words {
		put(b, i*4, w)
	}
	return b
}
func decodeStatus(b []byte) (map[string]int64, error) {
	if len(b) != 80 || word(b, 0) != 0x31574c43 || word(b, 4) != 1 {
		return nil, errors.New("receiver absent or incompatible status snapshot")
	}
	names := strings.Fields("magic version generation online socket_ready filter_ready received invalid suppressed assertions opens socket_errors filter_errors last_rx_ms last_assert_ms retry_at_ms last_socket_result last_filter_result have_asserted retry_pending")
	out := map[string]int64{}
	for i, n := range names {
		out[n] = int64(word(b, i*4))
	}
	for _, n := range []string{"last_socket_result", "last_filter_result"} {
		out[n] = int64(int32(out[n]))
	}
	return out, nil
}
