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
# Find the newest installed incremental patch, then apply only later patches.
patches="kakaocli-sheetdesk.patch kakaocli-hide.patch kakaocli-multi-chat.patch kakaocli-draft.patch kakaocli-focus.patch kakaocli-foreground.patch kakaocli-send-button.patch kakaocli-window-lookup.patch kakaocli-open-chat.patch"
installed_patch=""
for patch in $patches; do
    if git -C vendor/kakaocli apply --reverse --check "../../$patch" 2>/dev/null; then
        installed_patch="$patch"
    fi
done
apply_next=true
if [ -n "$installed_patch" ]; then
    apply_next=false
fi
for patch in $patches; do
    if [ "$apply_next" = true ]; then
        if ! git -C vendor/kakaocli apply --check "../../$patch"; then
            echo "kakaocli 패치 적용 실패: $patch. 소스 변경 사항을 확인하세요." >&2
            exit 1
        fi
        git -C vendor/kakaocli apply "../../$patch"
    elif [ "$patch" = "$installed_patch" ]; then
        apply_next=true
    fi
done
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
