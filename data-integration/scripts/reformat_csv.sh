#!/usr/bin/env bash
# reformat_csv.sh — normalise CSV/TSV/TXT files to clean UTF-8, in place.
set -euo pipefail

PROG="$(basename -- "$0")"

usage() {
    cat <<EOF
${PROG} — normalise CSV/TSV/TXT files to clean UTF-8, in place.

Usage:
  ${PROG} FILE.csv [options]
  ${PROG} FILE1.csv FILE2.tsv ... [options]
  ${PROG} --folder DIR [options]

Input selection:
  --folder DIR        Process every *.csv / *.tsv / *.txt under DIR.
  --shallow           With --folder: do not descend into subdirectories.
  --recursive, -r     With --folder: descend (default).

Default transforms:
  --keep-bom          Do not strip a UTF-8 BOM.
  --keep-eol          Do not normalise line endings.

Optional transforms:
  --fix-mojibake      Reverse "UTF-8 read as Latin-1" mojibake when safe.
  --ascii-punct       Replace typographic Unicode punctuation with ASCII.
  --trim-headers      Strip whitespace from header fields only.

Safety / observability:
  --backup            Save FILE.bak before rewriting (never clobbers).
  --dry-run, -n       Report what would change; write nothing.
  --verbose, -v       Per-file details on stdout / stderr.
  -h, --help          Show this message.
EOF
}

# ---------------- Defaults --------------------------------------------
folder=""
files=()
recursive=1
backup=0
dry_run=0
strip_bom=1
fix_eol=1
fix_mojibake=0
ascii_punct=0
trim_headers=0
verbose=0

while [[ $# -gt 0 ]]; do
    case "$1" in
        --folder)         folder="$2"; shift 2 ;;
        --shallow|--no-recursive) recursive=0; shift ;;
        --recursive|-r)   recursive=1; shift ;;
        --backup)         backup=1; shift ;;
        --dry-run|-n)     dry_run=1; shift ;;
        --keep-bom)       strip_bom=0; shift ;;
        --keep-eol)       fix_eol=0; shift ;;
        --fix-mojibake)   fix_mojibake=1; shift ;;
        --ascii-punct)    ascii_punct=1; shift ;;
        --trim-headers)   trim_headers=1; shift ;;
        --verbose|-v)     verbose=1; shift ;;
        --help|-h)        usage; exit 0 ;;
        -*) echo "Unknown option: $1" >&2; usage >&2; exit 2 ;;
        *)  files+=("$1"); shift ;;
    esac
done

if [[ -n "$folder" && ${#files[@]} -gt 0 ]]; then
    echo "ERROR: use either --folder or explicit file paths, not both." >&2
    exit 2
fi
if [[ -z "$folder" && ${#files[@]} -eq 0 ]]; then
    echo "ERROR: no input given." >&2
    usage >&2
    exit 2
fi

command -v python3 >/dev/null 2>&1 || { echo "ERROR: python3 required." >&2; exit 1; }
if ! python3 -c 'import csv, io, os, sys, pathlib' >/dev/null 2>&1; then
    echo "ERROR: python3 is installed but cannot import standard modules." >&2
    exit 1
fi

# ---------------- Discover inputs -------------------------------------
if [[ -n "$folder" ]]; then
    [[ -d "$folder" ]] || { echo "ERROR: not a directory: $folder" >&2; exit 2; }
    find_args=( "$folder" -type f \( -iname '*.csv' -o -iname '*.tsv' -o -iname '*.txt' \) ! -name '.*' )
    [[ $recursive -eq 0 ]] && find_args=( "$folder" -maxdepth 1 "${find_args[@]:1}" )
    mapfile -t files < <(find "${find_args[@]}" | sort)
fi

if [[ ${#files[@]} -eq 0 ]]; then
    echo "No .csv/.tsv/.txt files found."
    exit 0
fi

# ---------------- Write Python helper to a temp file ------------------
# Running Python from a real file avoids the heredoc-in-subshell
# environment that hides tracebacks on some shells/venvs.
PY_HELPER="$(mktemp -t reformat_csv.XXXXXX.py)"
trap 'rm -f "$PY_HELPER"' EXIT

cat > "$PY_HELPER" <<'PYEOF'
import csv
import io
import os
import sys
import traceback
from pathlib import Path


def detect_encoding(data: bytes) -> str:
    if data.startswith(b'\xef\xbb\xbf'):
        return 'utf-8-sig'
    if data.startswith(b'\xff\xfe\x00\x00') or data.startswith(b'\x00\x00\xfe\xff'):
        return 'utf-32'
    if data.startswith(b'\xff\xfe') or data.startswith(b'\xfe\xff'):
        return 'utf-16'

    try:
        data.decode('utf-8')
        return 'utf-8'
    except UnicodeDecodeError:
        pass

    if len(data) >= 8:
        sample = data[:8192]
        n = len(sample)
        odd_nulls  = sum(1 for i in range(1, n, 2) if sample[i] == 0)
        even_nulls = sum(1 for i in range(0, n, 2) if sample[i] == 0)
        if odd_nulls > n * 0.30 and even_nulls < n * 0.05:
            return 'utf-16-le'
        if even_nulls > n * 0.30 and odd_nulls < n * 0.05:
            return 'utf-16-be'

    try:
        import chardet  # type: ignore
        r = chardet.detect(data[:131072])
        if r and r.get('encoding') and (r.get('confidence') or 0) > 0.65:
            return r['encoding']
    except Exception:
        pass

    try:
        from charset_normalizer import from_bytes  # type: ignore
        m = from_bytes(data[:131072]).best()
        if m is not None and getattr(m, 'encoding', None):
            return m.encoding
    except Exception:
        pass

    return 'cp1252'


def fix_mojibake(text: str) -> str:
    try:
        candidate = text.encode('latin-1').decode('utf-8')
    except (UnicodeDecodeError, UnicodeEncodeError):
        return text
    # Suspicious characters that survive a bad UTF-8 -> Latin-1 round trip.
    bad = '\u00c3\u00c2\u00e2\ufffd'
    if sum(candidate.count(c) for c in bad) < sum(text.count(c) for c in bad):
        return candidate
    return text


ASCII_PUNCT = str.maketrans({
    '\u2018': "'", '\u2019': "'", '\u201A': "'", '\u201B': "'",
    '\u201C': '"', '\u201D': '"', '\u201E': '"',
    '\u2013': '-', '\u2014': '-', '\u2026': '...',
    '\u00A0': ' ', '\u2009': ' ', '\u200A': ' ', '\u202F': ' ',
    '\u200B': '', '\uFEFF': '',
})


def trim_header_fields(text: str) -> str:
    """Trim whitespace in the header row only. Body is preserved verbatim."""
    first_nl = text.find('\n')
    if first_nl < 0:
        header_line, body = text, ''
    else:
        header_line, body = text[:first_nl], text[first_nl:]

    trailing_cr = ''
    if header_line.endswith('\r'):
        header_line = header_line[:-1]
        trailing_cr = '\r'

    try:
        fields = next(csv.reader(io.StringIO(header_line)))
    except (csv.Error, StopIteration):
        return text

    fields = [f.strip() for f in fields]

    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator='')
    writer.writerow(fields)
    return buf.getvalue() + trailing_cr + body


def _main() -> int:
    path           = Path(sys.argv[1])
    strip_bom      = sys.argv[2] == '1'
    fix_eol        = sys.argv[3] == '1'
    do_mojibake    = sys.argv[4] == '1'
    do_ascii_punct = sys.argv[5] == '1'
    do_trim_header = sys.argv[6] == '1'
    dry_run        = sys.argv[7] == '1'

    try:
        raw = path.read_bytes()
    except OSError as e:
        print(f"error|read failed: {e}", file=sys.stderr)
        return 1

    if not raw:
        print("empty|0")
        return 0

    enc = detect_encoding(raw)

    try:
        text = raw.decode(enc)
    except (UnicodeDecodeError, LookupError):
        text = raw.decode('cp1252', errors='replace')
        enc = 'cp1252 (fallback)'

    if do_mojibake:
        text = fix_mojibake(text)
    if do_ascii_punct:
        text = text.translate(ASCII_PUNCT)
    if strip_bom and text and text[0] == '\ufeff':
        text = text[1:]
    if do_trim_header:
        text = trim_header_fields(text)
    if fix_eol:
        text = text.replace('\r\n', '\n').replace('\r', '\n')

    out = text.encode('utf-8')

    if out == raw:
        print(f"unchanged|{enc}|{len(raw)}")
        return 0

    if dry_run:
        print(f"converted|{enc}|{len(raw)}|{len(out)}")
        return 0

    tmp = path.with_name(path.name + '.utf8.tmp')
    try:
        tmp.write_bytes(out)
        os.replace(tmp, path)
    finally:
        try:
            if tmp.exists():
                tmp.unlink()
        except OSError:
            pass

    print(f"converted|{enc}|{len(raw)}|{len(out)}")
    return 0


if __name__ == '__main__':
    try:
        sys.exit(_main())
    except Exception as exc:
        print(f"uncaught: {type(exc).__name__}: {exc}", file=sys.stderr)
        traceback.print_exc()
        sys.exit(1)
PYEOF

# ---------------- Run loop --------------------------------------------
scope="explicit"
[[ -n "$folder" ]] && scope="folder ($([[ $recursive -eq 1 ]] && echo recursive || echo shallow))"
echo "Processing ${#files[@]} file(s) [$scope]"
[[ $dry_run -eq 1 ]] && echo "(dry-run: nothing will be written)"
[[ $verbose -eq 1 ]] && echo "Python:  $(python3 --version 2>&1)  ·  helper: $PY_HELPER"
echo

converted=0
unchanged=0
skipped=0
failed=0

for f in "${files[@]}"; do
    if [[ ! -f "$f" ]]; then
        printf '  [skip]      %s (not a regular file)\n' "$f"
        skipped=$((skipped + 1))
        continue
    fi

    if [[ $backup -eq 1 && $dry_run -eq 0 && ! -e "$f.bak" ]]; then
        cp -p -- "$f" "$f.bak"
    fi

    err_file="$(mktemp -t reformat_csv_err.XXXXXX)"
    result=""
    exit_code=0

    result=$(python3 "$PY_HELPER" "$f" \
                "$strip_bom" "$fix_eol" "$fix_mojibake" \
                "$ascii_punct" "$trim_headers" "$dry_run" \
                2>"$err_file") || exit_code=$?

    if [[ $exit_code -ne 0 ]]; then
        failed=$((failed + 1))
        echo "  [error]     $f — converter failed (exit $exit_code)" >&2
        if [[ -s "$err_file" ]]; then
            sed 's/^/    /' "$err_file" >&2
        fi
    elif [[ -z "$result" ]]; then
        failed=$((failed + 1))
        echo "  [error]     $f — converter produced no output" >&2
        [[ -s "$err_file" ]] && sed 's/^/    /' "$err_file" >&2
    else
        IFS='|' read -r action enc rest <<< "$result" || true

        case "$action" in
            unchanged)
                unchanged=$((unchanged + 1))
                [[ $verbose -eq 1 ]] && printf '  [unchanged] %s  (%s)\n' "$f" "$enc"
                ;;
            converted)
                converted=$((converted + 1))
                if [[ $verbose -eq 1 ]]; then
                    IFS='|' read -r before after <<< "$rest" || true
                    if [[ $dry_run -eq 1 ]]; then
                        printf '  [would]     %s  (%s → utf-8, %s → %s bytes)\n' \
                               "$f" "$enc" "$before" "$after"
                    else
                        printf '  [converted] %s  (%s → utf-8, %s → %s bytes)\n' \
                               "$f" "$enc" "$before" "$after"
                    fi
                fi
                ;;
            empty)
                skipped=$((skipped + 1))
                [[ $verbose -eq 1 ]] && printf '  [empty]     %s\n' "$f"
                ;;
            *)
                failed=$((failed + 1))
                echo "  [error]     $f — unexpected output: $result" >&2
                ;;
        esac

        if [[ $verbose -eq 1 && -s "$err_file" ]]; then
            sed 's/^/    [stderr]  /' "$err_file" >&2
        fi
    fi

    rm -f "$err_file"
done

echo
echo "=== Summary ==="
printf '  discovered : %d\n' "${#files[@]}"
printf '  unchanged  : %d\n' "$unchanged"
printf '  converted  : %d\n' "$converted"
printf '  skipped    : %d\n' "$skipped"
printf '  failed     : %d\n' "$failed"
[[ $dry_run -eq 1 ]] && echo "  (dry-run — no files were modified)"

[[ $failed -gt 0 ]] && exit 1
exit 0