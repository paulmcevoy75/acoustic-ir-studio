//go:build linux

package main

import (
	"io"
	"os"
	"path/filepath"
	"syscall"
	"unsafe"
)

type unixPort struct{ f *os.File }

func openPort(name string) (Port, error) {
	f, err := os.OpenFile(name, os.O_RDWR|syscall.O_NOCTTY, 0)
	if err != nil {
		return nil, err
	}
	var t syscall.Termios
	if _, _, e := syscall.Syscall(syscall.SYS_IOCTL, f.Fd(), syscall.TCGETS, uintptr(unsafe.Pointer(&t))); e != 0 {
		f.Close()
		return nil, e
	}
	t.Iflag = 0
	t.Oflag = 0
	t.Lflag = 0
	t.Cflag = syscall.CS8 | syscall.CREAD | syscall.CLOCAL
	t.Cc[syscall.VMIN] = 0
	t.Cc[syscall.VTIME] = 1 // 100 ms
	if _, _, e := syscall.Syscall(syscall.SYS_IOCTL, f.Fd(), syscall.TCSETS, uintptr(unsafe.Pointer(&t))); e != 0 {
		f.Close()
		return nil, e
	}
	return &unixPort{f}, nil
}

func (u *unixPort) Read(b []byte) (int, error) {
	n, err := u.f.Read(b)
	if n == 0 && err == io.EOF {
		return 0, nil // timeout
	}
	return n, err
}
func (u *unixPort) Write(b []byte) (int, error) { return u.f.Write(b) }
func (u *unixPort) Close() error                { return u.f.Close() }

func candidatePorts() []string {
	m, _ := filepath.Glob("/dev/ttyACM*")
	return m
}
