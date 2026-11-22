"""Command line entry point:  python -m app.cli <command> [options]

  check                                      free check of keys, models, prices, voices
  migrate                                    apply database migrations (once per deploy)
  voice-previews [--force]                   record each voice's Settings sample (once)
  research    --topic KEY [--force]          step 2 for one topic
  clips       --story ID [--tone/--language/--voice]   step 3 (scripts + audio) for one story
  assemble    --user ID [--dry-run]          steps 4-6 for one user (dry run: transcript only)
  run-daily   [--immediate]                  plan the day and run the worker
  costs       [--days 7]                     cost by stage / per episode / per active user
  demo        [--email .. --name .. ...]     dev: demo listeners through the real pipeline
  check-episodes [--episode ID] [--days 1]   audit stored episodes: format + accuracy rules
  export-demo [--out DIR]                    dev: the demo as static files (static site)

Global options: --mock (offline LLM + TTS), --db URL, -v
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path
from typing import Any

from sqlalchemy.engine import Engine
from sqlmodel import Session, select

from app.config import get_settings
from app.database.db import create_db_and_tables, make_engine
from app.database.models import Story, Topic
from app.jobs.scheduler import combos_by_topic, plan_daily, register_handlers
from app.jobs.worker import run_worker
from app.pipeline.step1_topics import seed_subtopics, seed_topics
from app.pipeline.step2_research import research_topic
from app.pipeline.step3_scripts import ensure_clips
from app.pipeline.step4_assemble import (
    EpisodePlan,
    assemble_episode,
    episode_sources,
    episode_transcript,
)
from app.providers import Providers, build_providers
from app.providers.voices import record_previews, seed_voices
from app.tracking.costs import cost_report


def setup(args: argparse.Namespace) -> tuple[Engine, Providers]:
    engine = make_engine(args.db or get_settings().database_url)
    create_db_and_tables(engine)
    register_handlers()
    providers = build_providers(mock=args.mock)
    with Session(engine) as session:
        seed_topics(session, providers.embedder)
        seed_subtopics(session, providers.embedder)
        seed_voices(session)
    return engine, providers


def cmd_research(args: argparse.Namespace) -> None:
    engine, providers = setup(args)
    with Session(engine) as session:
        topic = session.exec(select(Topic).where(Topic.key == args.topic)).first()
        if topic is None:
            sys.exit(f"Unknown topic {args.topic!r}")
        result = research_topic(session, topic, providers, force=args.force,
                                language=args.language)
        print(json.dumps(result.__dict__, indent=2))
        for sid in result.new_story_ids:
            s = session.get(Story, sid)
            print(f"  [{s.importance_score}] {s.headline} ({s.source_name}) {s.canonical_url}")  # type: ignore[union-attr]
            for o in s.sources:  # type: ignore[union-attr]
                print(f"        also: {o['name']} {o['url']}")
            for b in s.background:  # type: ignore[union-attr]
                print(f"        background: {b['term']} -> {b['title']} ({b['language']})")


def cmd_clips(args: argparse.Namespace) -> None:
    engine, providers = setup(args)
    with Session(engine) as session:
        story = session.get(Story, args.story)
        if story is None:
            sys.exit(f"Unknown story {args.story}")
        if args.tone or args.language or args.voice:
            combos = {(args.tone or "conversational", args.language or "en", args.voice or "aria")}
        else:
            combos = combos_by_topic(session, story.first_seen).get(story.topic_id) or {
                ("conversational", "en", "aria")}
        clips = ensure_clips(session, [(story, *c) for c in sorted(combos)], providers)
        for c in clips:
            print(f"\n[{c.tone}/{c.language}/{c.voice}] verified={c.verified} "
                  f"duration={c.duration_sec}s cost=${c.cost_estimate:.4f} {c.audio_path or ''}")
            print(c.script)
            if c.verification_notes:
                print("  notes:", c.verification_notes)


def cmd_assemble(args: argparse.Namespace) -> None:
    engine, providers = setup(args)
    with Session(engine) as session:
        result = assemble_episode(session, args.user, providers, dry_run=args.dry_run)
        if result is None:
            print("No unseen stories for this user; no episode generated.")
            return
        if isinstance(result, EpisodePlan):
            print(f"DRY RUN: {len(result.items)} stories, ~{result.est_seconds:.0f}s\n")
            print(result.transcript)
            return
        print(f"Episode {result.id}: {result.title} ({result.duration_seconds}s) "
              f"-> {result.audio_path}\n")
        print(episode_transcript(session, result))
        print("\nSources:")
        for s in episode_sources(session, result):
            print(f"  - {s['headline']} ({s['source_name']}) {s['url']}")


def cmd_run_daily(args: argparse.Namespace) -> None:
    engine, providers = setup(args)
    with Session(engine) as session:
        summary = plan_daily(session, run_immediately=args.immediate)
    print("Plan:", summary)
    stats = run_worker(engine, providers, stop_when_empty=True)
    print("Worker:", stats)
    with Session(engine) as session:
        print_cost_report(cost_report(session, days=1))


def cmd_costs(args: argparse.Namespace) -> None:
    engine, _ = setup(args)
    with Session(engine) as session:
        print_cost_report(cost_report(session, args.days))


def print_cost_report(report: dict[str, Any]) -> None:
    print(f"\nCosts over the last {report['days']} day(s)")
    print(f"{'stage':<15}{'events':>8}{'cost $':>11}{'in tok':>12}{'out tok':>10}"
          f"{'search':>8}{'tts chars':>11}")
    for stage, v in sorted(report["by_stage"].items(), key=lambda kv: -kv[1]["cost_usd"]):
        print(f"{stage:<15}{v['events']:>8}{v['cost_usd']:>11.4f}{v['input_tokens'] or 0:>12}"
              f"{v['output_tokens'] or 0:>10}{v['searches'] or 0:>8}{v['tts_chars'] or 0:>11}")
    print(f"{'TOTAL':<15}{'':>8}{report['total_usd']:>11.4f}")
    print(f"episodes: {report['episodes']}   active users: {report['active_users']}")
    print(f"cost per episode: ${report['per_episode_usd']}   per active user: "
          f"${report['per_active_user_usd']}")
    print(f"  shared (research + clips): ${report['shared_usd']}   new spoken phrases "
          f"(greeting/transitions/outro): ${report['per_user_marginal_usd']} per episode")


def cmd_maintenance(args: argparse.Namespace) -> None:
    from app.jobs.maintenance import run_maintenance

    engine, _ = setup(args)
    with Session(engine) as session:
        print(run_maintenance(session))


def cmd_voice_previews(args: argparse.Namespace) -> None:
    engine, providers = setup(args)
    with Session(engine) as session:
        done = record_previews(session, providers.tts, force=args.force)
    print(f"Recorded {len(done)} preview(s) with {providers.tts.name}: {', '.join(done) or '-'}")


def cmd_set_plan(args: argparse.Namespace) -> None:
    from app.database.models import User
    from app.plans import set_plan

    with Session(make_engine(get_settings().database_url)) as session:
        user = session.get(User, args.user_id)
        if user is None:
            sys.exit(f"no user {args.user_id}")
        set_plan(session, user, args.plan, "cli")
        print(f"user {user.id} is now on {user.plan}")


def cmd_sounds(args: argparse.Namespace) -> None:
    from app.providers.sounds import generate_sounds

    written = generate_sounds(force=args.force)
    print("generated: " + (", ".join(written) or "nothing (all exist; --force to redo)"))


def _print_run(title: str, r: dict) -> None:  # type: ignore[type-arg]
    print(f"\n{title} in {r['minutes']} min, store {r['store']} ({r['users_in_store']} users)")
    for line in r["worker"]:
        print(" ", line)
    for d in r["dead_jobs"]:
        print("  DEAD", d[:200])
    for u in r["listeners"]:
        print(f"  {u['name']:<6} {u['email']:<20} {u['plan']:<5} {u['language']}")
        for ep in u["episodes"]:
            print(f"      {ep['topic']:<12} {ep['status']:<10} {ep['minutes']} min  {ep['file']}")
    print("Cost:", r["cost_usd"], "total $", r["total_usd"])


def cmd_demo(args: argparse.Namespace) -> None:
    from app.dev.demo import run_demo

    one = None
    if args.email:
        topics = [t.strip() for t in args.topics.split(",") if t.strip()]
        one = [{"name": args.name or args.email.split("@")[0].capitalize(), "email": args.email,
                "language": args.language, "plan": args.plan, "voice": args.voice,
                "minutes": args.minutes, "topics": {t: [] for t in topics}}]
    r = run_demo(one, rebuild=args.rebuild)
    _print_run("Demo", r)
    if "feed" in r:
        print("Feed:", r["feed"])
    if r.get("transcript"):
        print("\n" + r["transcript"] + "\n\nSources:\n" + r["sources"])


def cmd_check_episodes(args: argparse.Namespace) -> None:
    from datetime import timedelta

    from sqlmodel import col

    from app.database.models import Episode, utcnow
    from app.pipeline.review import audit_episode

    engine = make_engine(args.db or get_settings().database_url)
    failed = 0
    with Session(engine) as session:
        query = select(Episode).where(Episode.status == "ready")
        query = (query.where(Episode.id == args.episode) if args.episode else
                 query.where(col(Episode.created_at) >= utcnow() - timedelta(days=args.days)))
        episodes = session.exec(query.order_by(col(Episode.id))).all()
        for ep in episodes:
            problems = audit_episode(session, ep)
            q = ep.quality or {}
            found = ", ".join(f"{i['type']} {i['segment']}" for i in q.get("issues", []))
            print(f"{'OK  ' if not problems else 'FAIL'} episode {ep.id} user {ep.user_id} "
                  f"{ep.topic:<12} review: {len(q.get('issues', []))} issue(s) fixed"
                  f"{' (' + found + ')' if found else ''}")
            for p in problems:
                print("       -", p)
            failed += bool(problems)
    print(f"\n{len(episodes) - failed}/{len(episodes)} episodes pass every rule.")
    if failed:
        sys.exit(1)


def cmd_export_demo(args: argparse.Namespace) -> None:
    from app.dev.static_demo import DEFAULT_OUT, export_demo

    r = export_demo(Path(args.out) if args.out else DEFAULT_OUT)
    print(f"Static demo in {r['out']} ({r['megabytes']} MB, {r['audio_files']} audio files)")
    for u in r["listeners"]:
        print(f"  {u['name']:<6} {u['email']:<20} {u['language']}  {u['episodes']} episodes")
    for m in r["missing_audio"]:
        print("  MISSING audio:", m)


def cmd_migrate(args: argparse.Namespace) -> None:
    from app.database.migrate import migrate

    print("Database", migrate(make_engine(args.db or get_settings().database_url)))


def cmd_check(args: argparse.Namespace) -> None:
    from app.providers.check import run_checks

    results = run_checks()
    for c in results:
        print(f"{'OK  ' if c.ok else 'FAIL'}  {c.name:<34} {c.detail}")
    failed = [c for c in results if not c.ok]
    print(f"\n{len(results) - len(failed)}/{len(results)} checks passed.")
    if failed:
        sys.exit(1)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m app.cli", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--mock", action="store_true", help="use offline mock LLM and TTS")
    parser.add_argument("--db", help="database URL (default: DATABASE_URL)")
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("voice-previews", help="record the Settings sample of each voice")
    p.add_argument("--force", action="store_true", help="record again even if current")
    p.set_defaults(func=cmd_voice_previews)

    p = sub.add_parser("set-plan", help="move a user to a plan (config/plans.yaml)")
    p.add_argument("user_id", type=int)
    p.add_argument("plan")
    p.set_defaults(func=cmd_set_plan)

    p = sub.add_parser("sounds", help="generate intro/transition/outro sounds (ElevenLabs, once)")
    p.add_argument("--force", action="store_true", help="regenerate existing sounds")
    p.set_defaults(func=cmd_sounds)

    p = sub.add_parser("demo", help="dev: demo listeners (en/es) through the real pipeline")
    p.add_argument("--email", help="one listener instead of the demo ones")
    p.add_argument("--rebuild", action="store_true",
                   help="delete today's episodes of these listeners and build them again")
    p.add_argument("--name", default="")
    p.add_argument("--language", default="es")
    p.add_argument("--plan", default="free")
    p.add_argument("--topics", default="technology,world")  # free plan: 2 topics
    p.add_argument("--voice", default="aria")
    p.add_argument("--minutes", type=int, default=5)
    p.set_defaults(func=cmd_demo)

    p = sub.add_parser("check-episodes", help="audit stored episodes: format + accuracy rules")
    p.add_argument("--episode", type=int, help="one episode (default: the last --days)")
    p.add_argument("--days", type=int, default=1)
    p.set_defaults(func=cmd_check_episodes)

    p = sub.add_parser("export-demo", help="dev: the demo listeners as static files")
    p.add_argument("--out", help="folder (default: frontend/demo-data)")
    p.set_defaults(func=cmd_export_demo)

    p = sub.add_parser("migrate", help="apply database migrations (run once per deploy)")
    p.set_defaults(func=cmd_migrate)

    p = sub.add_parser("check", help="verify API keys, models, prices and voices (free)")
    p.set_defaults(func=cmd_check)

    p = sub.add_parser("research", help="step 2: research one topic")
    p.add_argument("--topic", required=True)
    p.add_argument("--force", action="store_true", help="run even with no active subscribers")
    p.add_argument("--language", default="en",
                   help="research language: en, or one of research.languages (local outlets)")
    p.set_defaults(func=cmd_research)

    p = sub.add_parser("clips", help="step 3: scripts + audio for one story")
    p.add_argument("--story", type=int, required=True)
    p.add_argument("--tone")
    p.add_argument("--language")
    p.add_argument("--voice")
    p.set_defaults(func=cmd_clips)

    p = sub.add_parser("assemble", help="steps 4-6: assemble one user's episode")
    p.add_argument("--user", type=int, required=True)
    p.add_argument("--dry-run", action="store_true", help="stop before TTS and stitching")
    p.set_defaults(func=cmd_assemble)

    p = sub.add_parser("run-daily", help="plan the day and process due jobs")
    p.add_argument("--immediate", action="store_true",
                   help="run assemble jobs now instead of at each user's delivery time")
    p.set_defaults(func=cmd_run_daily)

    p = sub.add_parser("maintenance", help="summarize costs, archive and delete old rows")
    p.set_defaults(func=cmd_maintenance)

    p = sub.add_parser("costs", help="cost report")
    p.add_argument("--days", type=int, default=7)
    p.set_defaults(func=cmd_costs)

    return parser


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    for noisy in ("httpx", "httpcore", "anthropic", "openai"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    args.func(args)


if __name__ == "__main__":
    main()
