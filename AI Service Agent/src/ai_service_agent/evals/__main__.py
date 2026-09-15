import argparse
import json
from pathlib import Path

from ai_service_agent.evals import runner


def main() -> None:
    parser = argparse.ArgumentParser(description="P2 离线确定性评估（AGENTS.md §16）")
    parser.add_argument("--data", default="tests/evals/control", help="control 数据集目录（含 master.jsonl）")
    parser.add_argument("--out", default=None, help="JSON 报告输出路径（缺省打印 stdout）")
    args = parser.parse_args()
    report = runner.run_all(args.data)
    text = json.dumps(report, ensure_ascii=False, indent=2)
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
    else:
        print(text)


if __name__ == "__main__":
    main()
