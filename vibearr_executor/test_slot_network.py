import unittest
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
