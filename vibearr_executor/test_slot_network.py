import unittest
from types import SimpleNamespace
from unittest.mock import patch

from .slot_network import configure


class SlotFirewallTests(unittest.TestCase):
    def test_nft_reject_options_do_not_remove_the_terminal_deny_rule(self):
        observed = "-N VIBEARR_TASK\n-A VIBEARR_TASK -j REJECT --reject-with icmp-port-unreachable\n"
        with patch("vibearr_executor.slot_network.subprocess.run") as run:
            run.return_value.returncode = 0
            with patch(
                "vibearr_executor.slot_network.subprocess.check_output",
                return_value=observed,
            ):
                configure([])
        self.assertFalse(any("-D" in call.args[0] for call in run.call_args_list))

    def test_compact_app_uid_gets_the_same_deny_chain_as_task_uid(self):
        observed = "-N VIBEARR_TASK\n-A VIBEARR_TASK -j REJECT\n"

        def result(arguments, **kwargs):
            return SimpleNamespace(returncode=1 if "-C" in arguments else 0)

        with patch(
            "vibearr_executor.slot_network.subprocess.run", side_effect=result
        ) as run:
            with patch(
                "vibearr_executor.slot_network.subprocess.check_output",
                return_value=observed,
            ):
                configure([], task_uids=(1000, 10003))
        owner_rules = [
            call.args[0]
            for call in run.call_args_list
            if "OUTPUT" in call.args[0] and "-I" in call.args[0]
        ]
        self.assertEqual(len(owner_rules), 4)
        for uid in ("1000", "10003"):
            self.assertEqual(sum(uid in rule for rule in owner_rules), 2)

    def test_existing_uid_filters_are_not_duplicated(self):
        observed = "-N VIBEARR_TASK\n-A VIBEARR_TASK -j REJECT\n"
        with patch("vibearr_executor.slot_network.subprocess.run") as run:
            run.return_value.returncode = 0
            with patch(
                "vibearr_executor.slot_network.subprocess.check_output",
                return_value=observed,
            ):
                configure([], task_uids=(1000, 10003))
        self.assertFalse(
            any(
                "OUTPUT" in call.args[0] and "-I" in call.args[0]
                for call in run.call_args_list
            )
        )

    def test_task_uid_configuration_rejects_controller_or_unregistered_uids(self):
        for uids in ((), (0,), (10002,), (True,), ("1000",)):
            with self.subTest(uids=uids), self.assertRaises(ValueError):
                configure([], task_uids=uids)
