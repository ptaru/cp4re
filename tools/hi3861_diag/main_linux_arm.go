package main

// Linux/ARM-only readback implementation. The _linux_arm.go filename suffix
// keeps this file out of host-side go test builds.

import (
	"bytes"
	"crypto/sha256"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"runtime"
	"strconv"
	"strings"
	"syscall"
	"unsafe"
)

var logFile *os.File
var workerTicks = 3000 // 10 ms per tick

func logf(f string, a ...any) {
	s := fmt.Sprintf(f+"\n", a...)
	if logFile != nil {
		logFile.WriteString(s)
		logFile.Sync()
	}
	fmt.Print(s)
}
func pause() { ts := syscall.Timespec{Nsec: 10000000}; syscall.Nanosleep(&ts, nil) }

type target struct {
	pid      int
	mem      *os.File
	attached bool
}

func (t *target) read(a uint32, n int) ([]byte, error) {
	b := make([]byte, n)
	k, e := t.mem.ReadAt(b, int64(a))
	if e != nil {
		return nil, e
	}
	if k != n {
		return nil, fmt.Errorf("short memory read")
	}
	return b, nil
}
func (t *target) write(a uint32, b []byte) error {
	k, e := t.mem.WriteAt(b, int64(a))
	if e != nil {
		return e
	}
	if k != len(b) {
		return fmt.Errorf("short memory write")
	}
	return nil
}
func (t *target) setword(a, v uint32) error { b := make([]byte, 4); put(b, 0, v); return t.write(a, b) }

func (t *target) attach() error {
	if t.attached {
		return errors.New("already attached")
	}
	for attempt := 0; attempt < 500; attempt++ {
		stat, e := readWhole(fmt.Sprintf("/proc/%d/stat", t.pid))
		if e != nil {
			return e
		}
		close := strings.LastIndex(string(stat), ")")
		if close < 0 {
			return errors.New("invalid process stat")
		}
		fields := strings.Fields(string(stat[close+1:]))
		if len(fields) == 0 || fields[0] != "S" {
			pause()
			continue
		}
		if e = syscall.PtraceAttach(t.pid); e != nil {
			return e
		}
		t.attached = true
		var ws syscall.WaitStatus
		_, e = syscall.Wait4(t.pid, &ws, 0, nil)
		if e != nil {
			return e
		}
		if !ws.Stopped() {
			return fmt.Errorf("attach did not stop main")
		}
		var regs syscall.PtraceRegs
		if e = syscall.PtraceGetRegs(t.pid, &regs); e != nil {
			return e
		}
		restart := int32(regs.Uregs[0])
		nr := regs.Uregs[7]
		instruction, readErr := t.read(regs.Uregs[15]-4, 8)
		svc := readErr == nil && (word(instruction, 0) == 0xef000000 || word(instruction, 4) == 0xef000000)
		if (nr == 162 || nr == 265) && svc && regs.Uregs[16]&0x20 == 0 {
			logf("BORROW_IDLE_MAIN pc=%#x syscall=%d saved_r0=%#x", regs.Uregs[15], nr, uint32(restart))
			return nil
		}
		if attempt%100 == 0 {
			logf("WAIT_IDLE_MAIN pc=%#x r7=%d r0=%#x svc_bytes=%x", regs.Uregs[15], nr, regs.Uregs[0], instruction)
		}
		if e = t.detach(); e != nil {
			return e
		}
		pause()
	}
	return errors.New("could not observe main thread in an interrupted ARM nanosleep")
}
func (t *target) detach() error {
	if !t.attached {
		return nil
	}
	e := syscall.PtraceDetach(t.pid)
	if e == nil {
		t.attached = false
	}
	return e
}
func vfp(pid, request int, state *[264]byte) error {
	_, _, errno := syscall.Syscall6(syscall.SYS_PTRACE, uintptr(request), uintptr(pid), 0, uintptr(unsafe.Pointer(&state[0])), 0, 0)
	runtime.KeepAlive(state)
	if errno != 0 {
		return errno
	}
	return nil
}
func (t *target) call(addr uint32, args ...uint32) (uint32, error) {
	if !t.attached {
		return 0, errors.New("call without attach")
	}
	var original syscall.PtraceRegs
	if e := syscall.PtraceGetRegs(t.pid, &original); e != nil {
		return 0, e
	}
	if original.Uregs[16]&0x20 != 0 {
		return 0, errors.New("borrowed thread not in ARM mode")
	}
	var floating [264]byte
	if e := vfp(t.pid, 27, &floating); e != nil {
		return 0, fmt.Errorf("save VFP state: %w", e)
	}
	sp := original.Uregs[13]
	if sp < 0x5000 {
		return 0, errors.New("invalid stack")
	}
	base := (sp - 0x4000) &^ 7
	top := (sp - 0x100) &^ 7
	saved, e := t.read(base, int(sp-base))
	if e != nil {
		return 0, fmt.Errorf("save temporary stack: %w", e)
	}
	regs := original
	for i := 0; i < 4; i++ {
		regs.Uregs[i] = 0
	}
	for i, a := range args {
		if i < 4 {
			regs.Uregs[i] = a
		} else {
			if e = t.setword(top+uint32((i-4)*4), a); e != nil {
				return 0, e
			}
		}
	}
	regs.Uregs[13] = top
	regs.Uregs[14] = invalidReturn
	regs.Uregs[15] = addr
	regs.Uregs[17] = ^uint32(0)
	if e = syscall.PtraceSetRegs(t.pid, &regs); e != nil {
		t.write(base, saved)
		return 0, e
	}
	if e = syscall.PtraceCont(t.pid, 0); e != nil {
		return 0, e
	}
	var ws syscall.WaitStatus
	for i := 0; ; i++ {
		n, e := syscall.Wait4(t.pid, &ws, syscall.WNOHANG, nil)
		if e != nil {
			return 0, e
		}
		if n == t.pid {
			break
		}
		if i > 0 && i%500 == 0 {
			logf("CALL_WAIT address=%#x seconds=%d", addr, i/100)
		}
		pause()
	}
	if !ws.Stopped() {
		return 0, fmt.Errorf("borrowed thread exited %#x", ws)
	}
	var returned syscall.PtraceRegs
	if e = syscall.PtraceGetRegs(t.pid, &returned); e != nil {
		return 0, e
	}
	correct := ws.StopSignal() == syscall.SIGSEGV && returned.Uregs[15] == invalidReturn
	e1 := t.write(base, saved)
	efp := vfp(t.pid, 28, &floating)
	e2 := syscall.PtraceSetRegs(t.pid, &original)
	if e1 != nil {
		return 0, e1
	}
	if e2 != nil {
		return 0, e2
	}
	if efp != nil {
		return 0, fmt.Errorf("restore VFP state: %w", efp)
	}
	var restored syscall.PtraceRegs
	if e = syscall.PtraceGetRegs(t.pid, &restored); e != nil {
		return 0, e
	}
	if restored != original {
		return 0, errors.New("GPR restore readback mismatch")
	}
	var restoredFP [264]byte
	if e = vfp(t.pid, 27, &restoredFP); e != nil {
		return 0, e
	}
	if restoredFP != floating {
		return 0, errors.New("VFP restore readback mismatch")
	}
	restoredStack, e := t.read(base, len(saved))
	if e != nil {
		return 0, e
	}
	if !bytes.Equal(restoredStack, saved) {
		return 0, errors.New("temporary stack restore readback mismatch")
	}
	if !correct {
		return 0, fmt.Errorf("unexpected stop sig=%v pc=%#x", ws.StopSignal(), returned.Uregs[15])
	}
	return returned.Uregs[0], nil
}

func parseMaps(pid int) (mappings, error) {
	b, e := readWhole(fmt.Sprintf("/proc/%d/maps", pid))
	if e != nil {
		return nil, e
	}
	return parseMapsText(string(b))
}

func verifyPrologue(t *target, ms mappings, addr, expected uint32, label string) error {
	if e := ms.requireExec(addr, 4, label); e != nil {
		return e
	}
	b, e := t.read(addr, 4)
	if e != nil {
		return fmt.Errorf("read %s prologue: %w", label, e)
	}
	if got := word(b, 0); got != expected {
		return fmt.Errorf("%s prologue %#x != expected %#x", label, got, expected)
	}
	return nil
}

func findPID() (int, error) {
	directory, e := openBlocking("/proc", syscall.O_RDONLY, 0)
	if e != nil {
		return 0, e
	}
	defer directory.Close()
	ds, e := directory.Readdirnames(-1)
	if e != nil {
		return 0, e
	}
	found := 0
	for _, d := range ds {
		p, e := strconv.Atoi(d)
		if e != nil {
			continue
		}
		b, e := readWhole("/proc/" + d + "/cmdline")
		if e == nil && strings.Split(string(b), "\x00")[0] == "/dav/ezapp" {
			if found != 0 {
				return 0, errors.New("multiple ezapp processes")
			}
			found = p
		}
	}
	if found == 0 {
		return 0, errors.New("no ezapp process")
	}
	return found, nil
}

func flashRunning() (bool, error) {
	directory, e := openBlocking("/proc", syscall.O_RDONLY, 0)
	if e != nil {
		return false, e
	}
	defer directory.Close()
	ds, e := directory.Readdirnames(-1)
	if e != nil {
		return false, e
	}
	for _, d := range ds {
		if _, e := strconv.Atoi(d); e != nil {
			continue
		}
		b, e := readWhole("/proc/" + d + "/cmdline")
		if e != nil {
			continue
		}
		first := strings.Split(string(b), "\x00")[0]
		if strings.HasSuffix(first, "/launcher") && strings.Contains(first, "hi3861") {
			return true, nil
		}
	}
	return false, nil
}

func readbackRunning(exclude int) (bool, error) {
	directory, e := openBlocking("/proc", syscall.O_RDONLY, 0)
	if e != nil {
		return false, e
	}
	defer directory.Close()
	ds, e := directory.Readdirnames(-1)
	if e != nil {
		return false, e
	}
	for _, d := range ds {
		p, e := strconv.Atoi(d)
		if e != nil || p == exclude {
			continue
		}
		b, e := readWhole("/proc/" + d + "/cmdline")
		if e != nil {
			continue
		}
		first := strings.Split(string(b), "\x00")[0]
		if strings.HasSuffix(first, "cp4-independent-status") || strings.HasSuffix(first, "cp4-hi3861-readback") {
			return true, nil
		}
	}
	return false, nil
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

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
func writeAll(f *os.File, b []byte) error {
	for len(b) > 0 {
		n, e := f.Write(b)
		if e != nil {
			return e
		}
		if n == 0 {
			return io.ErrShortWrite
		}
		b = b[n:]
	}
	return nil
}
func writeFile(path string, data []byte) error {
	f, e := openBlocking(path, os.O_WRONLY|os.O_CREATE|os.O_EXCL|syscall.O_SYNC, 0600)
	if e != nil {
		return e
	}
	if e = writeAll(f, data); e != nil {
		f.Close()
		return e
	}
	if e = f.Sync(); e != nil {
		f.Close()
		return e
	}
	return f.Close()
}
func writeJSON(path string, v any) error {
	b, e := json.MarshalIndent(v, "", "  ")
	if e != nil {
		return e
	}
	return writeFile(path, append(b, '\n'))
}

func run() (err error) {
	mode := flag.String("mode", "check", "check or readback")
	logPath := flag.String("log", "/mnt/mmc01/hi3861-flash/readback.log", "durable log")
	outDir := flag.String("out", "", "new output directory under /mnt/mmc01 (readback only)")
	label := flag.String("label", "", "phase label, e.g. baseline or after99")
	enableLog := flag.Bool("enable-log", false, "set MCU logcfg to 1 before fw_log, then 0")
	adopt := flag.Bool("adopt-hold", false, "adopt an existing bounded research hold")
	external := flag.Bool("external-hold", false, "require an external hold manager; never write the sleep flag")
	fields := flag.String("fields", "status", "comma-separated: reason,info,fwlog,awake")
	workerSeconds := flag.Int("worker-seconds", 30, "per-call worker timeout")
	flag.Parse()

	if *mode != "check" && *mode != "readback" {
		return errors.New("mode must be check or readback")
	}
	if *adopt && *mode != "readback" {
		return errors.New("adopt-hold is only valid in readback mode")
	}
	if *external && *mode != "readback" {
		return errors.New("external-hold is only valid in readback mode")
	}
	if *adopt && *external {
		return errors.New("use either adopt-hold or external-hold, not both")
	}
	if *enableLog && *mode != "readback" {
		return errors.New("enable-log is only valid in readback mode")
	}
	if *mode == "readback" {
		if *outDir == "" {
			return errors.New("readback requires -out")
		}
		if !strings.HasPrefix(*outDir, "/mnt/mmc01/") {
			return errors.New("out must be under /mnt/mmc01")
		}
	}
	if !strings.HasPrefix(*logPath, "/mnt/mmc01/") {
		return errors.New("log must be under /mnt/mmc01")
	}
	if *workerSeconds < 5 || *workerSeconds > 120 {
		return errors.New("worker-seconds must be 5..120")
	}
	workerTicks = *workerSeconds * 100
	want := map[string]bool{}
	var requested []string
	for _, raw := range strings.Split(*fields, ",") {
		name := strings.TrimSpace(raw)
		switch name {
		case "reason", "info", "fwlog", "awake", "status", "adapter-probe":
			if !want[name] {
				want[name] = true
				requested = append(requested, name)
			}
		case "":
		default:
			return fmt.Errorf("unknown field %q; use reason,info,fwlog,awake", name)
		}
	}
	if want["status"] && want["adapter-probe"] {
		return errors.New("select status or adapter-probe, not both")
	}
	if len(requested) == 0 {
		return errors.New("no fields selected")
	}
	if *mode == "check" && *fields != "status" {
		return errors.New("fields are only used in readback mode")
	}

	if running, e := flashRunning(); e != nil {
		return fmt.Errorf("check for a running flash launcher: %w", e)
	} else if running {
		return errors.New("a flash launcher is running; refusing readback")
	}

	lock := filepath.Join(filepath.Dir(*logPath), "readback.lock")
	if _, e := os.Stat(lock); e == nil {
		running, re := readbackRunning(os.Getpid())
		if re == nil && !running {
			logf("STALE_LOCK removing %s", lock)
			if re = os.Remove(lock); re != nil {
				return fmt.Errorf("remove stale readback lock: %w", re)
			}
		}
	}
	if e := os.Mkdir(lock, 0700); e != nil {
		return fmt.Errorf("readback lock: %w", e)
	}
	defer os.Remove(lock)

	logFile, err = openBlocking(*logPath, os.O_WRONLY|os.O_CREATE|os.O_APPEND|syscall.O_SYNC, 0600)
	if err != nil {
		return err
	}
	defer logFile.Close()
	defer func() {
		if err != nil {
			logf("ERROR %v", err)
		}
	}()
	logf("START tool=cp4-independent-status mode=%s label=%q pid=%d", *mode, *label, os.Getpid())

	for path, want := range map[string]string{"/dav/ezapp": appSHA, "/dav/liblpwwifi.so": librarySHA} {
		if e := checkFile(path, want); e != nil {
			return e
		}
	}
	pid, e := findPID()
	if e != nil {
		return e
	}
	ms, e := parseMaps(pid)
	if e != nil {
		return e
	}
	base, e := ms.libraryBase()
	if e != nil {
		return e
	}
	if e = ms.requireExec(pidPLT, 4, "pidPLT"); e != nil {
		return e
	}
	if e = ms.requireExec(versionPLT, 4, "versionPLT"); e != nil {
		return e
	}
	if e = ms.requireExec(createPLT, 4, "createPLT"); e != nil {
		return e
	}
	if e = ms.requireExec(mmapPLT, 4, "mmapPLT"); e != nil {
		return e
	}
	if e = ms.requireExec(joinPLT, 4, "joinPLT"); e != nil {
		return e
	}
	if e = ms.requireExec(unmapPLT, 4, "unmapPLT"); e != nil {
		return e
	}
	logf("PREFLIGHT_OK ezapp_pid=%d library_base=%#x", pid, base)

	memFlags := os.O_RDONLY
	if *mode == "readback" {
		memFlags = os.O_RDWR
	}
	mem, e := openBlocking(fmt.Sprintf("/proc/%d/mem", pid), memFlags, 0)
	if e != nil {
		return e
	}
	defer mem.Close()

	t := target{pid: pid, mem: mem}
	if e = verifyPrologue(&t, ms, base+0xd6d8, 0xe92d4ff0, "generic ioctl"); e != nil {
		return e
	}

	if e = verifyPrologue(&t, ms, base+libWakeupReason, wordWakeupReason, "lpw_get_wakeup_reason"); e != nil {
		return e
	}
	if e = verifyPrologue(&t, ms, base+libWifiWakeupInfo, wordWifiWakeupInfo, "lpw_get_wifi_wakeup_info"); e != nil {
		return e
	}
	if e = verifyPrologue(&t, ms, base+libFwLog, wordFwLog, "lpw_get_fw_log"); e != nil {
		return e
	}
	if e = verifyPrologue(&t, ms, base+libSetLogcfg, wordSetLogcfg, "lpw_set_logcfg"); e != nil {
		return e
	}
	if e = verifyPrologue(&t, ms, base+libAwakePacket, wordAwakePacket, "lpw_get_awake_packet"); e != nil {
		return e
	}
	logf("PROLOGUES_OK reason=%#x info=%#x fwlog=%#x logcfg=%#x",
		base+libWakeupReason, base+libWifiWakeupInfo, base+libFwLog, base+libSetLogcfg)

	if *mode == "check" {
		logf("CHECK_OK no memory writes, Sleep-hold writes, HCC calls or updater calls")
		return nil
	}

	if e = os.Mkdir(*outDir, 0700); e != nil {
		return fmt.Errorf("create output directory: %w", e)
	}
	hold, e := t.read(sleepWord, 4)
	if e != nil {
		return e
	}
	holdMode := ""
	if *external {
		if word(hold, 0) != 0 {
			return errors.New("external hold not active: sleep flag is 1; acquire a hold first")
		}
		holdMode = "external"
		logf("EXTERNAL_HOLD active (flag 0); not writing or restoring the sleep flag")
	} else {
		if word(hold, 0) != 1 {
			if !*adopt || word(hold, 0) != 0 {
				return errors.New("sleep flag not 1; another hold manager may be active")
			}
			put(hold, 0, 1)
			holdMode = "adopted"
		} else {
			holdMode = "one-shot"
		}
		if e = t.setword(sleepWord, 0); e != nil {
			return e
		}
		logf("SLEEP_HOLD acquired mode=%s; one-shot holds can be overwritten by ezapp; prefer -external-hold with tools/wake_hold.py", holdMode)
		defer func() {
			if e := t.write(sleepWord, hold); e != nil {
				logf("SLEEP_RESTORE_ERROR %v", e)
			} else {
				logf("SLEEP_HOLD restored")
			}
		}()
	}

	if e = t.attach(); e != nil {
		return e
	}
	defer t.detach()
	address, e := t.call(mmapPLT, 0, mapSize, 7, 0x22, ^uint32(0), 0)
	if e != nil {
		return e
	}
	if address >= 0xfffff000 || address == 0 || address&4095 != 0 {
		return fmt.Errorf("mmap failed %#x", address)
	}
	logf("SCRATCH_MAPPED address=%#x size=%d", address, mapSize)
	code := workerBytes()
	if n, e := syscall.PtracePokeText(pid, uintptr(address), code); e != nil || n != len(code) {
		return fmt.Errorf("write worker code %d %v", n, e)
	}

	adapter := statusAdapter(base, 0x12, 80)
	if want["adapter-probe"] {
		adapter = statusAdapter(base, 0xf, 4)
	}
	if n, e := syscall.PtracePokeText(pid, uintptr(address+0x100), adapter); e != nil || n != len(adapter) {
		return fmt.Errorf("write adapter: %d %v", n, e)
	}
	ctx := address + 0x1000
	thread := address + 0x2000
	version := address + 0x2010
	reasonAddr := address + scratchReasonOffset
	infoAddr := address + scratchInfoOffset
	awakeAddr := address + scratchAwakeOffset
	if e = t.detach(); e != nil {
		return e
	}

	retained := false
	invoke := func(name string, fn, a0, a1 uint32) (uint32, error) {
		context := make([]byte, 20)
		put(context, 0, fn)
		put(context, 4, a0)
		put(context, 8, a1)
		if e := t.write(ctx, context); e != nil {
			return 0, e
		}
		if e := t.attach(); e != nil {
			return 0, e
		}
		logf("WORKER_CREATE name=%s function=%#x arg0=%#x arg1=%#x", name, fn, a0, a1)
		ret, e := t.call(createPLT, thread, 0, address, ctx)
		if e != nil {
			return 0, e
		}
		if ret != 0 {
			return 0, fmt.Errorf("pthread_create returned %d", ret)
		}
		tidBytes, e := t.read(thread, 4)
		if e != nil {
			return 0, e
		}
		handle := word(tidBytes, 0)
		if e = t.detach(); e != nil {
			return 0, e
		}
		for i := 0; ; i++ {
			state, e := t.read(ctx, 20)
			if e != nil {
				return 0, fmt.Errorf("worker memory unavailable (host may have rebooted): %w", e)
			}
			if word(state, 12) == 2 {
				result := word(state, 16)
				logf("WORKER_RETURN name=%s r0=%#x signed=%d", name, result, int32(result))
				if e = t.attach(); e != nil {
					return 0, e
				}
				r, e := t.call(joinPLT, handle, 0)
				if e != nil {
					return 0, e
				}
				if r != 0 {
					return 0, fmt.Errorf("pthread_join %d", r)
				}
				if e = t.detach(); e != nil {
					return 0, e
				}
				return result, nil
			}
			if i > 0 && i%500 == 0 {
				logf("WORKER_WAIT name=%s state=%d seconds=%d", name, word(state, 12), i/100)
			}
			if i == workerTicks {
				retained = true
				return 0, fmt.Errorf("%s did not finish; worker retained, not interrupted", name)
			}
			pause()
		}
	}

	m := manifest{
		Version:         1,
		Tool:            "cp4-independent-status",
		Mode:            *mode,
		Label:           *label,
		OutDir:          *outDir,
		LogPath:         *logPath,
		EzappPID:        pid,
		AppSHA256:       appSHA,
		LibrarySHA256:   librarySHA,
		LibraryBaseHex:  fmt.Sprintf("%#x", base),
		HoldMode:        holdMode,
		RequestedFields: requested,
		WakeupReason:    readResult{Name: "wakeup_reason", AddressHex: fmt.Sprintf("%#x", base+libWakeupReason)},
		WakeupInfo:      readResult{Name: "wifi_wakeup_info", AddressHex: fmt.Sprintf("%#x", base+libWifiWakeupInfo)},
		FwLog:           readResult{Name: "fw_log", AddressHex: fmt.Sprintf("%#x", base+libFwLog)},
		Errors:          []string{},
	}
	fail := func(what string, e error) {
		m.Errors = append(m.Errors, what+": "+e.Error())
		logf("READBACK_ERROR %s: %v", what, e)
	}

	r, e := invoke("getpid", pidPLT, 0, 0)
	if e != nil {
		fail("getpid", e)
	} else if r != uint32(pid) {
		fail("getpid", fmt.Errorf("returned %d, expected %d", r, pid))
	}

	if !retained {
		if e = t.setword(version, 0); e != nil {
			fail("MCU version", e)
		} else {
			r, e = invoke("MCU_version_read", versionPLT, version, 0)
			if e != nil {
				fail("MCU version", e)
			} else {
				vb, re := t.read(version, 4)
				if re != nil {
					fail("MCU version", re)
				} else if v := word(vb, 0); r != 0 || v != 0x2008b {
					fail("MCU version", fmt.Errorf("return=%#x value=%#x; expected 2.139", r, v))
				} else {
					m.MCUVersion = "2.139"
					logf("MCU_VERSION_OK version=2.139")
				}
			}
		}
	}

	diagReady := true
	var restoreReply func() error
	if want["status"] {
		if retained || m.MCUVersion != "2.139" || len(m.Errors) != 0 {
			diagReady = false
			fail("diagnostic baseline", errors.New("healthy MCU version/getpid required; no Linux patch or status request issued"))
		} else {
			r, e = invoke("baseline_reason", base+libWakeupReason, reasonAddr, 0)
			if e != nil || r != 0 {
				diagReady = false
				fail("diagnostic baseline reason", fmt.Errorf("return=%#x error=%v; no Linux patch or status request issued", r, e))
			} else {
				logf("DIAG_BASELINE_OK version=2.139 reason query succeeded")
				restoreReply, e = patchLinuxReply(&t, base)
				if e != nil {
					diagReady = false
					fail("Linux reply patch", e)
				} else {
					m.LinuxReplyPatch = "installed temporarily"
					defer func() {
						if retained {
							logf("LINUX_REPLY_RETAINED worker pending; reset on Linux reboot")
							return
						}
						if restoreReply != nil {
							if e := restoreReply(); e != nil {
								logf("LINUX_REPLY_RESTORE_ERROR %v", e)
							}
						}
					}()
				}
			}
		}
	}
	if !retained && diagReady && (want["status"] || want["adapter-probe"]) {
		statusAddr := address + 0x2200
		if e = t.write(statusAddr, make([]byte, 80)); e != nil {
			fail("status init", e)
		} else {
			r, e = invoke("status_adapter", address+0x100, statusAddr, 0)
			if e != nil {
				fail("status adapter", e)
			} else if r != 0 {
				fail("status adapter", fmt.Errorf("ioctl returned %#x", r))
			} else if rb, re := t.read(statusAddr, 80); re != nil {
				fail("status read", re)
			} else if want["adapter-probe"] {
				expectedAddr := statusAddr + 0x100
				r, e = invoke("direct_reason_control", base+libWakeupReason, expectedAddr, 0)
				if e != nil || r != 0 {
					fail("adapter control", fmt.Errorf("return %d %v", r, e))
				} else if direct, de := t.read(expectedAddr, 4); de != nil || word(direct, 0) != word(rb, 0) {
					fail("adapter control", errors.New("reason mismatch"))
				} else {
					m.AdapterProbe = "PASS: five-argument adapter agrees with direct wake-reason wrapper"
					logf("ADAPTER_PROBE_OK reason=%d", word(rb, 0))
				}
			} else if decoded, de := decodeStatus(rb); de != nil {
				fail("status decode", de)
			} else {
				m.Status = decoded
				if we := writeFile(filepath.Join(*outDir, "status.bin"), rb); we != nil {
					fail("status save", we)
				}
				if we := writeJSON(filepath.Join(*outDir, "status.json"), decoded); we != nil {
					fail("status json", we)
				}
				logf("STATUS_OK received=%d assertions=%d", decoded["received"], decoded["assertions"])
			}
		}
	}

	if restoreReply != nil && !retained {
		if e = t.setword(version, 0); e != nil {
			fail("post-diag version init", e)
		} else {
			r, e = invoke("post_diag_version", versionPLT, version, 0)
			vb, re := t.read(version, 4)
			if e != nil || re != nil || r != 0 || word(vb, 0) != 0x2008b {
				fail("post-diag version", fmt.Errorf("return=%#x error=%v read=%v", r, e, re))
			} else {
				logf("POST_DIAG_VERSION_OK version=2.139")
			}
		}
		if !retained {
			r, e = invoke("post_diag_reason", base+libWakeupReason, reasonAddr, 0)
			if e != nil || r != 0 {
				fail("post-diag reason", fmt.Errorf("return=%#x error=%v", r, e))
			} else {
				logf("POST_DIAG_REASON_OK")
			}
		}
		if !retained {
			if e = restoreReply(); e != nil {
				fail("Linux reply restore", e)
			} else {
				restoreReply = nil
				m.LinuxReplyPatch = "restored; both original instructions read back"
			}
		}
	}
	if !retained && want["reason"] {
		if e = t.write(reasonAddr, make([]byte, 4)); e != nil {
			fail("wakeup reason", e)
		} else {
			r, e = invoke("wakeup_reason", base+libWakeupReason, reasonAddr, 0)
			m.WakeupReason.Return = r
			m.WakeupReason.ReturnHex = fmt.Sprintf("%#x", r)
			m.WakeupReason.ReturnSigned = int32(r)
			if e != nil {
				fail("wakeup reason", e)
			} else if r != 0 {
				fail("wakeup reason", fmt.Errorf("return %#x", r))
			} else if rb, re := t.read(reasonAddr, 4); re != nil {
				fail("wakeup reason", re)
			} else {
				m.WakeupReason.OK = true
				m.WakeupReason.Value = word(rb, 0)
				m.WakeupReason.ValueHex = fmt.Sprintf("%#x", m.WakeupReason.Value)
				logf("WAKEUP_REASON value=%d hex=%#x", m.WakeupReason.Value, m.WakeupReason.Value)
			}
		}
	}

	if !retained && want["info"] {
		if e = t.write(infoAddr, make([]byte, infoSize)); e != nil {
			fail("wakeup info", e)
		} else {
			r, e = invoke("wakeup_info", base+libWifiWakeupInfo, infoAddr, 0)
			m.WakeupInfo.Return = r
			m.WakeupInfo.ReturnHex = fmt.Sprintf("%#x", r)
			m.WakeupInfo.ReturnSigned = int32(r)
			if e != nil {
				fail("wakeup info", e)
			} else if r != 0 {
				fail("wakeup info", fmt.Errorf("return %#x", r))
			} else if ib, re := t.read(infoAddr, infoSize); re != nil {
				fail("wakeup info", re)
			} else {
				m.WakeupInfo.OK = true
				m.WakeupInfo.Bytes = len(ib)
				m.WakeupInfo.SHA256 = hash(ib)
				m.WakeupInfo.File = filepath.Join(*outDir, "wakeup_info.bin")
				if we := writeFile(m.WakeupInfo.File, ib); we != nil {
					fail("wakeup info save", we)
				} else if we = writeFile(filepath.Join(*outDir, "wakeup_info.hex"), []byte(hexDump(ib))); we != nil {
					fail("wakeup info hex save", we)
				}
				logf("WAKEUP_INFO bytes=%d sha256=%s", len(ib), m.WakeupInfo.SHA256)
			}
		}
	}

	if !retained && want["awake"] {
		if e = t.write(awakeAddr, make([]byte, awakeBufferSize)); e != nil {
			fail("awake packet", e)
		} else {
			r, e = invoke("awake_packet", base+libAwakePacket, awakeAddr, awakeBufferSize)
			m.AwakePacket.Return = r
			m.AwakePacket.ReturnHex = fmt.Sprintf("%#x", r)
			m.AwakePacket.ReturnSigned = int32(r)
			if e != nil {
				fail("awake packet", e)
			} else if r != 0 {
				fail("awake packet", fmt.Errorf("return %#x", r))
			} else if ab, re := t.read(awakeAddr, awakeSize); re != nil {
				fail("awake packet", re)
			} else {
				m.AwakePacket.OK = true
				m.AwakePacket.Bytes = len(ab)
				m.AwakePacket.SHA256 = hash(ab)
				m.AwakePacket.File = filepath.Join(*outDir, "awake_packet.bin")
				if we := writeFile(m.AwakePacket.File, ab); we != nil {
					fail("awake packet save", we)
				} else if we = writeFile(filepath.Join(*outDir, "awake_packet.txt"), []byte(printableStrings(ab))); we != nil {
					fail("awake packet text save", we)
				}
				logf("AWAKE_PACKET bytes=%d sha256=%s", len(ab), m.AwakePacket.SHA256)
			}
		}
	}

	logcfg := &logcfgResult{Requested: *enableLog && want["fwlog"]}
	if want["fwlog"] {
		m.Logcfg = logcfg
	}
	if *enableLog && want["fwlog"] && !retained {
		r, e = invoke("logcfg_on", base+libSetLogcfg, 1, 0)
		logcfg.OnReturn = r
		logcfg.OnHex = fmt.Sprintf("%#x", r)
		if e != nil {
			fail("logcfg on", e)
		} else if r != 0 {
			fail("logcfg on", fmt.Errorf("return %#x", r))
		}
	}

	if !retained && want["fwlog"] {
		r, e = invoke("fw_log", base+libFwLog, 0, 0)
		m.FwLog.Return = r
		m.FwLog.ReturnHex = fmt.Sprintf("%#x", r)
		m.FwLog.ReturnSigned = int32(r)
		if e != nil {
			fail("fw log", e)
		} else if r == 0 || r == 0xffffffff {
			fail("fw log", fmt.Errorf("returned no buffer (%#x)", r))
		} else if e = ms.requireReadable(r, fwLogSize, "fw_log buffer"); e != nil {
			fail("fw log", e)
		} else if lb, re := t.read(r, fwLogSize); re != nil {
			fail("fw log", re)
		} else {
			m.FwLog.OK = true
			m.FwLog.Bytes = len(lb)
			m.FwLog.SHA256 = hash(lb)
			m.FwLog.PointerHex = fmt.Sprintf("%#x", r)
			m.FwLog.File = filepath.Join(*outDir, "fw_log.bin")
			if we := writeFile(m.FwLog.File, lb); we != nil {
				fail("fw log save", we)
			} else if we = writeFile(filepath.Join(*outDir, "fw_log.txt"), []byte(printableStrings(lb))); we != nil {
				fail("fw log text save", we)
			}
			logf("FW_LOG bytes=%d sha256=%s pointer=%#x", len(lb), m.FwLog.SHA256, r)
		}
	}

	if *enableLog && want["fwlog"] && !retained {
		r, e = invoke("logcfg_off", base+libSetLogcfg, 0, 0)
		logcfg.OffReturn = r
		logcfg.OffHex = fmt.Sprintf("%#x", r)
		if e != nil {
			fail("logcfg off", e)
		} else if r != 0 {
			fail("logcfg off", fmt.Errorf("return %#x", r))
		}
	} else if *enableLog && want["fwlog"] && retained {
		logcfg.SkippedOff = "worker retained; logcfg not restored"
		fail("logcfg off", errors.New("skipped because a worker was retained; MCU logcfg may remain 1"))
	}

	if m.WakeupReason.OK {
		rb := make([]byte, 4)
		put(rb, 0, m.WakeupReason.Value)
		if we := writeFile(filepath.Join(*outDir, "wakeup_reason.bin"), rb); we != nil {
			fail("wakeup reason save", we)
		}
		if we := writeFile(filepath.Join(*outDir, "wakeup_reason.txt"),
			[]byte(fmt.Sprintf("%d %#x\n", m.WakeupReason.Value, m.WakeupReason.Value))); we != nil {
			fail("wakeup reason text save", we)
		}
	}

	if !retained {
		if e = t.attach(); e != nil {
			fail("munmap attach", e)
		} else {
			r, e = t.call(unmapPLT, address, mapSize)
			if e != nil {
				fail("munmap", e)
			} else if r != 0 {
				fail("munmap", fmt.Errorf("return %d", r))
			}
			if e = t.detach(); e != nil {
				fail("munmap detach", e)
			}
		}
	} else {
		fail("scratch", errors.New("worker retained; scratch mapping left in place"))
	}

	if len(m.Errors) == 0 {
		m.Result = "OK"
	} else {
		m.Result = "PARTIAL"
	}
	if e = writeJSON(filepath.Join(*outDir, "manifest.json"), m); e != nil {
		return e
	}

	out := stdoutResult{Manifest: m}
	if m.Status != nil {
		if rb, e := readWhole(filepath.Join(*outDir, "status.bin")); e == nil {
			out.StatusB64 = base64.StdEncoding.EncodeToString(rb)
		}
	}
	if m.WakeupReason.OK {
		out.WakeupReasonHex = fmt.Sprintf("%08x", m.WakeupReason.Value)
	}
	if m.WakeupInfo.OK {
		if ib, re := readWhole(m.WakeupInfo.File); re == nil {
			out.WakeupInfoB64 = base64.StdEncoding.EncodeToString(ib)
		}
	}
	if m.FwLog.OK {
		if lb, re := readWhole(m.FwLog.File); re == nil {
			out.FwLogB64 = base64.StdEncoding.EncodeToString(lb)
		}
	}
	if m.AwakePacket.OK {
		if ab, re := readWhole(m.AwakePacket.File); re == nil {
			out.AwakePacketB64 = base64.StdEncoding.EncodeToString(ab)
		}
	}
	js, e := json.Marshal(out)
	if e != nil {
		return e
	}
	logf("READBACK_%s out=%s errors=%d", m.Result, *outDir, len(m.Errors))
	fmt.Printf("READBACK_RESULT %s\n", js)
	if m.Result != "OK" {
		return fmt.Errorf("readback partial: %d error(s)", len(m.Errors))
	}
	return nil
}

func main() {
	runtime.LockOSThread()
	defer func() {
		if p := recover(); p != nil {
			fmt.Fprintln(os.Stderr, p)
			os.Exit(1)
		}
	}()
	if e := run(); e != nil {
		fmt.Fprintln(os.Stderr, e)
		os.Exit(1)
	}
}

// Temporary, cache-coherent instruction changes in the mapped Linux library.
// Install common handler first (equivalent for its existing command 0x1f),
// then route 0x12. Restore in the opposite order. Disk and MCU flash unchanged.
func patchLinuxReply(t *target, base uint32) (func() error, error) {
	type patch struct{ offset, before, after uint32 }
	patches := []patch{{0xd08c, 0xe3a0c01f, 0xe5d5c000}, {0xd048, 0xea00001b, 0xea00000c}}
	for _, p := range patches {
		b, e := t.read(base+p.offset, 4)
		if e != nil {
			return nil, e
		}
		if word(b, 0) != p.before {
			return nil, fmt.Errorf("reply patch precondition offset=%#x got=%x", p.offset, b)
		}
	}
	if e := t.attach(); e != nil {
		return nil, e
	}
	changed := 0
	restore := func() error {
		if !t.attached {
			if e := t.attach(); e != nil {
				return e
			}
		}
		for changed > 0 {
			p := patches[changed-1]
			b, e := t.read(base+p.offset, 4)
			if e != nil {
				return e
			}
			if word(b, 0) != p.after {
				return fmt.Errorf("reply restore unexpected word at %#x", p.offset)
			}
			b = make([]byte, 4)
			put(b, 0, p.before)
			if n, e := syscall.PtracePokeText(t.pid, uintptr(base+p.offset), b); e != nil || n != 4 {
				return fmt.Errorf("reply restore: %d %v", n, e)
			}
			check, e := t.read(base+p.offset, 4)
			if e != nil || word(check, 0) != p.before {
				return fmt.Errorf("reply restore readback at %#x: %v", p.offset, e)
			}
			changed--
			logf("LINUX_REPLY_RESTORED offset=%#x word=%#x", p.offset, p.before)
		}
		return t.detach()
	}
	for _, p := range patches {
		b := make([]byte, 4)
		put(b, 0, p.after)
		if n, e := syscall.PtracePokeText(t.pid, uintptr(base+p.offset), b); e != nil || n != 4 {
			re := restore()
			return nil, fmt.Errorf("reply patch write: %d %v rollback=%v", n, e, re)
		}
		changed++
		check, e := t.read(base+p.offset, 4)
		if e != nil || word(check, 0) != p.after {
			re := restore()
			return nil, fmt.Errorf("reply patch readback: %v rollback=%v", e, re)
		}
		logf("LINUX_REPLY_PATCHED offset=%#x before=%#x after=%#x", p.offset, p.before, p.after)
	}
	if e := t.detach(); e != nil {
		re := restore()
		return nil, fmt.Errorf("reply patch detach: %v rollback=%v", e, re)
	}
	return restore, nil
}
