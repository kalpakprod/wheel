#!/bin/sh
# wheel: обновляет общий каталог проектов из релиза wheel-catalog.
#
# Каталог живёт отдельно от плагина и пополняется ежедневно, поэтому его нельзя
# везти вместе с кодом. Запускается фоном из хука SessionStart не чаще раза в сутки.
# Зависимости — curl, gzip и sha256sum (на macOS shasum); jq не нужен.
set -eu

DIR="${WHEEL_HOME:-$HOME/.claude/wheel}"
META="$DIR/catalog.meta.json"
OUT="$DIR/catalog.jsonl"
TMP="$DIR/.catalog.tmp.$$"

MIRRORS="${WHEEL_MIRRORS:-\
https://github.com/kalpakprod/wheel-catalog/releases/latest/download \
https://cdn.jsdelivr.net/npm/@kalpakprod/wheel-catalog@latest \
https://unpkg.com/@kalpakprod/wheel-catalog@latest \
https://wheel.kalpak.dev}"

[ "${WHEEL_NO_UPDATE:-0}" = "1" ] && exit 0
if [ -f "$DIR/config.json" ] && grep -q '"autoupdate"[[:space:]]*:[[:space:]]*false' "$DIR/config.json"; then
    exit 0
fi

# значение строкового или числового поля верхнего уровня из плоского json
field() { sed -n "s/.*\"$1\"[[:space:]]*:[[:space:]]*\"\{0,1\}\([^\",}]*\)\"\{0,1\}.*/\1/p" "$2" | head -1; }

sha256() {
    if command -v sha256sum > /dev/null 2>&1; then sha256sum "$1" | cut -d' ' -f1
    else shasum -a 256 "$1" | cut -d' ' -f1; fi
}

cleanup() { rm -f "$TMP" "$TMP.gz" "$TMP.manifest"; }
trap cleanup EXIT INT TERM

mkdir -p "$DIR"
have=""
[ -f "$META" ] && have=$(field version "$META")

for base in $MIRRORS; do
    curl -fsSL --max-time 5 -o "$TMP.manifest" "$base/manifest.json" 2> /dev/null || continue
    want=$(field version "$TMP.manifest")
    [ -n "$want" ] || continue
    if [ "$want" = "$have" ] && [ -s "$OUT" ]; then
        touch "$META"                       # проверили, свежее нет: сутки не возвращаемся
        exit 0
    fi
    file=$(field file "$TMP.manifest")
    sum=$(field sha256 "$TMP.manifest")
    curl -fsSL --max-time 120 -o "$TMP.gz" "$base/${file:-catalog.jsonl.gz}" 2> /dev/null || continue
    [ "$(sha256 "$TMP.gz")" = "$sum" ] || continue   # битое или подменённое зеркало: берём следующее
    gzip -dc "$TMP.gz" > "$TMP" || continue
    [ -s "$TMP" ] || continue
    mv "$TMP" "$OUT"                        # переставляем готовое: половинчатого каталога не бывает
    mv "$TMP.manifest" "$META"
    exit 0
done

exit 0   # сети нет или все зеркала молчат: плагин работает на том, что уже лежит
