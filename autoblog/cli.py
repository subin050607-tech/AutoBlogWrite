"""autoblog 명령행 인터페이스."""
from __future__ import annotations

import argparse
import logging
import shutil
import sys
from pathlib import Path

from .config import load_config
from .db import DB
from .generator import make_generator
from .pipeline import process_topic
from .publishers import FilePublisher, NaverPublisher, make_publisher
from .scheduler import Scheduler


def _topic_meta(args) -> dict:
    meta = {}
    for key in ("category", "tone", "notes"):
        v = getattr(args, key, None)
        if v:
            meta[key] = v
    if getattr(args, "images", None):
        meta["images"] = [s.strip() for s in args.images.split(",") if s.strip()]
    return meta


def cmd_init(args, cfg):
    for src, dst in (("config.example.yaml", "config.yaml"), (".env.example", ".env")):
        if Path(dst).exists():
            print(f"이미 있음: {dst}")
        elif Path(src).exists():
            shutil.copy(src, dst)
            print(f"생성: {dst}")
        else:
            print(f"템플릿 없음: {src}")
    print("config.yaml 과 .env 를 편집한 뒤 `autoblog check` 로 연결을 확인하세요.")


def cmd_add(args, cfg, db: DB):
    tid = db.add_topic(args.keyword, _topic_meta(args), args.priority)
    print(f"등록: #{tid} {args.keyword}" if tid else "이미 등록된 키워드이거나 비어 있습니다.")


def cmd_add_file(args, cfg, db: DB):
    n = 0
    for line in Path(args.path).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and db.add_topic(line):
            n += 1
    print(f"{n}개 등록")


def cmd_suggest(args, cfg, db: DB):
    gen = make_generator(cfg)
    items = gen.suggest_topics(args.theme, args.n)
    for k in items:
        print(("+ " if args.save and db.add_topic(k) else "  ") + k)
    if not args.save:
        print("\n(--save 를 주면 큐에 등록합니다)")


def cmd_list(args, cfg, db: DB):
    for t in db.list_topics(args.status):
        err = f"  ! {t['last_error']}" if t["last_error"] else ""
        print(f"#{t['id']:<4} [{t['status']:<7}] p{t['priority']} {t['keyword']}{err}")


def cmd_history(args, cfg, db: DB):
    for p in db.list_posts(args.limit):
        print(f"{p['published_at']} [{p['mode']}] {p['title']}  {p['url'] or p['file_path']}")


def _pick(args, db: DB):
    t = db.get_topic(args.id) if getattr(args, "id", None) else db.next_topic()
    if not t:
        sys.exit("처리할 주제가 없습니다. `autoblog add` 로 먼저 등록하세요.")
    return t


def cmd_preview(args, cfg, db: DB):
    """실제 발행 없이 HTML 파일로만 저장해 결과물을 검수한다."""
    cfg["blog"]["publisher"] = "file"
    t = _pick(args, db)
    res = process_topic(t, cfg, db, make_generator(cfg), FilePublisher(cfg), record=False)
    print(f"저장됨: {res.file_path}")


def cmd_post(args, cfg, db: DB):
    t = _pick(args, db)
    pub = make_publisher(cfg)
    res = process_topic(t, cfg, db, make_generator(cfg), pub)
    print(f"완료: {res.url or res.file_path}")


def cmd_run(args, cfg, db: DB):
    Scheduler(cfg, db, make_generator(cfg), make_publisher(cfg)).run_forever()


def cmd_check(args, cfg, db: DB):
    pub = NaverPublisher(cfg)
    try:
        pub.check_connection()
    except Exception as e:
        sys.exit(f"네이버 연결 실패: {e}\n- 블로그 ID / API 연동 비밀번호 / 글 API 사용 설정을 확인하세요.")
    print("네이버 블로그 API 연결 OK")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="autoblog", description="네이버 블로그 자동 작성·발행 도구")
    p.add_argument("-c", "--config", default="config.yaml")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("init", help="config.yaml/.env 템플릿 생성").set_defaults(fn=cmd_init, nodb=True)

    a = sub.add_parser("add", help="주제(키워드) 등록")
    a.add_argument("keyword")
    a.add_argument("--category"); a.add_argument("--tone"); a.add_argument("--notes")
    a.add_argument("--images", help="쉼표로 구분한 이미지 파일명")
    a.add_argument("--priority", type=int, default=0)
    a.set_defaults(fn=cmd_add)

    a = sub.add_parser("add-file", help="텍스트 파일(한 줄에 키워드 하나)로 일괄 등록")
    a.add_argument("path"); a.set_defaults(fn=cmd_add_file)

    a = sub.add_parser("suggest", help="LLM으로 주제 후보 추천")
    a.add_argument("theme"); a.add_argument("-n", type=int, default=10)
    a.add_argument("--save", action="store_true"); a.set_defaults(fn=cmd_suggest)

    a = sub.add_parser("list", help="주제 큐 보기")
    a.add_argument("--status", choices=["pending", "done", "failed", "skipped"]); a.set_defaults(fn=cmd_list)

    a = sub.add_parser("history", help="발행 이력")
    a.add_argument("--limit", type=int, default=20); a.set_defaults(fn=cmd_history)

    a = sub.add_parser("preview", help="발행 없이 HTML 로 미리보기")
    a.add_argument("--id", type=int); a.set_defaults(fn=cmd_preview)

    a = sub.add_parser("post", help="주제 1건 즉시 발행(스케줄 무시)")
    a.add_argument("--id", type=int); a.set_defaults(fn=cmd_post)

    sub.add_parser("run", help="스케줄러 상시 실행").set_defaults(fn=cmd_run)
    sub.add_parser("check", help="네이버 API 연결 확인").set_defaults(fn=cmd_check)
    return p


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    cfg = load_config(args.config)
    try:
        if getattr(args, "nodb", False):
            args.fn(args, cfg)
        else:
            args.fn(args, cfg, DB(cfg["paths"]["db"]))
    except KeyboardInterrupt:
        print("\n중단됨")
    except Exception as e:
        sys.exit(f"오류: {e}")


if __name__ == "__main__":
    main()
