#!/bin/sh
set -eu
cd "$(dirname "$0")"
if [ ! -f vendor/kakaocli/Package.swift ]; then
    if [ -d vendor/kakaocli ] && [ -n "$(ls -A vendor/kakaocli)" ]; then
        echo "vendor/kakaocli에 불완전한 소스가 있습니다. 내용을 확인한 뒤 다시 설치하세요." >&2
        exit 1
    fi
    git clone https://github.com/silver-flight-group/kakaocli.git vendor/kakaocli
    git -C vendor/kakaocli checkout 8b6ffcfdaebc592a735dc1a8bd5e50037e626406
fi
if git -C vendor/kakaocli apply --reverse --check ../../kakaocli-hide.patch 2>/dev/null; then
    : # Both patches are already installed.
elif git -C vendor/kakaocli apply --check ../../kakaocli-sheetdesk.patch 2>/dev/null; then
    git -C vendor/kakaocli apply ../../kakaocli-sheetdesk.patch
elif ! git -C vendor/kakaocli apply --reverse --check ../../kakaocli-sheetdesk.patch 2>/dev/null; then
    echo "kakaocli 패치를 적용할 수 없습니다. 소스 버전과 변경 사항을 확인하세요." >&2
    exit 1
fi
if git -C vendor/kakaocli apply --check ../../kakaocli-hide.patch 2>/dev/null; then
    git -C vendor/kakaocli apply ../../kakaocli-hide.patch
elif ! git -C vendor/kakaocli apply --reverse --check ../../kakaocli-hide.patch 2>/dev/null; then
    echo "kakaocli 숨김 패치를 적용할 수 없습니다. 소스 변경 사항을 확인하세요." >&2
    exit 1
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
if [ ! -x .build/release/kakaocli ]; then
    echo "kakaocli 실행 파일이 생성되지 않았습니다. 위 빌드 오류를 확인하세요." >&2
    exit 1
fi
cd ../..
if ! ./run.sh --doctor; then
    ./recover-account.sh
    ./run.sh --doctor
fi
printf '\n준비 완료. ./run.sh --demo 로 샘플 화면을 확인하세요.\n'
