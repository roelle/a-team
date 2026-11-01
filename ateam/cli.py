"""The one command.

    a-team                  the app: chat, board, wiki and admin
    a-team people ...       who else has a link
    a-team cli ...          run a team from a shell: --say, --daemon, --status...
    a-team lifecycle ...    export, import and retire teams
    a-team starter ...      what this machine can run, and new teams
    a-team plugin           install the agy CLI plugin
"""
import sys


def main(argv=None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    head, rest = (args[0], args[1:]) if args else ("", [])
    if head == "cli":
        from .supervisor import main as run
    elif head == "lifecycle":
        from .lifecycle import main as run
    elif head == "starter":
        from .starter import main as run
    elif head == "plugin":
        from .install_plugin import main as plugin
        return plugin()
    else:
        from .chat.server import main as app
        return app(args)
    return run(rest) or 0


if __name__ == "__main__":
    sys.exit(main())
