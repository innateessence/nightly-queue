from django.core.exceptions import ValidationError
from django.db.models.deletion import ProtectedError
from django.test import TestCase
from django.urls import reverse

from .configuration import configuration_for_settings
from .settings_models import RunnerSettings, TokenBudget
from .test_multiple_runners import form_data


class TokenBudgetTests(TestCase):
    def budget(self, **changes):
        return TokenBudget.objects.create(name="Keep weekly reserve", **changes)

    def test_navigation_places_budgets_after_schedules(self):
        response = self.client.get(reverse("token_budget_list"))
        self.assertContains(response, 'href="/token-budgets/" aria-current="page"')
        content = response.content.decode()
        self.assertLess(
            content.index('href="/schedules/"'), content.index('href="/token-budgets/"')
        )
        self.assertContains(response, "No token budgets yet")

    def test_create_and_edit_reusable_budget(self):
        response = self.client.post(
            reverse("token_budget_add"),
            {
                "name": "Weekly reserve",
                "weekly_remaining_percent": 40,
                "five_hour_remaining_percent": 0,
            },
        )
        budget = TokenBudget.objects.get(name="Weekly reserve")
        self.assertRedirects(response, reverse("token_budget_edit", args=[budget.pk]))
        self.assertEqual(budget.weekly_remaining_percent, 40)
        self.assertEqual(budget.five_hour_remaining_percent, 0)
        self.client.post(
            reverse("token_budget_edit", args=[budget.pk]),
            {
                "name": "Weekly reserve",
                "weekly_remaining_percent": 20,
                "five_hour_remaining_percent": 10,
            },
        )
        budget.refresh_from_db()
        self.assertEqual(
            (budget.weekly_remaining_percent, budget.five_hour_remaining_percent),
            (20, 10),
        )

    def test_percentages_must_be_between_zero_and_one_hundred(self):
        for field in ("weekly_remaining_percent", "five_hour_remaining_percent"):
            for value in (-1, 101):
                with (
                    self.subTest(field=field, value=value),
                    self.assertRaises(ValidationError),
                ):
                    self.budget(**{field: value})

    def test_runner_selects_budget_and_lists_shared_assignments(self):
        budget = self.budget()
        for name in ("First", "Second"):
            response = self.client.post(
                reverse("coding_runner_add"),
                form_data(
                    RunnerSettings(name=name),
                    token_budget=budget.pk,
                ),
            )
            self.assertEqual(response.status_code, 302)
            self.assertEqual(RunnerSettings.objects.get(name=name).token_budget, budget)
        response = self.client.get(reverse("token_budget_list"))
        self.assertContains(response, "First")
        self.assertContains(response, "Second")
        self.assertContains(response, "40%")
        self.assertContains(response, "0%")
        self.assertContains(
            self.client.get(reverse("token_budget_edit", args=[budget.pk])), "First"
        )
        with self.assertRaises(ProtectedError):
            budget.delete()

    def test_shared_edit_changes_only_assigned_configuration_revisions(self):
        budget = self.budget()
        first = RunnerSettings.objects.create(name="First", token_budget=budget)
        second = RunnerSettings.objects.create(name="Second", token_budget=budget)
        other = RunnerSettings.objects.create(name="Other")
        before = [
            configuration_for_settings(runner) for runner in (first, second, other)
        ]
        self.assertTrue(before[0]["global"]["reserve_enabled"])
        budget.weekly_remaining_percent = 55
        budget.save()
        after = [
            configuration_for_settings(runner) for runner in (first, second, other)
        ]
        for index in (0, 1):
            self.assertNotEqual(before[index]["revision"], after[index]["revision"])
            self.assertEqual(
                after[index]["global"]["token_budget"]["weekly_remaining_percent"], 55
            )
        self.assertEqual(before[2]["revision"], after[2]["revision"])

    def test_budget_assignment_can_be_cleared(self):
        budget = self.budget()
        runner = RunnerSettings.objects.create(name="Assigned", token_budget=budget)
        response = self.client.post(
            reverse("coding_runner_edit", args=[runner.pk]),
            form_data(runner, token_budget=""),
        )
        self.assertEqual(response.status_code, 302)
        runner.refresh_from_db()
        self.assertIsNone(runner.token_budget_id)
        self.assertFalse(
            configuration_for_settings(runner)["global"]["reserve_enabled"]
        )
