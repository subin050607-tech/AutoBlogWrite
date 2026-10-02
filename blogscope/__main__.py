"""실행: python -m blogscope  [--port 8765] [--no-browser] [--data data]"""
import argparse
import logging

from .web import serve


def main() -> None:
    p = argparse.ArgumentParser(prog="blogscope", description="네이버 블로그 분석 도구")
    p.add_argument("--port", type=int, default=8765)
    p.add_argument("--data", default="data", help="설정·기록 저장 폴더")
    p.add_argument("--no-browser", action="store_true", help="브라우저 자동 열기 끄기")
    p.add_argument("-v", "--verbose", action="store_true")
    a = p.parse_args()
    logging.basicConfig(level=logging.DEBUG if a.verbose else logging.WARNING, format="%(asctime)s %(message)s")
    serve(a.data, a.port, not a.no_browser)


if __name__ == "__main__":
    main()
