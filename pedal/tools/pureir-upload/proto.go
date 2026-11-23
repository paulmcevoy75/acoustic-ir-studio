package main

import (
	"errors"
	"fmt"
	"hash/crc32"
	"io"
	"strings"
	"time"
)

// Port is a serial port whose Read gives up after a short time (returning 0 bytes).
type Port interface {
	io.ReadWriteCloser
}

type Session struct {
	p    Port
	buf  []byte
	Info string
	Log  func(string)
}

// readLine returns the next protocol line (starting with '@'); other text (the diagnostic
// firmware prints status lines too) is skipped.  ok=false on timeout.
func (s *Session) readLine(timeout time.Duration) (string, bool) {
	deadline := time.Now().Add(timeout)
	tmp := make([]byte, 256)
	for {
		for {
			i := strings.IndexByte(string(s.buf), '\n')
			if i < 0 {
				break
			}
			line := strings.TrimRight(string(s.buf[:i]), "\r")
			s.buf = s.buf[i+1:]
			if strings.HasPrefix(line, "@") {
				return line, true
			}
		}
		if time.Now().After(deadline) {
			return "", false
		}
		n, err := s.p.Read(tmp)
		if n > 0 {
			s.buf = append(s.buf, tmp[:n]...)
			if len(s.buf) > 1<<16 {
				s.buf = s.buf[len(s.buf)-4096:]
			}
		}
		if err != nil && err != io.EOF {
			return "", false
		}
	}
}

func (s *Session) send(line string) error {
	_, err := s.p.Write([]byte(line + "\n"))
	return err
}

// Command sends one line and returns the pedal's first reply line ("@ERR ..." is an error).
func (s *Session) Command(line string, timeout time.Duration) (string, error) {
	s.buf = nil
	if err := s.send(line); err != nil {
		return "", err
	}
	r, ok := s.readLine(timeout)
	if !ok {
		return "", errors.New("the pedal did not answer")
	}
	if strings.HasPrefix(r, "@ERR") {
		return r, errors.New("the pedal said: " + strings.TrimSpace(strings.TrimPrefix(r, "@ERR")))
	}
	return r, nil
}

func (s *Session) Ping() bool {
	s.buf = nil
	if s.send("PING") != nil {
		return false
	}
	line, ok := s.readLine(700 * time.Millisecond)
	if ok && strings.HasPrefix(line, pingTag+" ") {
		s.Info = strings.TrimPrefix(line, pingTag+" ")
		return true
	}
	return false
}

type Slot struct {
	N      int
	Length int
	Rate   int
	Name   string
}

func (s *Session) List() ([]Slot, error) {
	s.buf = nil
	if err := s.send("LIST"); err != nil {
		return nil, err
	}
	var out []Slot
	for {
		line, ok := s.readLine(3 * time.Second)
		if !ok {
			return nil, errors.New("the pedal did not answer")
		}
		if line == "@END" {
			return out, nil
		}
		var sl Slot
		if strings.HasPrefix(line, "@SLOT ") {
			parts := strings.SplitN(line[6:], " ", 4)
			if len(parts) >= 3 {
				fmt.Sscanf(parts[0], "%d", &sl.N)
				fmt.Sscanf(parts[1], "%d", &sl.Length)
				fmt.Sscanf(parts[2], "%d", &sl.Rate)
				if len(parts) == 4 {
					sl.Name = parts[3]
				}
				out = append(out, sl)
			}
		}
	}
}

func (s *Session) Delete(slot int) error {
	s.buf = nil
	if err := s.send(fmt.Sprintf("DEL %d", slot)); err != nil {
		return err
	}
	return s.expectOK(5 * time.Second)
}

func (s *Session) Boot() error {
	s.buf = nil
	if err := s.send("BOOT"); err != nil {
		return err
	}
	return s.expectOK(3 * time.Second)
}

func (s *Session) expectOK(t time.Duration) error {
	line, ok := s.readLine(t)
	if !ok {
		return errors.New("the pedal did not answer")
	}
	if strings.HasPrefix(line, "@OK") {
		return nil
	}
	return fmt.Errorf("the pedal said: %s", strings.TrimPrefix(line, "@ERR "))
}

// Put stores samples in a slot.  progress(done,total) is called after each chunk.
func (s *Session) Put(slot int, name string, samples []int16, progress func(int, int)) error {
	raw := make([]byte, len(samples)*2)
	for i, v := range samples {
		raw[2*i] = byte(uint16(v))
		raw[2*i+1] = byte(uint16(v) >> 8)
	}
	crc := crc32.ChecksumIEEE(raw)
	s.buf = nil
	if err := s.send(fmt.Sprintf("PUT %d %d %08x %s", slot, len(samples), crc, name)); err != nil {
		return err
	}
	// the pedal erases flash first (a few seconds)
	line, ok := s.readLine(60 * time.Second)
	if !ok {
		return errors.New("the pedal did not answer (is an old upload still running? wait 10 seconds and retry)")
	}
	if line != "@READY" {
		return fmt.Errorf("the pedal said: %s", strings.TrimPrefix(line, "@ERR "))
	}
	const chunk = 4096
	for off := 0; off < len(raw); off += chunk {
		end := off + chunk
		if end > len(raw) {
			end = len(raw)
		}
		if n, err := s.p.Write(raw[off:end]); err != nil {
			return err
		} else if n != end-off {
			return errors.New("the USB connection stopped accepting data")
		}
		line, ok := s.readLine(10 * time.Second)
		if !ok {
			return errors.New("the pedal stopped answering during the upload")
		}
		last := end == len(raw)
		switch {
		case !last && line == "@A":
		case last && strings.HasPrefix(line, "@OK"):
		default:
			return fmt.Errorf("the pedal said: %s", strings.TrimPrefix(line, "@ERR "))
		}
		if progress != nil {
			progress(end, len(raw))
		}
	}
	return nil
}
