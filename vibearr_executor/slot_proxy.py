import os

from .proxy import Handler, Proxy


HOSTS = {
    "agent": {"chatgpt.com"},
    "setup": {"pypi.org", "files.pythonhosted.org"},
    "account": {"auth.openai.com", "chatgpt.com"},
}


def main():
    with Proxy(("0.0.0.0", 3128), Handler) as server:
        server.hosts = HOSTS[os.environ["VIBEARR_PROXY_PHASE"]]
        server.denied = set(
            filter(None, os.environ.get("VIBEARR_DENIED_PUBLIC_IPS", "").split(","))
        )
        server.serve_forever()


if __name__ == "__main__":
    main()
