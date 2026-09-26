"""Write a random VPS_AUTH_JWT_SECRET to a file. Does not print the secret."""
from __future__ import annotations

import argparse
import secrets
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--out",
        required=True,
        help="Destination file (chmod 600). Never commit this file.",
    )
    args = parser.parse_args()
    path = Path(args.out)
    if path.exists():
        print("refusing to overwrite existing file")
        return 1
    path.write_text(secrets.token_urlsafe(48), encoding="utf-8")
    path.chmod(0o600)
    print("wrote secret bytes to the path you supplied; copy into VPS_AUTH_JWT_SECRET manually")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
