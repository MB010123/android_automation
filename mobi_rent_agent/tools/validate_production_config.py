"""Validate production .env and farm maps without sending SMS or touching phones.

  python tools/validate_production_config.py
  python tools/validate_production_config.py --env /opt/mobi-rent-agent/.env
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from infrastructure.production_validate import validate_production_config


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env", default=str(ROOT / ".env"), help="Path to .env file")
    parser.add_argument("--root", type=Path, default=ROOT, help="Project root for relative paths")
    parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON")
    parser.add_argument(
        "--role",
        choices=("farm", "vps"),
        default=None,
        help="Validate for physical PC (farm) or VPS backend",
    )
    args = parser.parse_args()

    env_path = Path(args.env)
    if not env_path.exists():
        print(f"No .env at {env_path}; validating process environment only.", file=sys.stderr)
        env_file: str | None = None
    else:
        env_file = str(env_path)

    report = validate_production_config(
        env_file=env_file,
        project_root=args.root,
        role=args.role,
    )

    if args.json:
        print(
            json.dumps(
                {
                    "ok": report.ok,
                    "app_name": report.app_name,
                    "issues": [
                        {"level": i.level, "code": i.code, "message": i.message} for i in report.issues
                    ],
                },
                indent=2,
            )
        )
    else:
        print(f"Production validation: {'PASS' if report.ok else 'FAIL'} ({report.app_name})")
        for issue in report.issues:
            print(f"  [{issue.level.upper()}] {issue.code}: {issue.message}")

    return 0 if report.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
