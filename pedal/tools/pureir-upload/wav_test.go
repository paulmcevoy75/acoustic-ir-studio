package main

import (
	"encoding/binary"
	"math"
	"testing"
)

func mkWav(format, ch, rate, bits int, frames [][]float64) []byte {
	bps := bits / 8
	data := []byte{}
	for _, fr := range frames {
		for _, v := range fr {
			switch {
			case format == 3:
				b := make([]byte, 4)
				binary.LittleEndian.PutUint32(b, math.Float32bits(float32(v)))
				data = append(data, b...)
			case bits == 16:
				b := make([]byte, 2)
				binary.LittleEndian.PutUint16(b, uint16(int16(v*32767)))
				data = append(data, b...)
			case bits == 24:
				x := int32(v * 8388607)
				data = append(data, byte(x), byte(x>>8), byte(x>>16))
			}
		}
	}
	b := []byte("RIFF\x00\x00\x00\x00WAVE")
	f := make([]byte, 24)
	copy(f, "fmt ")
	binary.LittleEndian.PutUint32(f[4:], 16)
	binary.LittleEndian.PutUint16(f[8:], uint16(format))
	binary.LittleEndian.PutUint16(f[10:], uint16(ch))
	binary.LittleEndian.PutUint32(f[12:], uint32(rate))
	binary.LittleEndian.PutUint32(f[16:], uint32(rate*ch*bps))
	binary.LittleEndian.PutUint16(f[20:], uint16(ch*bps))
	binary.LittleEndian.PutUint16(f[22:], uint16(bits))
	b = append(b, f...)
	// an unknown chunk first, odd sized, to test padding
	b = append(b, []byte("junk\x03\x00\x00\x00abc\x00")...)
	d := make([]byte, 8)
	copy(d, "data")
	binary.LittleEndian.PutUint32(d[4:], uint32(len(data)))
	b = append(b, d...)
	b = append(b, data...)
	return b
}

func TestFloatMono(t *testing.T) {
	fr := [][]float64{{0.5}, {-0.25}, {0}}
	x, rate, err := parseWav(mkWav(3, 1, 48000, 32, fr))
	if err != nil || rate != 48000 || len(x) != 3 || math.Abs(x[0]-0.5) > 1e-6 || math.Abs(x[1]+0.25) > 1e-6 {
		t.Fatalf("%v %v %v", x, rate, err)
	}
}

func TestStereo24Mix(t *testing.T) {
	x, _, err := parseWav(mkWav(1, 2, 44100, 24, [][]float64{{1, 0}, {0, -1}}))
	if err != nil || len(x) != 2 || math.Abs(x[0]-0.5) > 1e-3 || math.Abs(x[1]+0.5) > 1e-3 {
		t.Fatalf("%v %v", x, err)
	}
}

func TestResamplePreservesTone(t *testing.T) {
	n := 44100
	x := make([]float64, n)
	for i := range x {
		x[i] = math.Sin(2 * math.Pi * 1000 * float64(i) / 44100)
	}
	y := resample(x, 44100)
	if len(y) != 48000 {
		t.Fatalf("len %d", len(y))
	}
	var e float64
	for i := 200; i < len(y)-200; i++ {
		e = math.Max(e, math.Abs(y[i]-math.Sin(2*math.Pi*1000*float64(i)/48000)))
	}
	if e > 0.01 {
		t.Fatalf("resample error %g", e)
	}
	// downsampling must remove content above the new Nyquist, not alias it
	hi := make([]float64, 96000)
	for i := range hi {
		hi[i] = math.Sin(2 * math.Pi * 30000 * float64(i) / 96000)
	}
	d := resample(hi, 96000)
	var m float64
	for _, v := range d[500 : len(d)-500] {
		m = math.Max(m, math.Abs(v))
	}
	if m > 0.02 {
		t.Fatalf("aliasing %g", m)
	}
}

func TestPrepareTrimAndNormalise(t *testing.T) {
	x := make([]float64, 300000)
	for i := range x {
		x[i] = 0.3
	}
	s, cut, err := prepare(x, 48000)
	if err != nil || !cut || len(s) != maxSamples || s[1000] != 32767 || s[len(s)-1] != 0 {
		t.Fatalf("len %d cut %v err %v v %d last %d", len(s), cut, err, s[1000], s[len(s)-1])
	}
	if _, _, err := prepare(make([]float64, 100), 48000); err == nil {
		t.Fatal("silent file accepted")
	}
}

func TestRejectsGarbage(t *testing.T) {
	if _, _, err := parseWav([]byte("hello world, not a wav")); err == nil {
		t.Fatal("accepted")
	}
}
