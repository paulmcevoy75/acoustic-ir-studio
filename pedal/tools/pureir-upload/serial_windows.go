//go:build windows

package main

import (
	"fmt"
	"syscall"
	"unsafe"
)

var (
	k32             = syscall.NewLazyDLL("kernel32.dll")
	pGetCommState   = k32.NewProc("GetCommState")
	pSetCommState   = k32.NewProc("SetCommState")
	pSetCommTimeout = k32.NewProc("SetCommTimeouts")
	pSetupComm      = k32.NewProc("SetupComm")
	pPurgeComm      = k32.NewProc("PurgeComm")
)

type dcb struct {
	DCBlength  uint32
	BaudRate   uint32
	Flags      uint32
	WReserved  uint16
	XonLim     uint16
	XoffLim    uint16
	ByteSize   uint8
	Parity     uint8
	StopBits   uint8
	XonChar    int8
	XoffChar   int8
	ErrorChar  int8
	EofChar    int8
	EvtChar    int8
	WReserved1 uint16
}

type commTimeouts struct {
	ReadIntervalTimeout         uint32
	ReadTotalTimeoutMultiplier  uint32
	ReadTotalTimeoutConstant    uint32
	WriteTotalTimeoutMultiplier uint32
	WriteTotalTimeoutConstant   uint32
}

type winPort struct{ h syscall.Handle }

func openPort(name string) (Port, error) {
	path := `\\.\` + name
	p, err := syscall.UTF16PtrFromString(path)
	if err != nil {
		return nil, err
	}
	h, err := syscall.CreateFile(p, syscall.GENERIC_READ|syscall.GENERIC_WRITE, 0, nil, syscall.OPEN_EXISTING, syscall.FILE_ATTRIBUTE_NORMAL, 0)
	if err != nil {
		return nil, err
	}
	var d dcb
	d.DCBlength = uint32(unsafe.Sizeof(d))
	if r, _, e := pGetCommState.Call(uintptr(h), uintptr(unsafe.Pointer(&d))); r == 0 {
		syscall.CloseHandle(h)
		return nil, fmt.Errorf("GetCommState: %v", e)
	}
	d.BaudRate = 115200
	d.ByteSize = 8
	d.Parity = 0
	d.StopBits = 0
	d.Flags = 1 | (1 << 4) | (1 << 12) // binary mode, DTR on, RTS on, no flow control
	if r, _, e := pSetCommState.Call(uintptr(h), uintptr(unsafe.Pointer(&d))); r == 0 {
		syscall.CloseHandle(h)
		return nil, fmt.Errorf("SetCommState: %v", e)
	}
	// Read returns after 100 ms even if nothing arrived
	t := commTimeouts{ReadIntervalTimeout: 0xFFFFFFFF, ReadTotalTimeoutMultiplier: 0xFFFFFFFF, ReadTotalTimeoutConstant: 100, WriteTotalTimeoutConstant: 5000}
	pSetCommTimeout.Call(uintptr(h), uintptr(unsafe.Pointer(&t)))
	pSetupComm.Call(uintptr(h), 16384, 16384)
	pPurgeComm.Call(uintptr(h), 0x0F)
	return &winPort{h}, nil
}

func (w *winPort) Read(b []byte) (int, error) {
	var n uint32
	err := syscall.ReadFile(w.h, b, &n, nil)
	return int(n), err
}
func (w *winPort) Write(b []byte) (int, error) {
	var n uint32
	err := syscall.WriteFile(w.h, b, &n, nil)
	return int(n), err
}
func (w *winPort) Close() error { return syscall.CloseHandle(w.h) }

// candidatePorts: every COM port that exists (opening a missing one fails at once)
func candidatePorts() []string {
	var out []string
	for i := 1; i <= 99; i++ {
		out = append(out, fmt.Sprintf("COM%d", i))
	}
	return out
}
