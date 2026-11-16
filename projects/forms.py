from django import forms

from .models import Project


class ProjectForm(forms.ModelForm):
    def clean_permitted_author_ids(self):
        value = self.cleaned_data["permitted_author_ids"]
        return [] if value is None else value

    class Meta:
        model = Project
        fields = [
            "name",
            "repository_url",
            "base_branch",
            "credential",
            "autonomous",
            "permitted_author_ids",
            "backfill_issues",
        ]
        labels = {
            "repository_url": "GitHub repository URL",
            "permitted_author_ids": "Permitted GitHub author IDs",
        }
        help_texts = {
            "repository_url": "Use an HTTPS URL, such as https://github.com/owner/repo.",
            "base_branch": "Leave blank to use the repository's default branch.",
            "credential": "Choose a saved credential. Manage credentials on the Credentials page.",
            "autonomous": "Import open issues and make imported issues eligible for claiming. Manual tasks remain independently claimable.",
            "permitted_author_ids": "GitHub user IDs as JSON, for example [123]. Empty uses the repository owner ID.",
            "backfill_issues": "Explicitly allow older open issues created before execution was first enabled.",
        }
        widgets = {
            "permitted_author_ids": forms.TextInput(),
            "name": forms.TextInput(attrs={"placeholder": "e.g. Nightly queue"}),
            "repository_url": forms.URLInput(
                attrs={"placeholder": "https://github.com/owner/repo"}
            ),
            "base_branch": forms.TextInput(attrs={"placeholder": "Repository default"}),
            "autonomous": forms.CheckboxInput(
                attrs={
                    "aria-labelledby": "id_autonomous_label",
                    "aria-describedby": "id_autonomous_helptext",
                }
            ),
        }
