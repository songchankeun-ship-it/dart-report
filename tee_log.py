"""stdin 을 화면과 로그파일에 동시에 씀 (윈도우에 tee 가 없어서)."""
import sys
path = sys.argv[1]
with open(path, "a", encoding="utf-8", errors="replace") as f:
    for line in sys.stdin:
        sys.stdout.write(line); sys.stdout.flush()
        f.write(line); f.flush()
