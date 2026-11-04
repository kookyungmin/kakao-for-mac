#!/bin/sh
set -eu
cd "$(dirname "$0")"
if [ ! -d vendor/kakaocli ]; then
    git clone https://github.com/silver-flight-group/kakaocli.git vendor/kakaocli
    git -C vendor/kakaocli checkout 8b6ffcfdaebc592a735dc1a8bd5e50037e626406
    git -C vendor/kakaocli apply ../../kakaocli-sheetdesk.patch
fi
if ! command -v brew >/dev/null 2>&1; then
    echo "Homebrew와 Xcode Command Line Tools가 필요합니다."
    exit 1
fi
if ! brew list sqlcipher >/dev/null 2>&1; then
    brew install sqlcipher
fi
cd vendor/kakaocli
swift build -c release
cd ../..
if ! ./run.sh --doctor; then
    ./recover-account.sh
    ./run.sh --doctor
fi
printf '\n준비 완료. ./run.sh --demo 로 샘플 화면을 확인하세요.\n'
