import os

from .proxy import Handler, Proxy


def main():
    hosts = {
        "agent": {"api.openai.com"},
        "setup": {"pypi.org", "files.pythonhosted.org"},
    }
    with Proxy(("0.0.0.0", 3128), Handler) as server:
        server.hosts = hosts[os.environ["VIBEARR_PROXY_PHASE"]]
        server.denied = set(
            filter(None, os.environ.get("VIBEARR_DENIED_PUBLIC_IPS", "").split(","))
        )
        server.serve_forever()


if __name__ == "__main__":
    main()
