"""One-time numeric ID recovery for the user's own installed KakaoTalk account.
Only the numeric ID and its existing account hash are stored, mode 0600.
"""
import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import plistlib
import re
import subprocess
import sys


def main():
    parser = argparse.ArgumentParser(description="최초 1회 로컬 계정 번호 확인 (대화/비밀번호/키 저장 없음)")
    parser.add_argument("--threads", type=int, default=4)
    parser.add_argument("--max-id", type=int, default=1000000000)
    args = parser.parse_args()
    if not 1 <= args.threads <= 16 or not 1 <= args.max_id <= 10000000000:
        parser.error("threads는 1~16, max-id는 1~100억 범위여야 합니다.")
    root = Path(__file__).resolve().parent.parent
    base = Path.home() / "Library/Containers/com.kakao.KakaoTalkMac/Data/Library"
    prefs = base / "Preferences"
    db_dir = base / "Application Support/com.kakao.KakaoTalkMac"
    hashes = []
    try:
        # Same preference ordering as DeviceInfo.containerPreferencesPath.
        paths = list(prefs.glob("com.kakao.KakaoTalkMac.*.plist"))
        paths += [prefs / "com.kakao.KakaoTalkMac.plist", Path.home() / "Library/Preferences/com.kakao.KakaoTalkMac.plist"]
        for path in paths:
            if not path.exists():
                continue
            with path.open("rb") as stream:
                data = plistlib.load(stream)
            for key, val in data.items():
                if key.startswith("DESIGNATEDFRIENDSREVISION:") and val:
                    value = key.split(":", 1)[1]
                    if re.fullmatch("[0-9a-f]{128}", value) and value != hashlib.sha512(b"0").hexdigest() and value not in hashes:
                        hashes.append(value)
        if not hashes:
            raise RuntimeError("계정 메타데이터를 찾지 못했습니다. 카톡에 로그인하고 터미널의 전체 디스크 접근 권한을 확인하세요.")
        output = subprocess.run(["/usr/sbin/ioreg", "-rd1", "-c", "IOPlatformExpertDevice"], capture_output=True, text=True, timeout=5)
        uuid = re.search(r'"IOPlatformUUID" = "([^"]+)"', output.stdout).group(1)
        hashed_uuid = base64.b64encode(hashlib.sha1(uuid.encode()).digest() + hashlib.sha256(uuid.encode()).digest()).decode()
        for account_hash in hashes:
            print("본인 기기의 계정 번호를 확인합니다. 최초 1회 수 분 걸릴 수 있습니다.", flush=True)
            # Only the C child receives the hash. Never include it in process argv.
            result = subprocess.run([str(root / "tools/recover-user"), str(args.threads), str(args.max_id)],
                                    input=account_hash + "\n", stdout=subprocess.PIPE, text=True)
            if result.returncode:
                continue
            uid = int(result.stdout.strip())
            password = ".".join([".", "F", str(uid), "A", "F", uuid[::-1], ".", "|"])
            db_name = hashlib.pbkdf2_hmac("sha256", password.encode(), hashed_uuid[::-1].encode(), 100000, 128).hex()[28:106]
            if not (db_dir / db_name).exists() and not (db_dir / (db_name + ".db")).exists():
                continue
            state = root / ".state"
            state.mkdir(mode=0o700, exist_ok=True)
            state.chmod(0o700)
            path = state / "account.json"
            fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w") as stream:
                json.dump({"user_id": uid, "account_hash": account_hash}, stream)
            path.chmod(0o600)
            print("계정 확인 완료. 대화·비밀번호·DB 키는 저장하지 않았습니다.")
            return 0
        print("계정 번호를 찾지 못했습니다. 카톡 계정/DB 호환성을 확인해야 합니다.", file=sys.stderr)
        return 1
    except (OSError, ValueError, AttributeError, RuntimeError, subprocess.SubprocessError) as error:
        # Do not print metadata values, hashes, keys, or file contents on failure.
        print("계정 확인 실패: {}. 권한 또는 카톡 버전을 확인하세요.".format(type(error).__name__), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
