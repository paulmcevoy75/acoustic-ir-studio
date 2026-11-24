#!/usr/bin/env python3
"""Build a versioned release zip on Windows (or anywhere) without pandoc/wkhtmltopdf/zip.

    python tools/package_release.py 0.2.1 [--irs irs] [--out deliverables] [--libdaisy PATH]

Does the same as tools/package_release.sh: normal + diag firmware, the Windows uploader,
the manual as PDF (Markdown -> HTML here, HTML -> PDF with Microsoft Edge or Chrome headless),
START-HERE.txt, CHANGES.txt, the control chart and the IR files.  Run from the repo root.
Needs on PATH: make, arm-none-eabi-gcc, go.
"""
import argparse, html, os, re, shutil, subprocess, sys, zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def run(cmd, **kw):
    print("  $", " ".join(str(c) for c in cmd))
    subprocess.run(cmd, check=True, **kw)


def build_firmware(libdaisy, diag):
    for f in ("build/main.o", "build/main.d"):
        (ROOT / f).unlink(missing_ok=True)
    cmd = ["make", "-s", f"LIBDAISY_DIR={libdaisy}"]
    if diag:
        cmd.append("EXTRA_DEFS=-DPUREIR_DIAG")
    run(cmd, cwd=ROOT, stdout=subprocess.DEVNULL)
    return ROOT / "build" / "PureIR.bin"


# ---- tiny Markdown -> HTML (only what docs/MANUAL.md uses) ---------------------------
def inline(t):
    t = html.escape(t, quote=False)
    t = re.sub(r"`([^`]+)`", r"<code>\1</code>", t)
    t = re.sub(r"\*\*([^*]+)\*\*", r"<b>\1</b>", t)
    t = re.sub(r"!\[([^\]]*)\]\(([^)]+)\)", r'<img alt="\1" src="\2">', t)
    t = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r'<a href="\2">\1</a>', t)
    t = re.sub(r"(?<![\"=>])(https?://[^\s<]+)", r'<a href="\1">\1</a>', t)
    return t


def md_to_html(md, title):
    out, para, lst, table = [], [], None, []

    def flush():
        nonlocal para, lst, table
        if para:
            out.append("<p>" + inline(" ".join(para)) + "</p>")
            para = []
        if lst:
            out.append(f"</{lst}>")
            lst = None
        if table:
            rows = [r for r in table if not re.fullmatch(r"\|?[\s|:-]+\|?", r)]
            cells = [[c.strip() for c in r.strip().strip("|").split("|")] for r in rows]
            out.append("<table>")
            for i, r in enumerate(cells):
                tag = "th" if i == 0 else "td"
                out.append("<tr>" + "".join(f"<{tag}>{inline(c)}</{tag}>" for c in r) + "</tr>")
            out.append("</table>")
            table = []

    for line in md.splitlines():
        s = line.rstrip()
        m_h = re.match(r"(#{1,4})\s+(.*)", s)
        m_ol = re.match(r"\d+\.\s+(.*)", s)
        m_ul = re.match(r"[-*]\s+(.*)", s)
        if not s:
            flush()
        elif s.startswith("|"):
            if para or lst:
                t = table; table = []; flush(); table = t
            table.append(s)
        elif m_h:
            flush()
            n = len(m_h.group(1))
            out.append(f"<h{n}>{inline(m_h.group(2))}</h{n}>")
        elif m_ol or m_ul:
            kind = "ol" if m_ol else "ul"
            if para or table or (lst and lst != kind):
                flush()
            if not lst:
                out.append(f"<{kind}>")
                lst = kind
            out.append("<li>" + inline((m_ol or m_ul).group(1)) + "</li>")
        else:
            if lst or table:
                flush()
            para.append(s.strip())
    flush()
    css = ("body{font-family:Helvetica,Arial,sans-serif;font-size:11pt;line-height:1.4;margin:0 12px}"
           "h1{font-size:20pt}h2{font-size:14pt;margin-top:18px;border-bottom:1px solid #999}"
           "table{border-collapse:collapse}td,th{border:1px solid #888;padding:3px 8px}img{max-width:100%}"
           "code{font-size:10pt}")
    return (f"<!DOCTYPE html><html><head><meta charset='utf-8'><title>{html.escape(title)}</title>"
            f"<style>{css}</style></head><body>\n" + "\n".join(out) + "\n</body></html>\n")


def find_browser():
    for p in (r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
              r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
              r"C:\Program Files\Google\Chrome\Application\chrome.exe",
              "/usr/bin/chromium", "/usr/bin/google-chrome"):
        if os.path.exists(p):
            return p
    sys.exit("No Edge or Chrome found for making the manual PDF")


def make_pdf(html_path, pdf_path):
    pdf_path.unlink(missing_ok=True)
    run([find_browser(), "--headless", "--disable-gpu", "--no-pdf-header-footer",
         f"--print-to-pdf={pdf_path}", html_path.resolve().as_uri()],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if not pdf_path.exists() or pdf_path.stat().st_size < 1000:
        sys.exit("manual PDF was not made")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("version")
    ap.add_argument("--irs", default=str(ROOT / "irs"))
    ap.add_argument("--out", default=str(ROOT / "deliverables"))
    ap.add_argument("--libdaisy", default="../../deps/libDaisy")
    a = ap.parse_args()
    v = a.version
    src = (ROOT / "src" / "main.cpp").read_text(encoding="utf-8")
    if f'#define PUREIR_VERSION "{v}"' not in src:
        sys.exit(f"src/main.cpp PUREIR_VERSION is not {v}")

    out = Path(a.out).resolve()
    name = f"PureIR-v{v}"
    stage = out / name
    shutil.rmtree(stage, ignore_errors=True)
    for d in ("firmware", "docs", "your-IRs", "uploader"):
        (stage / d).mkdir(parents=True)

    print("firmware (diag, then normal)")
    shutil.copyfile(build_firmware(a.libdaisy, True), stage / "firmware" / "PureIR-diag.bin")
    shutil.copyfile(build_firmware(a.libdaisy, False), stage / "firmware" / "PureIR.bin")

    print("uploader")
    env = dict(os.environ, GOOS="windows", GOARCH="amd64", CGO_ENABLED="0")
    run(["go", "build", "-trimpath", "-ldflags", "-s -w", "-o",
         str(stage / "uploader" / "pureir-upload.exe"), "."], cwd=ROOT / "tools" / "pureir-upload", env=env)

    print("manual PDF")
    shutil.copyfile(ROOT / "docs" / "PureIR-controls.png", stage / "docs" / "PureIR-controls.png")
    md = (ROOT / "docs" / "MANUAL.md").read_text(encoding="utf-8")
    md += "\n\n## Control chart\n\n![controls](PureIR-controls.png)\n"
    tmp_html = stage / "docs" / "_manual.html"
    tmp_html.write_text(md_to_html(md, f"PureIR manual v{v}"), encoding="utf-8")
    make_pdf(tmp_html, stage / "docs" / "PureIR-Manual.pdf")
    tmp_html.unlink()

    print("text files and IRs")
    tpl = (ROOT / "docs" / "START-HERE.template.txt").read_text(encoding="utf-8")
    crlf = lambda t: t.replace("\r\n", "\n").replace("\n", "\r\n")  # Notepad-friendly
    (stage / "START-HERE.txt").write_bytes(crlf(tpl.replace("{VERSION}", f"v{v}")).encode("utf-8"))
    (stage / "CHANGES.txt").write_bytes(crlf((ROOT / "CHANGES.txt").read_text(encoding="utf-8")).encode("utf-8"))
    (stage / "VERSION.txt").write_text(f"v{v}\n")
    for w in sorted(Path(a.irs).glob("*.wav")):
        shutil.copyfile(w, stage / "your-IRs" / w.name)  # copyfile: no read-only flags

    zpath = out / f"{name}.zip"
    zpath.unlink(missing_ok=True)
    with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED) as z:
        for p in sorted(stage.rglob("*")):
            z.write(p, p.relative_to(out).as_posix())
    shutil.rmtree(stage)
    print(zpath)
    with zipfile.ZipFile(zpath) as z:
        for i in z.infolist():
            print(f"  {i.file_size:>9}  {i.filename}")


if __name__ == "__main__":
    main()
