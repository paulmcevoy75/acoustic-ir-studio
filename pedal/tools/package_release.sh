#!/usr/bin/env bash
# Build a versioned release zip:  tools/package_release.sh 0.1.1 [IR_DIR] [OUT_DIR]
# Needs: make, arm-none-eabi-gcc, pandoc, wkhtmltopdf, zip.  Run from the repo root.
set -euo pipefail
V="${1:?version, e.g. 0.1.1}"; IR_DIR="${2:-/home/claude/irs}"; OUT="${3:-/home/claude/out}"
NAME="PureIR-v${V}"; STAGE="$OUT/$NAME"
rm -rf "$STAGE" "$OUT/$NAME.zip"; mkdir -p "$STAGE"/{firmware,docs,your-IRs,uploader}
# firmware: normal + diag
rm -f build/main.o build/main.d; make -s EXTRA_DEFS=-DPUREIR_DIAG >/dev/null; cp build/PureIR.bin "$STAGE/firmware/PureIR-diag.bin"
rm -f build/main.o build/main.d; make -s >/dev/null;                          cp build/PureIR.bin "$STAGE/firmware/PureIR.bin"
# Windows uploader
(cd tools/pureir-upload && GOOS=windows GOARCH=amd64 CGO_ENABLED=0 go build -trimpath -ldflags "-s -w" -o "$STAGE/uploader/pureir-upload.exe" .)
# manual PDF
cat > "$OUT/manual.css" <<'CSS'
body{font-family:Helvetica,Arial,sans-serif;font-size:11pt;line-height:1.4;margin:0 12px}
h1{font-size:20pt}h2{font-size:14pt;margin-top:18px;border-bottom:1px solid #999}
table{border-collapse:collapse}td,th{border:1px solid #888;padding:3px 8px}img{max-width:100%}
CSS
{ cat docs/MANUAL.md; printf '\n\n## Control chart\n\n![controls](%s)\n' "$PWD/docs/PureIR-controls.png"; } > "$OUT/manual-full.md"
pandoc -f markdown+lists_without_preceding_blankline "$OUT/manual-full.md" -s --metadata title="PureIR manual v${V}" -c "$OUT/manual.css" -o "$OUT/manual.html"
wkhtmltopdf --enable-local-file-access -q "$OUT/manual.html" "$STAGE/docs/PureIR-Manual.pdf"
cp docs/PureIR-controls.png "$STAGE/docs/"
sed "s/{VERSION}/v${V}/g" docs/START-HERE.template.txt > "$STAGE/START-HERE.txt"
cp CHANGES.txt "$STAGE/CHANGES.txt"
cp "$IR_DIR"/*.wav "$STAGE/your-IRs/" 2>/dev/null || true
echo "v${V}" > "$STAGE/VERSION.txt"
(cd "$OUT" && zip -qr "$NAME.zip" "$NAME")
echo "$OUT/$NAME.zip"; unzip -l "$OUT/$NAME.zip"
