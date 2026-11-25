from .auth import token_digest

CONTROLLER_SECRET = "a9d0366e84fb43c7b4f68ef31905a1b0d3b105e467b24979be0c10d5449e8aa3"


def provision_controller(test):
    test.runner.controller_digest = token_digest(CONTROLLER_SECRET)
    test.runner.save(update_fields=["controller_digest"])
    test.runner.controller_proven = True
    test.client.defaults["HTTP_X_VIBEARR_CONTROLLER_SECRET"] = CONTROLLER_SECRET
