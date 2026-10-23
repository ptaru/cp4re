package main

import (
	"bytes"
	"errors"
	"flag"
	"fmt"
	"os"
	"path/filepath"
	"runtime"
	"strconv"
	"strings"
	"syscall"
	"unsafe"
)

const sleepWord = 0x670bd4
const mapSize = 0x10000
const invalidReturn = 0xdeadbeec
const createPLT = 0x170c4
const mmapPLT = 0x16ef0
const joinPLT = 0x17b20
const unmapPLT = 0x17454
const pidPLT = 0x16e78
const versionPLT = 0x17100
const fileVersionPLT = 0x16950
const upgradeWrapper = 0x2fd9c

var logFile *os.File

func logf(f string, a ...any) {
	s := fmt.Sprintf(f+"\n", a...)
	if logFile != nil {
		logFile.WriteString(s)
		logFile.Sync()
	}
	fmt.Print(s)
}
func pause() { ts := syscall.Timespec{Nsec: 10000000}; syscall.Nanosleep(&ts, nil) }
func must(e error) {
	if e != nil {
		panic(e)
	}
}

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
	// /proc/PID/wchan is absent on this kernel. Inspect the actual stopped ARM
	// syscall context instead; require an interruptible sleep, nanosleep number and SVC.
	// This kernel leaves the original pointer in r0 at attach, rather than -ERESTART*.
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
	// mmap/pthread setup calls use a saved, separate portion of the borrowed stack.
	// The actual worker obtains its own stack from pthread_create.
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
	// Never cancel an in-flight injected function or restore its registers on a
	// timeout: it may hold a libc lock. Log stalls and keep supervision instead.
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
func executableMaps(pid int) error {
	b, e := readWhole(fmt.Sprintf("/proc/%d/maps", pid))
	if e != nil {
		return e
	}
	text := string(b)
	if !strings.Contains(text, "/dav/ezapp") || !strings.Contains(text, "liblpwwifi.so") {
		return errors.New("expected images not mapped")
	}
	for _, l := range strings.Split(text, "\n") {
		if !strings.Contains(l, "/dav/ezapp") {
			continue
		}
		fields := strings.Fields(l)
		if len(fields) < 2 || !strings.Contains(fields[1], "x") {
			continue
		}
		var lo, hi uint32
		fmt.Sscanf(fields[0], "%x-%x", &lo, &hi)
		if lo <= upgradeWrapper && upgradeWrapper < hi {
			return nil
		}
	}
	return errors.New("wrapper not in expected executable mapping")
}
func run() (err error) {
	mode := flag.String("mode", "check", "check, probe, or flash")
	image := flag.String("image", "/mnt/mmc01/hi3861-wake/candidate.bin", "exact pinned candidate")
	logPath := flag.String("log", "/mnt/mmc01/hi3861-wake/launcher.log", "durable log")
	adopt := flag.Bool("adopt-hold", false, "probe only: take over the preparation hold and restore sleep on completion")
	execute := flag.String("execute-flash", "", "flash requires exact value INSTALL_CP4_HI3861_PREWAKE")
	flag.Parse()
	if *mode != "check" && *mode != "probe" && *mode != "flash" {
		return errors.New("invalid mode")
	}
	if *mode == "flash" && *execute != "INSTALL_CP4_HI3861_PREWAKE" {
		return errors.New("flash not armed")
	}
	if *adopt && *mode != "probe" {
		return errors.New("adopt-hold is only valid in probe mode")
	}
	if *mode != "flash" && *execute != "" {
		return errors.New("execution token supplied outside flash mode")
	}
	// Exclusive control; persistent lock deliberately survives ambiguous outcomes.
	lock := filepath.Join(filepath.Dir(*logPath), "launcher.lock")
	if e := os.Mkdir(lock, 0700); e != nil {
		return fmt.Errorf("lock: %w", e)
	}
	committed := false
	defer func() {
		if !committed {
			os.Remove(lock)
		}
	}()
	logFile, err = openBlocking(*logPath, os.O_WRONLY|os.O_CREATE|os.O_APPEND|syscall.O_SYNC, 0600)
	if err != nil {
		return err
	}
	defer logFile.Close()
	defer func() {
		if err != nil {
			logf("ERROR %v committed=%v", err, committed)
		}
	}()
	logf("START mode=%s launcher_pid=%d", *mode, os.Getpid())
	for path, sha := range map[string]string{"/dav/ezapp": appSHA, "/dav/liblpwwifi.so": librarySHA, "/dav/Hi3861L_ezviz_sdk_ota.bin": originalSHA} {
		if e := checkFile(path, sha); e != nil {
			return e
		}
	}
	data, e := readWhole(*image)
	if e != nil {
		return e
	}
	if e = validateOTA(data); e != nil {
		return e
	}
	// Pin the open inode for the updater, instead of passing a replaceable filename.
	pinned, e := openBlocking(*image, syscall.O_RDONLY, 0)
	if e != nil {
		return e
	}
	defer pinned.Close()
	pb := make([]byte, len(data))
	if _, e = pinned.ReadAt(pb, 0); e != nil {
		return e
	}
	if hash(pb) != candidateSHA {
		return errors.New("candidate changed while opening")
	}
	pid, e := findPID()
	if e != nil {
		return e
	}
	if e = executableMaps(pid); e != nil {
		return e
	}
	logf("PREFLIGHT_OK ezapp_pid=%d candidate_sha256=%s bytes=%d", pid, candidateSHA, len(data))
	if *mode == "check" {
		logf("CHECK_OK no memory writes, MCU commands or updater calls")
		return nil
	}
	mem, e := openBlocking(fmt.Sprintf("/proc/%d/mem", pid), os.O_RDWR, 0)
	if e != nil {
		return e
	}
	defer mem.Close()
	t := target{pid: pid, mem: mem}
	hold, e := t.read(sleepWord, 4)
	if e != nil {
		return e
	}
	if word(hold, 0) != 1 {
		if !*adopt || word(hold, 0) != 0 {
			return errors.New("sleep flag not 1; another hold manager may be active")
		}
		put(hold, 0, 1)
	}
	if e = t.setword(sleepWord, 0); e != nil {
		return e
	}
	logf("SLEEP_HOLD acquired")
	defer func() {
		if !committed {
			if e := t.write(sleepWord, hold); e != nil {
				logf("SLEEP_RESTORE_ERROR %v", e)
			} else {
				logf("SLEEP_HOLD restored")
			}
		}
	}()
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
	ctx := address + 0x1000
	thread := address + 0x2000
	version := address + 0x2010
	path := address + 0x2100
	filepathBytes := []byte(fmt.Sprintf("/proc/%d/fd/%d\x00", os.Getpid(), pinned.Fd()))
	if e = t.write(path, filepathBytes); e != nil {
		return e
	}
	if e = t.detach(); e != nil {
		return e
	}
	invoke := func(name string, fn, a0, a1 uint32, mayFlash bool) (uint32, error) {
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
		logf("WORKER_CREATE name=%s function=%#x arg0=%#x arg1=%d", name, fn, a0, a1)
		if mayFlash {
			committed = true
			logf("FLASH_COMMITTED calling OEM wrapper; do not interrupt or retry")
		}
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
			if !mayFlash && i == 1000 {
				return 0, errors.New("probe did not finish; worker retained, not interrupted")
			}
			pause()
		}
	}
	r, e := invoke("getpid", pidPLT, 0, 0, false)
	if e != nil {
		return e
	}
	if r != uint32(pid) {
		return fmt.Errorf("worker getpid %d != %d", r, pid)
	}
	r, e = invoke("MCU_version_read", versionPLT, version, 0, false)
	if e != nil {
		return e
	}
	vb, e := t.read(version, 4)
	if e != nil {
		return e
	}
	v := word(vb, 0)
	if r != 0 || v != 0x2008b {
		return fmt.Errorf("MCU version read ret=%#x value=%#x", r, v)
	}
	logf("MCU_VERSION_OK version=2.139")

	if e = t.setword(version, 0); e != nil {
		return e
	}
	r, e = invoke("pinned_image_read", fileVersionPLT, version, path, false)
	if e != nil {
		return e
	}
	vb, e = t.read(version, 4)
	if e != nil {
		return e
	}
	v = word(vb, 0)
	if r != 0 || v != 0x2008b {
		return fmt.Errorf("pinned image metadata read ret=%#x version=%#x", r, v)
	}
	logf("PINNED_IMAGE_READ_OK candidate opened inside ezapp through helper fd; version=2.139")
	if *mode == "flash" {
		logf("FLASH_START image_fd_path=%s wrapper=%#x wait_seconds=10", strings.TrimRight(string(filepathBytes), "\x00"), upgradeWrapper)
		r, e = invoke("OEM_kernel_update", upgradeWrapper, path, 10, true)
		if e != nil {
			return e
		}
		logf("FLASH_WRAPPER_RETURN r0=%#x; this does not prove MCU reboot/boot success", r)
		// No automatic reset, second update, sleep release, or lock deletion after commit.
		// The OEM wrapper owns its delay and reboot. Preserve evidence on any outcome.
		return nil
	}
	if e = t.attach(); e != nil {
		return e
	}
	r, e = t.call(unmapPLT, address, mapSize)
	if e != nil {
		return e
	}
	if r != 0 {
		return fmt.Errorf("munmap %d", r)
	}
	if e = t.detach(); e != nil {
		return e
	}
	logf("PROBE_OK fresh-thread call, two-argument ABI, MCU read, join, unmap and GPR/VFP/stack restore; updater NOT called")
	for i := 0; i < 300; i++ {
		pause()
	} // bounded time to retrieve evidence before restoring sleep
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
