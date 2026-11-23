# PureIR - Daisy Seed Pedal DevKit firmware
#
#   make                     build  (build/PureIR.bin)
#   make program-dfu         flash over USB (hold BOOT, tap RESET, then tap RESET again)
#
# LIBDAISY_DIR must point at a recent libDaisy checkout (built with `make -C ... `).
TARGET = PureIR

DEBUG = 0
OPT   = -O3

CPP_SOURCES = src/main.cpp

# BOOT_SRAM: the Daisy bootloader copies the app from QSPI into SRAM and runs it.
APP_TYPE = BOOT_SRAM
# our linker script = libDaisy's sram script + a .axi_bss section in D2 SRAM
LDSCRIPT = PureIR_sram.lds

USE_FATFS = 1

LIBDAISY_DIR ?= ../../deps/libDaisy
SYSTEM_FILES_DIR = $(LIBDAISY_DIR)/core

C_INCLUDES += -Isrc
C_DEFS += $(EXTRA_DEFS)   # e.g. make EXTRA_DEFS=-DPUREIR_DIAG  (diagnostic-light build)
CPP_STANDARD ?= -std=gnu++17
CPPFLAGS += -Wall -Wno-psabi

include $(SYSTEM_FILES_DIR)/Makefile
