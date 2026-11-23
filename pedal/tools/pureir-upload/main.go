// pureir-upload: put guitar IRs (WAV files) onto the PureIR pedal over USB.
//
//	pureir-upload.exe my-guitar.wav            (or drag a WAV file onto the .exe)
//	pureir-upload.exe a.wav b.wav c.wav        (several IRs: they go into the next free slots)
//	pureir-upload.exe --slot 2 my-guitar.wav   (replace what is in slot 2)
//	pureir-upload.exe list | delete 2 | boot
//
// The pedal's USB-C port on the Daisy module must be connected and the pedal powered.
package main

import (
	"bufio"
	"flag"
	"fmt"
	"os"
	"path/filepath"
	"regexp"
	"strconv"
	"strings"
)

const version = "1.0"

// Settings and hooks for builds that add their own .go file to this folder
// (set them from that file's init(); extra command-line flags can be registered there too).
var (
	productName = "PureIR"        // shown in messages
	exeName     = "pureir-upload" // shown in the help text
	pingTag     = "@PUREIR"       // first word of the pedal's PING reply
	// fileHooks get every command-line file that is not a .wav; return true to take it.
	fileHooks []func(path string) bool
	// beforePut runs just before each IR is sent (for example to send extra data with it).
	beforePut []func(s *Session, slot int, wavPath string) error
)

var pause = true

func finish(code int) {
	if pause {
		fmt.Print("\nPress Enter to close this window...")
		bufio.NewReader(os.Stdin).ReadString('\n')
	}
	os.Exit(code)
}

func die(format string, a ...any) {
	fmt.Printf("\nERROR: "+format+"\n", a...)
	finish(1)
}

func findPedal(want string) (*Session, string) {
	var cands []string
	if want != "" {
		cands = []string{want}
	} else {
		cands = candidatePorts()
	}
	for _, name := range cands {
		p, err := openPort(name)
		if err != nil {
			continue
		}
		s := &Session{p: p}
		for try := 0; try < 2; try++ {
			if s.Ping() {
				return s, name
			}
		}
		p.Close()
	}
	return nil, ""
}

func cleanName(file string) string {
	n := strings.TrimSuffix(filepath.Base(file), filepath.Ext(file))
	n = regexp.MustCompile(`[^A-Za-z0-9 ._-]`).ReplaceAllString(n, "_")
	n = strings.TrimSpace(n)
	if len(n) > 23 {
		n = n[:23]
	}
	if n == "" {
		n = "IR"
	}
	return n
}

func bar(done, total int) {
	w := 30
	f := done * w / total
	fmt.Printf("\r  [%s%s] %3d%%", strings.Repeat("#", f), strings.Repeat(".", w-f), done*100/total)
}

func printSlots(sl []Slot) {
	if len(sl) == 0 {
		fmt.Println("  (no uploaded IRs yet)")
	}
	for _, s := range sl {
		fmt.Printf("  slot %d: %-23s %.2f s\n", s.N, s.Name, float64(s.Length)/float64(s.Rate))
	}
}

func main() {
	port := flag.String("port", "", "serial port, e.g. COM5 (normally found automatically)")
	slot := flag.Int("slot", -1, "slot number 0-7 to store into (default: next free slot)")
	name := flag.String("name", "", "name to show for the IR (default: the file name)")
	noPause := flag.Bool("no-pause", false, "do not wait for Enter at the end")
	flag.Usage = func() {
		fmt.Println(productName + " IR uploader " + version)
		fmt.Println("\nDrag a .wav file onto this program, or run:")
		fmt.Println("  " + exeName + " my-guitar.wav [more.wav ...]")
		fmt.Println("  " + exeName + " --slot 2 my-guitar.wav      replace slot 2")
		fmt.Println("  " + exeName + " list                        show what is stored")
		fmt.Println("  " + exeName + " delete 2                    empty slot 2")
		fmt.Println("  " + exeName + " boot                        restart the pedal into the bootloader (for flashing)")
		fmt.Println("\nOptions:")
		flag.PrintDefaults()
	}
	flag.Parse()
	// Go's flag package stops at the first file name; parse again so options may follow files.
	args := flag.Args()
	for {
		var rest []string
		moved := false
		for i := 0; i < len(args); i++ {
			if strings.HasPrefix(args[i], "-") && len(args[i]) > 1 {
				flag.CommandLine.Parse(args[i:])
				rest = append(rest, flag.Args()...)
				moved = true
				break
			}
			rest = append(rest, args[i])
		}
		args = rest
		if !moved {
			break
		}
	}
	pause = !*noPause

	fmt.Println(productName + " IR uploader " + version)
	s, pname := findPedal(*port)
	if s == nil {
		die("could not find the pedal.\n  - Is the USB cable in the Daisy module's own USB-C socket, and the pedal powered?\n  - Is it running " + productName + " v0.2.0 or newer? (older versions cannot take IRs over USB)\n  - If an upload was interrupted, wait 10 seconds and try again.\n  - You can name the port yourself: " + exeName + " --port COM5 ...")
	}
	defer s.p.Close()
	fmt.Printf("Found the pedal on %s (%s)\n", pname, s.Info)

	if len(args) == 0 || args[0] == "list" {
		sl, err := s.List()
		if err != nil {
			die("%v", err)
		}
		fmt.Println("Stored IRs:")
		printSlots(sl)
		if len(args) == 0 {
			fmt.Println("\nTo add an IR, drag a .wav file onto this program.")
		}
		finish(0)
	}
	switch args[0] {
	case "delete":
		if len(args) < 2 {
			die("usage: %s delete <slot 0-7>", exeName)
		}
		n, err := strconv.Atoi(args[1])
		if err != nil || n < 0 || n > 7 {
			die("slot must be 0 to 7")
		}
		if err := s.Delete(n); err != nil {
			die("%v", err)
		}
		fmt.Printf("Slot %d is now empty.\n", n)
		finish(0)
	case "boot":
		if err := s.Boot(); err != nil {
			die("%v", err)
		}
		fmt.Println("The pedal is restarting into the bootloader. You can now flash it at https://flash.daisy.audio (File Upload tab).")
		finish(0)
	}

	// upload one or more WAV files
	sl, err := s.List()
	if err != nil {
		die("%v", err)
	}
	used := map[int]bool{}
	for _, x := range sl {
		used[x.N] = true
	}
	if len(args) > 1 && *slot >= 0 {
		die("--slot can only be used with a single file")
	}
	// files a hook takes (not IRs) are handled first
	var wavs []string
	for _, file := range args {
		taken := false
		if !strings.EqualFold(filepath.Ext(file), ".wav") {
			for _, h := range fileHooks {
				if h(file) {
					taken = true
					break
				}
			}
		}
		if !taken {
			wavs = append(wavs, file)
		}
	}
	args = wavs
	failed := 0
	for _, file := range args {
		fmt.Printf("\n%s\n", filepath.Base(file))
		x, rate, err := readWav(file)
		if err != nil {
			fmt.Printf("  skipped: %v\n", err)
			failed++
			continue
		}
		samples, cut, err := prepare(x, rate)
		if err != nil {
			fmt.Printf("  skipped: %v\n", err)
			failed++
			continue
		}
		n := *slot
		if n < 0 {
			for i := 0; i < 8; i++ {
				if !used[i] {
					n = i
					break
				}
			}
			if n < 0 {
				fmt.Println("  skipped: all 8 slots are full. Use --slot N to replace one, or 'delete N' to empty one.")
				failed++
				continue
			}
		}
		if n > 7 {
			die("slot must be 0 to 7")
		}
		nm := *name
		if nm == "" {
			nm = cleanName(file)
		}
		if rate != outRate {
			fmt.Printf("  converted %d Hz to 48000 Hz\n", rate)
		}
		if cut {
			fmt.Println("  note: longer than 4 seconds, so the end was cut off")
		}
		fmt.Printf("  sending to slot %d as \"%s\" (%.2f s)\n", n, nm, float64(len(samples))/outRate)
		hookErr := error(nil)
		for _, h := range beforePut {
			if hookErr = h(s, n, file); hookErr != nil {
				break
			}
		}
		if hookErr != nil {
			fmt.Printf("  FAILED: %v\n", hookErr)
			failed++
			continue
		}
		if err := s.Put(n, nm, samples, bar); err != nil {
			fmt.Println()
			fmt.Printf("  FAILED: %v\n", err)
			failed++
			continue
		}
		used[n] = true
		fmt.Printf("\n  done: stored in slot %d.\n", n)
	}
	fmt.Println()
	if failed == 0 {
		fmt.Println("All done. The newest IR is playing on the pedal now; tap the right footswitch to step through the IRs.")
	} else {
		fmt.Printf("%d file(s) did not upload (see above).\n", failed)
	}
	if failed > 0 {
		finish(1)
	}
	finish(0)
}
