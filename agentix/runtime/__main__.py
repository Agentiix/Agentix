"""Entry point: python -m agentix.runtime --agent <plugin> [--dataset <plugin>]"""

import argparse

import uvicorn


def main():
    parser = argparse.ArgumentParser(description="agentix runtime server")
    parser.add_argument("--agent", required=True, help="Agent plugin path (dir with runner.py + bin/)")
    parser.add_argument("--dataset", default=None, help="Dataset plugin path (dir with dataset.py)")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--debug", action="store_true")
    parser.add_argument("--debug-port", type=int, default=5678)
    parser.add_argument("--debug-wait", action="store_true")
    args = parser.parse_args()

    from agentix.runtime.server import load_agent_plugin, load_dataset_plugin
    load_agent_plugin(args.agent)
    if args.dataset:
        load_dataset_plugin(args.dataset)

    if args.debug:
        import debugpy
        debugpy.listen(("0.0.0.0", args.debug_port))
        print(f"debugpy listening on 0.0.0.0:{args.debug_port}")
        if args.debug_wait:
            print("Waiting for debugger to attach...")
            debugpy.wait_for_client()

    uvicorn.run("agentix.runtime.server:app", host=args.host, port=args.port)


if __name__ == "__main__":
    main()
