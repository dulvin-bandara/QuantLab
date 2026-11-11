"""Command line: ``python -m quantlab serve`` or ``python -m quantlab signal AAPL BHP.AX``."""
from __future__ import annotations

import argparse


def main() -> None:
    parser = argparse.ArgumentParser(prog="quantlab", description="QuantLab quant research platform")
    sub = parser.add_subparsers(dest="command", required=True)

    serve = sub.add_parser("serve", help="start the web app")
    serve.add_argument("--host", default="127.0.0.1", help="use 0.0.0.0 to accept outside connections")
    serve.add_argument("--port", type=int, default=8000)

    sig = sub.add_parser("signal", help="print the current signal for one or more tickers")
    sig.add_argument("symbols", nargs="+")

    args = parser.parse_args()
    if args.command == "serve":
        import uvicorn

        from .config import settings

        if args.host not in ("127.0.0.1", "localhost") and not settings.password:
            print("WARNING: listening beyond localhost with no QUANTLAB_PASSWORD set; anyone who can reach "
                  "this port can use the app.")
        uvicorn.run("quantlab.api:app", host=args.host, port=args.port, proxy_headers=True)
    else:
        from .signals import compute_signal

        for symbol in args.symbols:
            s = compute_signal(symbol)
            print(f"\n{s['symbol']}  {s['price']:.2f} {s['currency']}  ({s['as_of']})  ->  {s['label']}  [{s['score']:+.2f}]")
            for name, c in s["components"].items():
                print(f"  {name:<9}{c['score']:+.2f}  x{c['weight']:.2f}  {c['note']}")
            p = s["plan"]
            print(f"  plan     stop {p['stop']:.2f}, target {p['target']:.2f}, size {p['shares']} shares "
                  f"(risking {p['risk_amount']:,.0f})")


if __name__ == "__main__":
    main()
