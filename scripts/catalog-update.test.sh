#!/bin/sh
# Проверка клиента обновления каталога на подставных зеркалах.
# Запуск: scripts/catalog-update.test.sh [каталог-с-релизом]
# По умолчанию берёт сборку куратора: ~/tools/curator/dist
set -eu

DIST="${1:-$HOME/tools/curator/dist}"
UP="$(dirname "$0")/catalog-update.sh"
GOOD=/tmp/wheel-test-good
BAD=/tmp/wheel-test-bad
HOME_DIR=/tmp/wheel-test-home
fail=0

if [ ! -f "$DIST/manifest.json" ]; then
    # на чужой машине и в CI сборки куратора нет: берём опубликованный релиз
    DIST=/tmp/wheel-test-dist
    mkdir -p "$DIST"
    base=https://github.com/kalpakprod/wheel-catalog/releases/latest/download
    curl -fsSL --max-time 60 -o "$DIST/manifest.json" "$base/manifest.json" &&
        curl -fsSL --max-time 180 -o "$DIST/catalog.jsonl.gz" "$base/catalog.jsonl.gz" ||
        { echo "нет сборки и релиз не скачался"; exit 1; }
fi

rm -rf "$GOOD" "$BAD" "$HOME_DIR"
mkdir -p "$GOOD" "$BAD" "$HOME_DIR"
cp "$DIST/manifest.json" "$DIST/catalog.jsonl.gz" "$GOOD/"

# битое зеркало: версия новее, а архив обрезан
sed 's/"version": "[^"]*"/"version": "9999.99.99"/' "$DIST/manifest.json" > "$BAD/manifest.json"
head -c 10000 "$DIST/catalog.jsonl.gz" > "$BAD/catalog.jsonl.gz"

(cd "$GOOD" && python3 -m http.server 8899 --bind 127.0.0.1 > /dev/null 2>&1 &)
(cd "$BAD" && python3 -m http.server 8898 --bind 127.0.0.1 > /dev/null 2>&1 &)
trap 'pkill -f "http.server 889" > /dev/null 2>&1 || true' EXIT

# ждём готовности, а не спим наугад: на медленном раннере питон стартует дольше двух секунд,
# и клиент с таймаутом в пять секунд успевает сходить в пустоту
for _ in 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15; do
    curl -fsS --max-time 2 -o /dev/null "http://127.0.0.1:8899/manifest.json" 2> /dev/null && break
    sleep 1
done
curl -fsS --max-time 2 -o /dev/null "http://127.0.0.1:8899/manifest.json" 2> /dev/null ||
    { echo "локальное зеркало не поднялось"; exit 1; }

check() { if [ "$2" = "$3" ]; then echo "ok   $1"; else echo "ПАДЕНИЕ $1: ждали [$3], получили [$2]"; fail=1; fi; }

WHEEL_HOME="$HOME_DIR" WHEEL_MIRRORS="http://127.0.0.1:8899" "$UP"
want=$(gzip -dc "$DIST/catalog.jsonl.gz" | wc -l)
check "первая загрузка" "$(wc -l < "$HOME_DIR/catalog.jsonl")" "$want"

before=$(stat -c %Y "$HOME_DIR/catalog.jsonl" 2> /dev/null || stat -f %m "$HOME_DIR/catalog.jsonl")
WHEEL_HOME="$HOME_DIR" WHEEL_MIRRORS="http://127.0.0.1:8899" "$UP"
after=$(stat -c %Y "$HOME_DIR/catalog.jsonl" 2> /dev/null || stat -f %m "$HOME_DIR/catalog.jsonl")
check "та же версия не перекачивается" "$before" "$after"

WHEEL_HOME="$HOME_DIR" WHEEL_MIRRORS="http://127.0.0.1:8898" "$UP"
check "битый архив отвергнут" "$(wc -l < "$HOME_DIR/catalog.jsonl")" "$want"

WHEEL_HOME="$HOME_DIR" WHEEL_MIRRORS="http://127.0.0.1:1" "$UP"
check "зеркала молчат, каталог цел" "$(wc -l < "$HOME_DIR/catalog.jsonl")" "$want"

rm -rf "$HOME_DIR"
WHEEL_NO_UPDATE=1 WHEEL_HOME="$HOME_DIR" WHEEL_MIRRORS="http://127.0.0.1:8899" "$UP"
check "WHEEL_NO_UPDATE=1 ничего не качает" "$([ -f "$HOME_DIR/catalog.jsonl" ] && echo есть || echo нет)" "нет"

[ "$fail" = 0 ] && echo "все проверки прошли" || exit 1
