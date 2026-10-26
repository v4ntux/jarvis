"""python -m jarvis4.exam --agent jarvis3 [-k forward] [--no-judge] [--max-cost 0.5]"""
import argparse
import asyncio
import os
import sys

from . import runner
from .scenario import load_all

REPORTS = os.path.join(os.path.dirname(__file__), "reports")


def main() -> int:
    parser = argparse.ArgumentParser(prog="python -m jarvis4.exam", description="Экзамен Джарвиса")
    parser.add_argument("--agent", default="scripted:reference",
                        help="scripted:reference | scripted:silent | jarvis3")
    parser.add_argument("-k", default="", help="только сценарии, в id или имени файла которых есть эта строка")
    parser.add_argument("--no-judge", action="store_true", help="без судьи-модели: только проверки кодом, бесплатно")
    parser.add_argument("--max-cost", type=float, default=0.5, help="остановиться, когда потрачено столько $")
    parser.add_argument("--timeout", type=float, default=None, help="секунд на событие (по умолчанию из сценария)")
    parser.add_argument("--out", default=REPORTS, help="куда писать отчёт")
    args = parser.parse_args()
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    scenarios = load_all(only=args.k)
    if not scenarios:
        print("Нет таких сценариев.")
        return 2
    judge = not args.no_judge and not args.agent.startswith("scripted:")
    results = asyncio.run(runner.run_all(scenarios, args.agent, judge, args.max_cost, args.timeout))
    print("\n" + runner.summary(results))
    print("Отчёт:", runner.write_report(results, args.agent, args.out))
    return 0 if all(r.status in ("pass", "skip") for r in results) else 1


if __name__ == "__main__":
    sys.exit(main())
