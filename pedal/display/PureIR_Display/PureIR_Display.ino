// PureIR display - ESP32 "Cheap Yellow Display" (ESP32-2432S028R, ILI9341 320x240)
//
// Listens on GPIO35 (UART2 RX, 115200 8N1) for status frames from the Daisy
// (Daisy D12 -> ESP32 GPIO35, plus a common GND) and draws them.  The pedal does
// not need this display: if it is absent or unpowered nothing changes on the Daisy.
//
// Needs: Arduino-ESP32 core 2.x or 3.x, library "LovyanGFX" (by lovyan03).
// If colours look inverted or red/blue are swapped, change the two defines below.
// If your board is the ST7789 variant (single USB-C), set PANEL_ST7789 to 1.

#define DISPLAY_INVERT     0
#define DISPLAY_RGB_ORDER  0
#define PANEL_ST7789       0
#define PIN_BACKLIGHT      21   // some CYD revisions use 27

#define LGFX_USE_V1
#include <LovyanGFX.hpp>
#include "link_parser.h"
#include "params_table.h"

class Lcd : public lgfx::LGFX_Device
{
#if PANEL_ST7789
    lgfx::Panel_ST7789 panel_;
#else
    lgfx::Panel_ILI9341 panel_;
#endif
    lgfx::Bus_SPI     bus_;
    lgfx::Light_PWM   light_;

  public:
    Lcd()
    {
        {
            auto c        = bus_.config();
            c.spi_host    = HSPI_HOST;
            c.spi_mode    = 0;
            c.freq_write  = 40000000;
            c.freq_read   = 16000000;
            c.spi_3wire   = false;
            c.use_lock    = true;
            c.dma_channel = SPI_DMA_CH_AUTO;
            c.pin_sclk    = 14;
            c.pin_mosi    = 13;
            c.pin_miso    = 12;
            c.pin_dc      = 2;
            bus_.config(c);
            panel_.setBus(&bus_);
        }
        {
            auto c          = panel_.config();
            c.pin_cs        = 15;
            c.pin_rst       = -1;
            c.pin_busy      = -1;
            c.panel_width   = 240;
            c.panel_height  = 320;
            c.offset_x      = 0;
            c.offset_y      = 0;
            c.readable      = false;
            c.invert        = DISPLAY_INVERT;
            c.rgb_order     = DISPLAY_RGB_ORDER;
            c.dlen_16bit    = false;
            c.bus_shared    = false;
            panel_.config(c);
        }
        {
            auto c       = light_.config();
            c.pin_bl     = PIN_BACKLIGHT;
            c.invert     = false;
            c.freq       = 44100;
            c.pwm_channel = 7;
            light_.config(c);
            panel_.setLight(&light_);
        }
        setPanel(&panel_);
    }
};

static Lcd         lcd;
static FrameParser parser;
static AirStatus   cur, shown;
static bool        haveData = false, drawnOnce = false, shownLink = true;
static uint32_t    lastFrame = 0;

// ---- colours ---------------------------------------------------------------
static const uint16_t C_BG = 0x0000, C_FG = 0xFFFF, C_DIM = 0x7BEF, C_BAR = 0x2D7F,
                      C_OK = 0x07E0, C_WARN = 0xFFE0, C_BAD = 0xF800, C_PANEL = 0x18C3;

static float ValueOf(int idx, int v1000)
{
    const DParam& d = kDParams[idx];
    const float   x = v1000 / 1000.f;
    return d.exp ? d.lo * powf(d.hi / d.lo, x) : d.lo + (d.hi - d.lo) * x;
}

static void FormatValue(int idx, int v1000, char* out, size_t n)
{
    const float v = ValueOf(idx, v1000);
    const DParam& d = kDParams[idx];
    if(v >= 100.f)       snprintf(out, n, "%.0f %s", v, d.unit);
    else if(v >= 10.f)   snprintf(out, n, "%.1f %s", v, d.unit);
    else                 snprintf(out, n, "%.2f %s", v, d.unit);
}

// ---- drawing ---------------------------------------------------------------
static void Bar(int x, int y, int w, int h, float frac, uint16_t col, uint16_t bg = C_PANEL)
{
    frac = frac < 0 ? 0 : (frac > 1 ? 1 : frac);
    const int fw = (int)(frac * w);
    lcd.fillRect(x, y, fw, h, col);
    lcd.fillRect(x + fw, y, w - fw, h, bg);
}

static float DbFrac(int db) { return (db + 60) / 60.f; }   // -60..0 dB

static void DrawHeader(const AirStatus& s)
{
    lcd.fillRect(0, 0, 320, 22, C_PANEL);
    lcd.setTextDatum(textdatum_t::middle_left);
    lcd.setTextColor(C_FG, C_PANEL);
    lcd.setFont(&fonts::Font2);
    lcd.drawString(kDBanks[s.bank], 6, 11);
    lcd.setTextDatum(textdatum_t::middle_center);
    lcd.setTextColor(C_BAR, C_PANEL);
    lcd.drawString(kDEngines[s.engine], 160, 11);
    lcd.setTextDatum(textdatum_t::middle_right);
    char t[24];
    if(s.flags & F_MUTE)         { lcd.setTextColor(C_WARN, C_PANEL); snprintf(t, sizeof t, "MUTE"); }
    else if(s.flags & F_BYPASS)  { lcd.setTextColor(C_BAD, C_PANEL);  snprintf(t, sizeof t, "BYPASS"); }
    else                         { lcd.setTextColor(C_OK, C_PANEL);   snprintf(t, sizeof t, "ON"); }
    lcd.drawString(t, 314, 11);
}

static void DrawIr(const AirStatus& s)
{
    lcd.fillRect(0, 24, 320, 62, C_BG);
    lcd.setTextDatum(textdatum_t::top_left);
    lcd.setFont(&fonts::Font4);
    lcd.setTextColor(C_FG, C_BG);
    lcd.drawString(s.name[0] ? s.name : "-", 6, 30);
    lcd.setFont(&fonts::Font2);
    char t[40];
    snprintf(t, sizeof t, "IR %d/%d %s", s.irCnt ? s.irIdx + 1 : 0, s.irCnt,
             (s.flags & F_BUILTIN) ? "(built-in)" : "(SD card)");
    lcd.setTextColor(C_DIM, C_BG);
    lcd.drawString(t, 6, 60);
    // status badge
    const char* msg = nullptr;
    uint16_t    col = C_OK;
    if(s.flags & F_LOADING)       { msg = "LOADING..."; col = C_BAR; }
    else if(s.flags & F_ERROR)    { msg = "IR ERROR";   col = C_BAD; }
    else if(s.flags & F_OVERLOAD) { msg = "CPU LATE";   col = C_BAD; }
    else if(!(s.flags & F_SD))    { msg = "NO SD";      col = C_WARN; }
    if(msg)
    {
        lcd.setTextDatum(textdatum_t::top_right);
        lcd.setTextColor(col, C_BG);
        lcd.drawString(msg, 314, 62);
    }
}

static void DrawMeters(const AirStatus& s)
{
    lcd.setFont(&fonts::Font2);
    lcd.setTextDatum(textdatum_t::middle_left);
    lcd.fillRect(0, 88, 320, 40, C_BG);
    lcd.setTextColor(C_DIM, C_BG);
    lcd.drawString("IN", 6, 96);
    lcd.drawString("OUT", 6, 114);
    Bar(40, 91, 200, 10, DbFrac(s.inDb), s.inDb > -3 ? C_BAD : (s.inDb > -12 ? C_WARN : C_OK));
    Bar(40, 109, 200, 10, DbFrac(s.outDb), (s.flags & F_CLIP) ? C_BAD : (s.outDb > -6 ? C_WARN : C_OK));
    char t[24];
    lcd.setTextColor(C_FG, C_BG);
    snprintf(t, sizeof t, "%d dB", s.inDb <= -99 ? -99 : s.inDb);
    lcd.drawString(t, 246, 96);
    snprintf(t, sizeof t, "%d dB", s.outDb <= -99 ? -99 : s.outDb);
    lcd.drawString(t, 246, 114);
    lcd.setTextColor(C_DIM, C_BG);
    snprintf(t, sizeof t, "GR %d", s.grDb);
    lcd.drawString(t, 286, 96);
    snprintf(t, sizeof t, "%d%%", s.cpu);
    lcd.drawString(t, 286, 114);
}

static void DrawParams(const AirStatus& s, const AirStatus* old)
{
    lcd.setFont(&fonts::Font2);
    for(int k = 0; k < 6; ++k)
    {
        const int idx = s.bank * 6 + k;
        const int col = k % 2, row = k / 2;
        const int x = 6 + col * 158, y = 134 + row * 34;
        if(old && old->bank == s.bank && old->v[idx] == s.v[idx]) continue;
        lcd.fillRect(x, y, 152, 32, C_BG);
        lcd.setTextDatum(textdatum_t::top_left);
        lcd.setTextColor(C_DIM, C_BG);
        lcd.drawString(kDParams[idx].name, x, y);
        char t[24];
        FormatValue(idx, s.v[idx], t, sizeof t);
        lcd.setTextDatum(textdatum_t::top_right);
        lcd.setTextColor(C_FG, C_BG);
        lcd.drawString(t, x + 150, y);
        Bar(x, y + 19, 150, 7, s.v[idx] / 1000.f, C_BAR, C_PANEL);
    }
}

static void DrawNoLink(bool on)
{
    lcd.fillRect(0, 0, 320, 22, C_PANEL);
    lcd.setFont(&fonts::Font2);
    lcd.setTextDatum(textdatum_t::middle_left);
    lcd.setTextColor(on ? C_WARN : C_FG, C_PANEL);
    lcd.drawString(on ? "PureIR - waiting for pedal (GPIO35)..." : "PureIR", 6, 11);
}

void setup()
{
    Serial.begin(115200);
    Serial2.begin(115200, SERIAL_8N1, 35, -1);   // RX only
    lcd.init();
    lcd.setRotation(1);
    lcd.setBrightness(200);
    lcd.fillScreen(C_BG);
    DrawNoLink(true);
}

void loop()
{
    bool got = false;
    while(Serial2.available())
        got |= parser.Feed((char)Serial2.read(), cur);
    const uint32_t now = millis();
    if(got)
    {
        lastFrame = now;
        haveData  = true;
        if(!shownLink) shownLink = true;
        const AirStatus* old = drawnOnce ? &shown : nullptr;
        if(!old || old->bank != cur.bank || old->engine != cur.engine || old->flags != cur.flags)
            DrawHeader(cur);
        if(!old || strcmp(old->name, cur.name) || old->irIdx != cur.irIdx || old->irCnt != cur.irCnt
           || old->flags != cur.flags)
            DrawIr(cur);
        // meters change constantly: redraw at ~10 Hz (one frame each)
        DrawMeters(cur);
        DrawParams(cur, old);
        shown     = cur;
        drawnOnce = true;
    }
    if(haveData && shownLink && now - lastFrame > 1500)
    {
        shownLink = false;
        lcd.fillRect(0, 0, 320, 22, C_PANEL);
        lcd.setFont(&fonts::Font2);
        lcd.setTextDatum(textdatum_t::middle_left);
        lcd.setTextColor(C_BAD, C_PANEL);
        lcd.drawString("NO LINK FROM PEDAL", 6, 11);
        drawnOnce = false;   // full redraw when it returns
    }
    delay(2);
}
