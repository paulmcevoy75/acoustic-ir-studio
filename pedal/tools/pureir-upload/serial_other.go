//go:build !windows && !linux

package main

import "errors"

func openPort(name string) (Port, error) {
	return nil, errors.New("this build only supports Windows and Linux")
}
func candidatePorts() []string { return nil }
