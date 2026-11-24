package main

import (
	"encoding/binary"
	"errors"
	"fmt"
	"math"
	"os"
)

const (
	outRate    = 48000
	maxSamples = 192000 // 4 s; must match the firmware (store::kMaxSamples)
)

// readWav reads a WAV file (PCM 8/16/24/32-bit or 32/64-bit float, any channel count)
// and returns mono samples in the range -1..1 and the sample rate.
func readWav(path string) ([]float64, int, error) {
	b, err := os.ReadFile(path)
	if err != nil {
		return nil, 0, err
	}
	return parseWav(b)
}

func parseWav(b []byte) ([]float64, int, error) {
	if len(b) < 12 || string(b[0:4]) != "RIFF" || string(b[8:12]) != "WAVE" {
		return nil, 0, errors.New("not a WAV file")
	}
	var format, channels, bits int
	var rate int
	var data []byte
	haveFmt := false
	p := 12
	for p+8 <= len(b) {
		id := string(b[p : p+4])
		sz := int(binary.LittleEndian.Uint32(b[p+4 : p+8]))
		body := p + 8
		end := body + sz
		if end > len(b) || sz < 0 {
			end = len(b) // streamed / truncated file: use what is there
		}
		switch id {
		case "fmt ":
			if end-body < 16 {
				return nil, 0, errors.New("bad fmt chunk")
			}
			format = int(binary.LittleEndian.Uint16(b[body:]))
			channels = int(binary.LittleEndian.Uint16(b[body+2:]))
			rate = int(binary.LittleEndian.Uint32(b[body+4:]))
			bits = int(binary.LittleEndian.Uint16(b[body+14:]))
			if format == 0xFFFE && end-body >= 26 {
				format = int(binary.LittleEndian.Uint16(b[body+24:])) // sub-format
			}
			haveFmt = true
		case "data":
			data = b[body:end]
		}
		p = end + (sz & 1)
		if data != nil && haveFmt {
			break
		}
	}
	if !haveFmt || data == nil {
		return nil, 0, errors.New("WAV has no audio data")
	}
	if channels < 1 || rate < 8000 || rate > 384000 {
		return nil, 0, fmt.Errorf("unsupported WAV (channels %d, rate %d)", channels, rate)
	}
	bps := bits / 8
	if (format == 1 && (bits != 8 && bits != 16 && bits != 24 && bits != 32)) ||
		(format == 3 && bits != 32 && bits != 64) || (format != 1 && format != 3) {
		return nil, 0, fmt.Errorf("unsupported WAV format (type %d, %d-bit)", format, bits)
	}
	frame := bps * channels
	n := len(data) / frame
	out := make([]float64, n)
	for i := 0; i < n; i++ {
		var sum float64
		for c := 0; c < channels; c++ {
			o := i*frame + c*bps
			var v float64
			switch {
			case format == 3 && bits == 32:
				v = float64(math.Float32frombits(binary.LittleEndian.Uint32(data[o:])))
			case format == 3 && bits == 64:
				v = math.Float64frombits(binary.LittleEndian.Uint64(data[o:]))
			case bits == 8:
				v = (float64(data[o]) - 128) / 128
			case bits == 16:
				v = float64(int16(binary.LittleEndian.Uint16(data[o:]))) / 32768
			case bits == 24:
				x := int32(data[o]) | int32(data[o+1])<<8 | int32(data[o+2])<<16
				if x&0x800000 != 0 {
					x |= ^0xFFFFFF
				}
				v = float64(x) / 8388608
			case bits == 32:
				v = float64(int32(binary.LittleEndian.Uint32(data[o:]))) / 2147483648
			}
			sum += v
		}
		out[i] = sum / float64(channels)
	}
	return out, rate, nil
}

// resample converts to 48 kHz with a windowed-sinc filter (band-limited when going down).
func resample(x []float64, from int) []float64 {
	if from == outRate {
		return x
	}
	r := float64(from) / float64(outRate)
	n := int(math.Floor(float64(len(x)) / r))
	fc := math.Min(1, 1/r)
	a := 16 * math.Max(1, r)
	out := make([]float64, n)
	for i := 0; i < n; i++ {
		pos := float64(i) * r
		k0 := int(math.Ceil(pos - a))
		k1 := int(math.Floor(pos + a))
		var s float64
		for k := k0; k <= k1; k++ {
			if k < 0 || k >= len(x) {
				continue
			}
			d := float64(k) - pos
			w := 0.5 + 0.5*math.Cos(math.Pi*d/a) // Hann window
			s += x[k] * sinc(d*fc) * fc * w
		}
		out[i] = s
	}
	return out
}

func sinc(x float64) float64 {
	if x == 0 {
		return 1
	}
	return math.Sin(math.Pi*x) / (math.Pi * x)
}

// prepare turns file samples into what the pedal stores: 48 kHz mono 16-bit, peak at full scale,
// at most 4 s (with a short fade-out if it had to be cut).
func prepare(x []float64, rate int) ([]int16, bool, error) {
	x = resample(x, rate)
	cut := false
	if len(x) > maxSamples {
		x = x[:maxSamples]
		cut = true
		fade := 480 // 10 ms
		for i := 0; i < fade; i++ {
			x[len(x)-1-i] *= 0.5 - 0.5*math.Cos(math.Pi*float64(i)/float64(fade))
		}
	}
	peak := 0.0
	for _, v := range x {
		peak = math.Max(peak, math.Abs(v))
	}
	if len(x) == 0 || peak == 0 {
		return nil, false, errors.New("the file is empty or silent")
	}
	out := make([]int16, len(x))
	for i, v := range x {
		q := math.Round(v / peak * 32767)
		out[i] = int16(math.Max(-32768, math.Min(32767, q)))
	}
	return out, cut, nil
}
